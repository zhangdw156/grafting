"""User agent implementations.

Two implementations of simulated user for trajectory synthesis:

- ChatUserAgent (recommended): uses chat-format messages with role
  inversion. Produces richer multi-turn interactions and benefits
  from API-level prompt caching. This is the default used by
  SimulationRunner.

- PromptUserAgent: uses a single long prompt per turn. Simpler but
  produces slightly fewer turns and no prompt caching benefit.

Both share the same interface (start/respond) and can be swapped
in SimulationRunner by changing the import.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from grafting.agent.user.config import UserAgentConfig
from grafting.agent.user.types import UserTurnResult
from grafting.utils import http_clients, uses_responses_api
from grafting.utils.token_usage import TokenUsage

DONE_MARKER = "[DONE]"

_SYSTEM_TEMPLATE = """\
You simulate a real user in a multi-turn dialogue with a customer service assistant.

## Your Persona
Role: {role}
Personality: {personality}
Knowledge boundary: {knowledge_boundary}

## Your Goal
{demand}

## Rules
- Stay fully in character. Use vocabulary and tone natural to your persona.
- Do NOT use tool names, API endpoints, parameter names, or system internals.
- Respond to the assistant's questions based on what your persona would know.
- If the assistant asks for information outside your knowledge boundary, say you don't know.
- When the assistant has fully completed your goal, output exactly: {done_marker}
- If the assistant cannot do what you asked and there is no way forward, output: {done_marker}
- Do NOT output {done_marker} until the goal is genuinely resolved or clearly impossible.
- Keep messages to 1-3 sentences. No markdown, no bullet points, no labels.

## Critical: Reveal Information Gradually
- In your FIRST message, only mention your primary reason for calling and give just \
enough context for the assistant to start helping (e.g. your name, that you need help \
with a booking). Do NOT list all your requests upfront.
- Reveal additional requests and specific details (dates, names, amounts) only when the \
assistant asks or after the current issue is resolved.
- A real person does not open a call by listing every single thing they need in one \
breath — they start with the most important thing and bring up the rest as the \
conversation progresses.
"""


class BaseUserAgent(ABC):
    """Base class for user agent implementations."""

    def __init__(self, config: UserAgentConfig):
        self.config = config

    @abstractmethod
    def start(self, task: dict[str, Any]) -> UserTurnResult:
        """Initialize from a task and generate the first user message."""

    @abstractmethod
    def respond(self, assistant_response: str) -> UserTurnResult:
        """Process assistant's response, return next user message or done."""

    @abstractmethod
    def get_message_count(self) -> int:
        """Return the number of messages generated so far."""

    def _build_system_prompt(self, task: dict[str, Any]) -> str:
        uac = task.get("user_agent_config", {})
        return _SYSTEM_TEMPLATE.format(
            role=uac.get("role", "a user"),
            personality=uac.get("personality", ""),
            knowledge_boundary=uac.get("knowledge_boundary", ""),
            demand=task.get("demand", ""),
            done_marker=DONE_MARKER,
        )

    def _parse_result(self, raw: str, usage: TokenUsage | None = None) -> UserTurnResult:
        cleaned = _clean_message(raw)
        if DONE_MARKER in raw:
            return UserTurnResult(message=cleaned, is_done=True, usage=usage)
        return UserTurnResult(message=cleaned, is_done=False, usage=usage)


class ChatUserAgent(BaseUserAgent):
    """User agent using chat-format messages with role inversion.

    In this agent's LLM calls:
    - system: persona + demand + rules
    - assistant: simulated user's messages (what we generate)
    - user: real assistant's responses (what we receive)

    This leverages native turn-taking and prompt caching.
    """

    def __init__(self, config: UserAgentConfig):
        super().__init__(config)
        self._messages: list[dict[str, str]] = []
        self._turn_count = 0

    def start(self, task: dict[str, Any]) -> UserTurnResult:
        system_prompt = self._build_system_prompt(task)
        self._messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "The conversation is starting now. Generate your first "
                    "message to the assistant. Mention only your most "
                    "immediate need — do not list everything upfront."
                ),
            },
        ]

        raw, usage = _call_model(
            self.config, self._messages,
        )
        result = self._parse_result(raw, usage)
        self._messages.append({"role": "assistant", "content": raw})
        self._turn_count = 1
        return result

    def respond(self, assistant_response: str) -> UserTurnResult:
        self._messages.append({"role": "user", "content": assistant_response})

        raw, usage = _call_model(
            self.config, self._messages,
        )
        result = self._parse_result(raw, usage)
        self._messages.append({"role": "assistant", "content": raw})
        self._turn_count += 1
        return result

    def get_message_count(self) -> int:
        return self._turn_count


class PromptUserAgent(BaseUserAgent):
    """User agent using a single long prompt per turn.

    Each turn assembles the full conversation history into one prompt
    with explicit Customer:/Agent: labels. No role inversion needed.
    """

    def __init__(self, config: UserAgentConfig):
        super().__init__(config)
        self._system_prompt = ""
        self._history: list[tuple[str, str]] = []
        self._turn_count = 0

    def start(self, task: dict[str, Any]) -> UserTurnResult:
        self._system_prompt = self._build_system_prompt(task)
        self._history = []

        prompt = self._assemble_prompt(first_turn=True)
        raw, usage = _call_model_single(self.config, prompt)
        result = self._parse_result(raw, usage)
        self._history.append(("Customer", raw))
        self._turn_count = 1
        return result

    def respond(self, assistant_response: str) -> UserTurnResult:
        self._history.append(("Agent", assistant_response))

        prompt = self._assemble_prompt(first_turn=False)
        raw, usage = _call_model_single(self.config, prompt)
        result = self._parse_result(raw, usage)
        self._history.append(("Customer", raw))
        self._turn_count += 1
        return result

    def get_message_count(self) -> int:
        return self._turn_count

    def _assemble_prompt(self, first_turn: bool) -> str:
        parts = [self._system_prompt, ""]

        if self._history:
            parts.append("## Conversation so far\n")
            for role, content in self._history:
                parts.append(f"{role}: {content}")
            parts.append("")

        if first_turn:
            parts.append(
                "Generate your first message to the assistant. "
                "Mention only your most immediate need — "
                "do not list everything upfront."
            )
        else:
            parts.append(
                "Generate your next message. Continue the conversation "
                "naturally based on the agent's latest response."
            )

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM calling helpers
# ---------------------------------------------------------------------------

def _call_model(
    config: UserAgentConfig,
    messages: list[dict[str, str]],
) -> tuple[str, TokenUsage | None]:
    if config.provider == "anthropic":
        return _call_anthropic_chat(config, messages)
    return _call_openai_chat(config, messages)


def _call_model_single(
    config: UserAgentConfig,
    prompt: str,
) -> tuple[str, TokenUsage | None]:
    if config.provider == "anthropic":
        return _call_anthropic_single(config, prompt)
    return _call_openai_single(config, prompt)


def _call_openai_chat(
    config: UserAgentConfig,
    messages: list[dict[str, str]],
) -> tuple[str, TokenUsage | None]:
    ep = config.endpoint
    if ep is None:
        raise RuntimeError(
            "No endpoint configured for user agent. "
            "Configure simulation.user_agent.endpoint in settings.yaml."
        )

    if uses_responses_api(ep.provider):
        raise ValueError(
            f"Simulation user agent requires a chat completions provider "
            f"(openai_chat_completions), "
            f"got {ep.provider!r}. The responses API is supported by "
            f"harness runners (Codex/OpenCode) but not by simulation agents."
        )

    model = ep.model
    client = http_clients.get_openai_client(
        api_key=ep.api_key,
        base_url=ep.base_url,
        timeout=120.0,
        max_retries=2,
    )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    usage = None
    if response.usage:
        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens or 0,
            output_tokens=response.usage.completion_tokens or 0,
        )
    return (response.choices[0].message.content or "").strip(), usage


def _call_openai_single(config: UserAgentConfig, prompt: str) -> tuple[str, TokenUsage | None]:
    return _call_openai_chat(
        config, [{"role": "user", "content": prompt}],
    )


def _call_anthropic_chat(
    config: UserAgentConfig,
    messages: list[dict[str, str]],
) -> tuple[str, TokenUsage | None]:
    ep = config.endpoint
    if ep is None:
        raise RuntimeError(
            "No endpoint configured for user agent. "
            "Configure simulation.user_agent.endpoint in settings.yaml."
        )

    model = ep.model
    client = http_clients.get_anthropic_client(
        api_key=ep.api_key, base_url=ep.base_url,
    )

    system = ""
    conv_messages = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            conv_messages.append({"role": m["role"], "content": m["content"]})

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=conv_messages,
        timeout=120.0,
    )
    usage = None
    if response.usage:
        inp = (
            (response.usage.input_tokens or 0)
            + getattr(response.usage, "cache_read_input_tokens", 0) or 0
            + getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        )
        usage = TokenUsage(
            input_tokens=inp,
            output_tokens=response.usage.output_tokens or 0,
        )
    for block in response.content:
        if block.type == "text":
            return block.text.strip(), usage
    return "", usage


def _call_anthropic_single(config: UserAgentConfig, prompt: str) -> tuple[str, TokenUsage | None]:
    return _call_anthropic_chat(
        config, [{"role": "user", "content": prompt}],
    )


def _clean_message(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = text.replace(DONE_MARKER, "").strip()
    text = re.sub(r"^(Customer|User):\s*", "", text).strip()
    return text
