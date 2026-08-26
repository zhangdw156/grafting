# ruff: noqa: E501
"""BFCL handler for models trained with the Grafting/Fathoming prompt format."""

from __future__ import annotations

import json

from bfcl_eval.model_handler.local_inference.qwen_fc import QwenFCHandler
from overrides import override

FATHOM_SYSTEM_PROMPT = """You are an assistant operating in a tool-using multi-turn conversation.

Your job is to help the user complete the current task by:
1. understanding the user's request,
2. deciding whether a tool is needed,
3. calling tools when they are actually useful,
4. giving the user a natural-language answer that is grounded in visible tool results.

Core rules:

- Be tool-first, not tool-only.
- If a tool is needed, call the tool before making factual claims that depend on it.
- If the available tools include discovery or listing tools (e.g. list_*, categories, get_config, get_schema), you MUST call them when the user asks about available options, supported values, or environment-specific metadata. Never answer such questions from your own knowledge alone.
- If the user has not provided enough information for a required tool call, ask a short clarifying question instead of guessing.
- If a tool call fails because a required parameter is missing, ask the user for that missing value instead of retrying with a guessed default, placeholder, or inferred identifier.
- If the available tools do not support the user's requested calculation or action, say so clearly.
- Do not invent numbers, facts, file contents, entities, or outcomes that are not supported by:
  - the user's messages,
  - prior tool results,
  - or clearly stated assumptions that you explicitly label as assumptions.
- Never present unsupported assumptions as computed results.
- If a tool returns an empty result, zero result, failure, or validation error, do not pretend the task succeeded.
- When tool results are partial, give a partial answer and explain the limitation briefly.

Tool-use policy:

- Use only available tools.
- Prefer the smallest set of tool calls that makes real progress.
- Do not call tools redundantly.
- Do not call a tool if the answer can already be given from prior tool outputs in the conversation.
- When multiple tools are relevant, use them in a sensible order.
- If no tool is needed for the current turn, respond directly in natural language.

Response policy:

- After tool use, provide a concise natural-language response to the user.
- Summarize the relevant result, not the raw tool protocol.
- Do not expose tool names, JSON schemas, internal state keys, or backend mechanics unless the user explicitly asks.
- Keep the answer focused on the user's current request.
- Do not jump ahead to unrelated future steps unless the user asks.
- If you need clarification, ask only the minimum question needed to continue.

Grounding policy:

- Any specific numeric result must be traceable to a tool result or explicit user-provided numbers.
- If the tool does not support the exact scenario, do not produce a made-up estimate.
- Instead say what the tool can do, what it cannot do, and what extra information or tool support would be needed.

Output policy:

- Normal case: return a helpful natural-language assistant message, and include tool calls when needed.
- Do not output hidden reasoning.
- Do not output XML wrappers like <tool_call> unless the runtime specifically requires them.
- Do not output raw JSON except when required for a tool call.

Priority order for each turn:

1. Stay grounded.
2. Make real progress with the available tools.
3. Ask for clarification only when necessary.
4. Keep the conversation natural and useful."""


def _build_system_with_tools(functions: list[dict]) -> str:
    if not functions:
        return FATHOM_SYSTEM_PROMPT
    tool_lines = "\n".join(
        json.dumps(function, ensure_ascii=False) for function in functions
    )
    return (
        FATHOM_SYSTEM_PROMPT
        + "\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
        + "You are provided with function signatures within <tools></tools> XML tags:\n"
        + "<tools>\n"
        + tool_lines
        + "\n</tools>\n\nFor each function call, return a json object with function "
        + "name and arguments within <tool_call></tool_call> XML tags:\n"
        + "<tool_call>\n"
        + '{"name": <function-name>, "arguments": <args-json-object>}\n'
        + "</tool_call>"
    )


_SUFFIX_NONE = ""
_SUFFIX_EMPTY_THINK_BLOCK = "<think>\n\n</think>\n\n"
_SUFFIX_THINK_OPEN = "<think>\n"


class FathomFCBase(QwenFCHandler):
    """Qwen XML tool-call handler using the paper's system prompt."""

    assistant_generation_suffix: str = _SUFFIX_NONE

    @override
    def _format_prompt(self, messages, function):
        if function:
            system_content = _build_system_with_tools(function)
        elif messages and messages[0].get("role") == "system":
            system_content = str(messages[0].get("content") or "")
        else:
            system_content = FATHOM_SYSTEM_PROMPT
        formatted = f"<|im_start|>system\n{system_content}<|im_end|>\n"

        last_query_index = len(messages) - 1
        for offset, message in enumerate(reversed(messages)):
            index = len(messages) - 1 - offset
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and not (
                    content.startswith("<tool_response>")
                    and content.endswith("</tool_response>")
                )
            ):
                last_query_index = index
                break

        for index, message in enumerate(messages):
            role = message["role"]
            content = message.get("content") or ""
            if role == "user" or (role == "system" and index != 0):
                formatted += f"<|im_start|>{role}\n{content}<|im_end|>\n"
                continue

            if role == "assistant":
                reasoning_content = str(message.get("reasoning_content") or "")
                if not reasoning_content and "</think>" in content:
                    before, content = content.rsplit("</think>", 1)
                    reasoning_content = before.split("<think>")[-1].strip("\n")
                    content = content.lstrip("\n")

                if index > last_query_index:
                    if index == len(messages) - 1 or reasoning_content:
                        formatted += (
                            "<|im_start|>assistant\n<think>\n"
                            + reasoning_content.strip("\n")
                            + "\n</think>\n\n"
                            + content.lstrip("\n")
                        )
                    else:
                        formatted += f"<|im_start|>assistant\n{content}"
                else:
                    formatted += f"<|im_start|>assistant\n{content}"

                for call_index, tool_call in enumerate(message.get("tool_calls") or []):
                    if (call_index == 0 and content) or call_index > 0:
                        formatted += "\n"
                    if "function" in tool_call:
                        tool_call = tool_call["function"]
                    arguments = tool_call["arguments"]
                    arguments_text = (
                        arguments
                        if isinstance(arguments, str)
                        else json.dumps(arguments)
                    )
                    formatted += (
                        '<tool_call>\n{"name": "'
                        + tool_call["name"]
                        + '", "arguments": '
                        + arguments_text
                        + "}\n</tool_call>"
                    )
                formatted += "<|im_end|>\n"
                continue

            if role == "tool":
                previous_role = messages[index - 1]["role"] if index > 0 else None
                next_role = (
                    messages[index + 1]["role"]
                    if index < len(messages) - 1
                    else None
                )
                if index == 0 or previous_role != "tool":
                    formatted += "<|im_start|>user"
                formatted += f"\n<tool_response>\n{content}\n</tool_response>"
                if index == len(messages) - 1 or next_role != "tool":
                    formatted += "<|im_end|>\n"

        return (
            formatted
            + "<|im_start|>assistant\n"
            + self.assistant_generation_suffix
        )


class FathomFCNoPrefixFCHandler(FathomFCBase):
    """Use the Fathom system prompt and append ``""``."""

    assistant_generation_suffix = _SUFFIX_NONE


class FathomFCEmptyThinkBlockPrefixFCHandler(FathomFCBase):
    """Use the Fathom system prompt and append ``"<think>\\n\\n</think>\\n\\n"``."""

    assistant_generation_suffix = _SUFFIX_EMPTY_THINK_BLOCK


class FathomFCOpenThinkPrefixFCHandler(FathomFCBase):
    """Use the Fathom system prompt and append ``"<think>\\n"``."""

    assistant_generation_suffix = _SUFFIX_THINK_OPEN


class FathomFCNoSPBase(QwenFCHandler):
    """Use the upstream Qwen system prompt with configurable thinking prefixes."""

    assistant_generation_suffix: str = _SUFFIX_NONE

    @override
    def _format_prompt(self, messages, function):
        formatted = super()._format_prompt(messages, function)
        return formatted + self.assistant_generation_suffix


class FathomFCNoPrefixNoSPFCHandler(FathomFCNoSPBase):
    """Use the upstream Qwen system prompt and append ``""``."""

    assistant_generation_suffix = _SUFFIX_NONE


class FathomFCEmptyThinkBlockPrefixNoSPFCHandler(FathomFCNoSPBase):
    """Use the upstream Qwen prompt and append ``"<think>\\n\\n</think>\\n\\n"``."""

    assistant_generation_suffix = _SUFFIX_EMPTY_THINK_BLOCK


class FathomFCOpenThinkPrefixNoSPFCHandler(FathomFCNoSPBase):
    """Use the upstream Qwen system prompt and append ``"<think>\\n"``."""

    assistant_generation_suffix = _SUFFIX_THINK_OPEN
