from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from grafting.agent.assistant.config import AssistantAgentConfig
from grafting.agent.assistant.types import AssistantToolCall, AssistantTurnResult
from grafting.utils import (
    canonicalize_tool_name,
    http_clients,
    logger,
    uses_responses_api,
)
from grafting.utils.token_usage import TokenUsage

_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


def _load_assistant_system_prompt() -> str:
    prompt_file = Path(__file__).resolve().parent / "prompt.md"
    try:
        text = prompt_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return text.strip()


ASSISTANT_SYSTEM_PROMPT = _load_assistant_system_prompt()


def _build_anthropic_client() -> Any:
    """Build Anthropic client with support for local proxy environments."""
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if base_url and auth_token and not api_key:
        return http_clients.get_anthropic_client(
            base_url=base_url,
            api_key=auth_token,
        )
    return http_clients.get_anthropic_client(api_key=api_key)


class AssistantAgent:
    """Unified multi-turn tool-calling agent supporting OpenAI and Anthropic backends."""

    def __init__(self, config: AssistantAgentConfig):
        self.config = config
        self._openai_client: Any = None
        self._anthropic_client: Any = None
        self._openai_model: str = ""
        self._tools_raw: list[dict[str, Any]] = []
        self._tools_converted: list[dict[str, Any]] = []
        self._tool_executor: Any = None
        self._tool_names: list[str] = []
        self._state_key: str = ""
        self._anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", _DEFAULT_ANTHROPIC_MODEL)
        self._use_local_openai: bool = False

    def create(
        self,
        *,
        state_key: str = "",
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any = None,
    ) -> None:
        self._state_key = state_key
        self._tool_executor = tool_executor

        if tools:
            self._tools_raw = tools
            self._tools_converted = [self._convert_tool_schema(t) for t in tools]
            self._tool_names = [canonicalize_tool_name(t.get("name", "")) for t in tools]

        if self.config.provider == "anthropic":
            ep = self.config.endpoint
            if ep is not None:
                self._anthropic_client = http_clients.get_anthropic_client(
                    api_key=ep.api_key,
                    base_url=ep.base_url,
                )
                self._anthropic_model = ep.model
            else:
                self._anthropic_client = _build_anthropic_client()
            logger.info(
                "Assistant agent created (anthropic) with {} tools, model={}",
                len(self._tools_converted),
                self._anthropic_model,
            )
        else:
            ep = self.config.endpoint
            if ep is not None:
                if uses_responses_api(ep.provider):
                    raise ValueError(
                        f"Simulation assistant agent requires a chat completions "
                        f"provider (openai_chat_completions), "
                        f"got {ep.provider!r}. "
                        f"The responses API is supported by harness runners "
                        f"(Codex/OpenCode) but not by simulation agents."
                    )
                self._openai_model = ep.model
                logger.info(
                    "Assistant agent created (openai/endpoint) with {} tools, model={}",
                    len(self._tools_converted),
                    self._openai_model,
                )
            else:
                env_base_url = os.environ.get("OPENAI_BASE_URL")
                env_api_key = os.environ.get("OPENAI_API_KEY")
                if env_base_url and env_api_key:
                    self._use_local_openai = True
                    self._openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o")
                    logger.info(
                        "Assistant agent created (openai/local) with {} tools, model={}",
                        len(self._tools_converted),
                        self._openai_model,
                    )
                else:
                    self._use_local_openai = False
                    logger.info(
                        "Assistant agent created (openai) with {} tools",
                        len(self._tools_converted),
                    )

    def get_tool_names(self) -> list[str]:
        return list(self._tool_names)

    def close(self) -> None:
        self._openai_client = None
        self._anthropic_client = None

    def run_turn(self, *, messages: list[dict[str, Any]]) -> AssistantTurnResult:
        if self.config.provider == "anthropic":
            return self._run_turn_anthropic(messages)
        return self._run_turn_openai(messages)

    # -------------------------------------------------------------------------
    # OpenAI backend
    # -------------------------------------------------------------------------

    def _refresh_openai_client(self) -> None:
        ep = self.config.endpoint
        if ep is not None:
            self._openai_model = ep.model
            self._openai_client = http_clients.get_openai_client(
                api_key=ep.api_key,
                base_url=ep.base_url,
                timeout=self.config.request_timeout_sec,
                max_retries=self.config.max_retries,
            )
            return

        if self._use_local_openai:
            self._openai_client = http_clients.get_openai_client(
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.environ["OPENAI_BASE_URL"],
                timeout=self.config.request_timeout_sec,
                max_retries=self.config.max_retries,
            )
            return

        raise RuntimeError(
            "No endpoint configured for assistant agent. "
            "Configure simulation.assistant_agent.endpoint in settings.yaml."
        )

    def _run_turn_openai(self, messages: list[dict[str, Any]]) -> AssistantTurnResult:
        self._refresh_openai_client()
        if self._openai_client is None:
            raise RuntimeError("Agent not created — call create() first")

        openai_messages = self._convert_messages_openai(messages)
        all_output_messages: list[dict[str, Any]] = []
        tool_calls: list[AssistantToolCall] = []
        assistant_message = ""
        thinking_parts: list[str] = []
        turn_usage = TokenUsage()

        for _ in range(self.config.max_llm_calls_per_run):
            kwargs: dict[str, Any] = {
                "model": self._openai_model,
                "messages": openai_messages,
            }
            if self._tools_converted:
                kwargs["tools"] = self._tools_converted

            response = self._openai_client.chat.completions.create(**kwargs)
            if response.usage:
                turn_usage = turn_usage + TokenUsage(
                    input_tokens=response.usage.prompt_tokens or 0,
                    output_tokens=response.usage.completion_tokens or 0,
                )
            choice = response.choices[0]
            msg = choice.message

            current_text = (msg.content or "").strip()
            # Extract thinking before stripping
            turn_thinking = self._extract_thinking(current_text)
            api_reasoning = getattr(msg, "reasoning_content", None) or ""
            if api_reasoning:
                if turn_thinking:
                    turn_thinking = f"{api_reasoning}\n{turn_thinking}"
                else:
                    turn_thinking = api_reasoning
            if turn_thinking:
                thinking_parts.append(turn_thinking)
            # Strip <think> blocks from reasoning models
            current_text = self._strip_thinking(current_text)
            if current_text:
                assistant_message = current_text

            turn_tool_calls: list[dict[str, Any]] = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    turn_tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments_str": tc.function.arguments,
                        }
                    )

            # Build canonical output message
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": current_text,
            }
            if turn_tool_calls:
                assistant_msg["function_calls"] = [
                    {
                        "name": canonicalize_tool_name(tc["name"]),
                        "arguments": self._parse_arguments(tc["arguments_str"]),
                        "tool_use_id": tc["id"],
                    }
                    for tc in turn_tool_calls
                ]
            all_output_messages.append(assistant_msg)

            if not turn_tool_calls or choice.finish_reason == "stop":
                break

            # Feed assistant response back and execute tools
            openai_tool_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                openai_tool_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            openai_messages.append(openai_tool_msg)

            for tc in turn_tool_calls:
                canonical_name = canonicalize_tool_name(tc["name"])
                args = self._parse_arguments(tc["arguments_str"])
                result_text = self._execute_tool(canonical_name, args)
                tool_calls.append(
                    AssistantToolCall(
                        name=canonical_name,
                        arguments=tc["arguments_str"],
                        result=result_text,
                    )
                )
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    }
                )
                all_output_messages.append(
                    {
                        "role": "function",
                        "name": canonical_name,
                        "content": result_text,
                        "tool_use_id": tc["id"],
                    }
                )

        return AssistantTurnResult(
            messages=all_output_messages,
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            raw_response=[],
            reasoning_content="\n".join(thinking_parts),
            usage=turn_usage,
        )

    def _run_turn_anthropic(self, messages: list[dict[str, Any]]) -> AssistantTurnResult:
        if self._anthropic_client is None:
            raise RuntimeError("Agent not created — call create() first")

        anthropic_messages = self._convert_messages_anthropic(messages)
        all_output_messages: list[dict[str, Any]] = []
        tool_calls: list[AssistantToolCall] = []
        assistant_message = ""
        thinking_parts: list[str] = []
        turn_usage = TokenUsage()

        for _ in range(self.config.max_llm_calls_per_run):
            kwargs = {
                "model": self._anthropic_model,
                "max_tokens": 32768,
                "system": self._get_system_prompt(),
                "tools": self._tools_converted,
                "messages": anthropic_messages,
                "timeout": self.config.request_timeout_sec,
            }
            
            # Extended Thinking configuration
            thinking_budget = os.environ.get("ANTHROPIC_THINKING_BUDGET")
            if thinking_budget and int(thinking_budget) > 0:
                kwargs["thinking"] = {
                    "type": "enabled", 
                    "budget_tokens": int(thinking_budget)
                }

            response = self._anthropic_client.messages.create(**kwargs)
            if response.usage:
                inp = (
                    (response.usage.input_tokens or 0)
                    + getattr(response.usage, "cache_read_input_tokens", 0) or 0
                    + getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                )
                turn_usage = turn_usage + TokenUsage(
                    input_tokens=inp,
                    output_tokens=response.usage.output_tokens or 0,
                )

            text_parts: list[str] = []
            turn_tool_calls: list[dict[str, Any]] = []

            for block in response.content:
                if block.type == "thinking":
                    thinking_parts.append(block.thinking)
                elif block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    turn_tool_calls.append(
                        {
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            current_text = "\n".join(text_parts).strip()
            if current_text:
                assistant_message = current_text

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": current_text,
            }
            if turn_tool_calls:
                assistant_msg["function_calls"] = [
                    {
                        "name": canonicalize_tool_name(tc["name"]),
                        "arguments": tc["input"],
                        "tool_use_id": tc["id"],
                    }
                    for tc in turn_tool_calls
                ]
            all_output_messages.append(assistant_msg)

            if not turn_tool_calls or response.stop_reason == "end_turn":
                break

            # Execute tools and feed results back
            anthropic_messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []

            for tc in turn_tool_calls:
                canonical_name = canonicalize_tool_name(tc["name"])
                result_text = self._execute_tool(canonical_name, tc["input"])
                tool_calls.append(
                    AssistantToolCall(
                        name=canonical_name,
                        arguments=json.dumps(tc["input"], ensure_ascii=False),
                        result=result_text,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": result_text,
                    }
                )
                all_output_messages.append(
                    {
                        "role": "function",
                        "name": canonical_name,
                        "content": result_text,
                        "tool_use_id": tc["id"],
                    }
                )

            anthropic_messages.append({"role": "user", "content": tool_results})

        return AssistantTurnResult(
            messages=all_output_messages,
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            raw_response=[],
            reasoning_content="\n".join(thinking_parts),
            usage=turn_usage,
        )

    # -------------------------------------------------------------------------
    # Tool execution
    # -------------------------------------------------------------------------

    def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._tool_executor is None:
            return json.dumps({"error": "no tool executor configured"})
        try:
            result = self._tool_executor(
                tool_name=tool_name,
                arguments=arguments,
                state_key=self._state_key,
            )
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.error("Tool execution failed for {}: {}", tool_name, exc)
            return json.dumps({"error": str(exc)})

    # -------------------------------------------------------------------------
    # Schema conversion
    # -------------------------------------------------------------------------

    def _convert_tool_schema(self, tool: dict[str, Any]) -> dict[str, Any]:
        """Convert MCP/tools.jsonl schema to the backend-specific format."""
        input_schema = (
            tool.get("inputSchema")
            or tool.get("input_schema")
            or {
                "type": "object",
                "properties": {},
            }
        )
        props = dict(input_schema.get("properties", {}))
        props.pop("__state_key", None)
        required = [r for r in input_schema.get("required", []) if r != "__state_key"]
        cleaned_schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
        }
        if required:
            cleaned_schema["required"] = required

        name = canonicalize_tool_name(tool.get("name", ""))
        description = tool.get("description", "")

        if self.config.provider == "anthropic":
            return {
                "name": name,
                "description": description,
                "input_schema": cleaned_schema,
            }
        else:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": cleaned_schema,
                },
            }

    # -------------------------------------------------------------------------
    # Message conversion — OpenAI
    # -------------------------------------------------------------------------

    def _get_system_prompt(self) -> str:
        if self.config.extra_system_prompt:
            return ASSISTANT_SYSTEM_PROMPT + "\n\n" + self.config.extra_system_prompt
        return ASSISTANT_SYSTEM_PROMPT

    def _convert_messages_openai(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical messages to OpenAI chat format."""
        result: list[dict[str, Any]] = [
            {"role": "system", "content": self._get_system_prompt()},
        ]

        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                result.append(
                    {
                        "role": "user",
                        "content": msg.get("content", ""),
                    }
                )
            elif role == "assistant":
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                }
                fc_list = msg.get("function_calls")
                if isinstance(fc_list, list) and fc_list:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": fc.get("tool_use_id", f"call_{fc['name']}"),
                            "type": "function",
                            "function": {
                                "name": fc["name"],
                                "arguments": (
                                    fc["arguments"]
                                    if isinstance(fc["arguments"], str)
                                    else json.dumps(fc["arguments"], ensure_ascii=False)
                                ),
                            },
                        }
                        for fc in fc_list
                    ]
                result.append(assistant_msg)
            elif role == "function":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_use_id", f"call_{msg.get('name', '')}"),
                        "content": msg.get("content", ""),
                    }
                )

        return result

    # -------------------------------------------------------------------------
    # Message conversion — Anthropic
    # -------------------------------------------------------------------------

    def _convert_messages_anthropic(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical messages to Anthropic message format."""
        result: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                if pending_tool_results:
                    result.append({"role": "user", "content": pending_tool_results})
                    pending_tool_results = []
                result.append(
                    {
                        "role": "user",
                        "content": msg.get("content", ""),
                    }
                )

            elif role == "assistant":
                if pending_tool_results:
                    result.append({"role": "user", "content": pending_tool_results})
                    pending_tool_results = []

                content_blocks: list[dict[str, Any]] = []
                text = (msg.get("content") or "").strip()
                if text:
                    content_blocks.append({"type": "text", "text": text})

                fc_list = msg.get("function_calls")
                if isinstance(fc_list, list) and fc_list:
                    for fc in fc_list:
                        args = fc.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        tool_use_id = fc.get("tool_use_id", f"toolu_{fc['name']}")
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": fc["name"],
                                "input": args,
                            }
                        )

                if content_blocks:
                    result.append({"role": "assistant", "content": content_blocks})

            elif role == "function":
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_use_id", f"toolu_{msg.get('name', '')}"),
                        "content": msg.get("content", ""),
                    }
                )

        if pending_tool_results:
            result.append({"role": "user", "content": pending_tool_results})

        return result

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_arguments(args_str: str) -> dict[str, Any]:
        if not args_str or not args_str.strip():
            return {}
        try:
            parsed = json.loads(args_str)
            if isinstance(parsed, dict):
                return parsed
            return {"_raw": parsed}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Strip <think>...</think> blocks from model output. Handles missing <think> tag."""
        if not text:
            return text
        if "</think>" in text and "<think>" not in text:
            parts = text.split("</think>", 1)
            return parts[1].strip() if len(parts) > 1 else ""
        if "<think>" not in text:
            return text
        import re

        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    @staticmethod
    def _extract_thinking(text: str) -> str:
        """Extract <think>...</think> content from model output. Handles missing <think> tag."""
        if not text:
            return ""
        if "</think>" in text and "<think>" not in text:
            return text.split("</think>", 1)[0].strip()
        if "<think>" not in text:
            return ""
        import re

        blocks = re.findall(r"<think>(.*?)</think>", text, flags=re.DOTALL)
        return "\n".join(b.strip() for b in blocks if b.strip())
