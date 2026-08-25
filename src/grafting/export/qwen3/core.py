"""Convert a trajectory dictionary into one Qwen3 SFT record."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from grafting.agent import ASSISTANT_SYSTEM_PROMPT
from grafting.utils import canonicalize_tool_name

RUNTIME_ARGUMENT_KEYS = {"__state_key"}

SYSTEM_PROMPT = ASSISTANT_SYSTEM_PROMPT


def strip_tool_prefix(name: Any) -> str:
    return canonicalize_tool_name(name)


def try_parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def clean_runtime_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            str(key): clean_runtime_keys(value)
            for key, value in obj.items()
            if str(key) not in RUNTIME_ARGUMENT_KEYS
        }
    if isinstance(obj, list):
        return [clean_runtime_keys(item) for item in obj]
    return obj


def normalize_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(tool)
    normalized["name"] = strip_tool_prefix(normalized.get("name", ""))
    parameters = normalized.pop("inputSchema", None)
    if parameters is None:
        parameters = normalized.get("parameters")
    normalized["parameters"] = clean_runtime_keys(
        parameters or {"type": "object", "properties": {}}
    )
    return normalized


def normalize_tools(raw_tools: list[Any]) -> list[dict[str, Any]]:
    return [normalize_tool_schema(tool) for tool in raw_tools if isinstance(tool, dict)]


def normalize_function_call_obj(name: Any, arguments: Any) -> dict[str, Any]:
    parsed_arguments = clean_runtime_keys(try_parse_json(arguments))
    if not isinstance(parsed_arguments, dict):
        parsed_arguments = {"_raw": parsed_arguments}
    return {
        "name": strip_tool_prefix(name),
        "arguments": parsed_arguments,
    }


def format_tool_call_block(name: Any, arguments: Any) -> str:
    obj = normalize_function_call_obj(name, arguments)
    args_json = json.dumps(obj["arguments"], ensure_ascii=False)
    return f'<tool_call>\n{{"name": "{obj["name"]}", "arguments": {args_json}}}\n</tool_call>'


def format_tool_response_block(content: Any) -> str:
    body = "" if content is None else str(content)
    return f"<tool_response>\n{body}\n</tool_response>"


def extract_tool_call_dicts_from_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, str) or not content.strip():
        return []
    pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    calls: list[dict[str, Any]] = []
    for raw_block in pattern.findall(content):
        parsed = try_parse_json(raw_block.strip())
        if not isinstance(parsed, dict):
            continue
        calls.append(
            normalize_function_call_obj(
                parsed.get("name", ""),
                parsed.get("arguments", {}),
            )
        )
    return calls


def extract_message_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []

    # Prefer function_calls (plural array) over function_call (singular)
    fc_list = message.get("function_calls")
    if isinstance(fc_list, list) and fc_list:
        for fc in fc_list:
            if isinstance(fc, dict) and fc.get("name"):
                tool_calls.append(
                    normalize_function_call_obj(
                        fc.get("name"),
                        fc.get("arguments", {}),
                    )
                )
    else:
        function_call = message.get("function_call")
        if isinstance(function_call, dict) and function_call.get("name"):
            tool_calls.append(
                normalize_function_call_obj(
                    function_call.get("name"),
                    function_call.get("arguments", {}),
                )
            )

    raw_tool_calls = message.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        for item in raw_tool_calls:
            if isinstance(item, dict) and isinstance(item.get("function"), dict):
                item = item["function"]
            if isinstance(item, dict) and item.get("name"):
                tool_calls.append(
                    normalize_function_call_obj(
                        item.get("name"),
                        item.get("arguments", {}),
                    )
                )

    tool_calls.extend(extract_tool_call_dicts_from_content(message.get("content")))
    return tool_calls


def build_system_content(tools: list[dict[str, Any]]) -> str:
    base_prompt = SYSTEM_PROMPT
    tool_lines = "\n".join(json.dumps(tool, ensure_ascii=False) for tool in tools)
    if tools:
        return (
            base_prompt
            + "\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
            + "You are provided with function signatures within <tools></tools> XML tags:\n<tools>"
            + (f"\n{tool_lines}\n" if tool_lines else "\n")
            + "</tools>\n\nFor each function call, return a json object with function name and "
            + "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n"
            + '{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>'
        )
    return base_prompt


def rebuild_messages(raw_messages: list[Any], system_content: str) -> list[dict[str, str]]:
    rebuilt: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    pending_assistant_texts: list[str] = []
    pending_assistant_tool_calls: list[dict[str, Any]] = []
    seen_user = False

    def flush_assistant() -> None:
        nonlocal pending_assistant_texts, pending_assistant_tool_calls
        if pending_assistant_tool_calls:
            content = "\n".join(
                format_tool_call_block(call["name"], call["arguments"])
                for call in pending_assistant_tool_calls
            )
            rebuilt.append({"role": "assistant", "content": content})
        else:
            content = "\n".join(text for text in pending_assistant_texts if text.strip()).strip()
            if content:
                rebuilt.append({"role": "assistant", "content": content})
        pending_assistant_texts = []
        pending_assistant_tool_calls = []

    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue

        role = str(raw_message.get("role", "")).strip()
        if role == "system":
            continue

        if role == "user":
            flush_assistant()
            rebuilt.append({"role": "user", "content": str(raw_message.get("content", "") or "")})
            seen_user = True
            continue

        if not seen_user:
            continue

        if role == "assistant":
            content = str(raw_message.get("content", "") or "").strip()
            tool_calls = extract_message_tool_calls(raw_message)
            if tool_calls:
                pending_assistant_tool_calls.extend(tool_calls)
            elif content:
                pending_assistant_texts.append(content)
            continue

        if role in {"function", "tool"}:
            flush_assistant()
            rebuilt.append(
                {
                    "role": "user",
                    "content": format_tool_response_block(raw_message.get("content", "")),
                }
            )
            continue

        flush_assistant()
        content = str(raw_message.get("content", "") or "")
        if content:
            rebuilt.append({"role": role, "content": content})

    flush_assistant()
    return rebuilt


def build_output_record(trajectory: dict[str, Any], source_trajectory_path: str) -> dict[str, Any]:
    """
    将单条 trajectory（dict）转换为 SFT 单条记录。

    对 trajectory 的 messages 做格式重写（system/user/assistant、<tool_call>/<tool_response> 等），
    并归一化 tools，得到含 source_trajectory_path、run_id、messages、tools 的 record。
    """

    raw_tools = trajectory.get("tools") or trajectory.get("tools_jsonl") or []
    if not isinstance(raw_tools, list):
        raw_tools = []

    raw_messages = trajectory.get("messages") or []
    if not isinstance(raw_messages, list):
        raise ValueError("trajectory.messages 必须是数组")

    tools = normalize_tools(raw_tools)
    system_content = build_system_content(tools)

    return {
        "source_trajectory_path": source_trajectory_path,
        "run_id": trajectory.get("run_id", ""),
        "messages": rebuild_messages(raw_messages, system_content),
        "tools": tools,
    }
