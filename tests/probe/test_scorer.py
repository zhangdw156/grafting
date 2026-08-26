from __future__ import annotations

from probe.scorer import compare_tool_calls, extract_tool_calls, score_response


def test_extract_and_compare_tool_calls() -> None:
    first = {"name": "a", "arguments": {"x": 1}}
    second = {"name": "b", "arguments": {"values": [1, 2]}}
    text = (
        '<tool_call>{"name": "a", "arguments": {"x": 1}}</tool_call>'
        '<tool_call>{"name": "b", "arguments": {"values": [1, 2]}}</tool_call>'
    )
    calls = extract_tool_calls(text)
    assert calls[0] == first
    assert calls[1] == second
    assert extract_tool_calls("<tool_call>not json</tool_call>") == []
    assert compare_tool_calls([first, second], [second, first])
    assert not compare_tool_calls([first], [second])


def test_score_response() -> None:
    ground_truth = (
        '<tool_call>{"name": "search", "arguments": {"q": "x"}}</tool_call>'
    )
    assert score_response(ground_truth, ground_truth) == (0.1, 1.0)
    assert score_response(
        '<tool_call>{"name": "other", "arguments": {}}</tool_call>',
        ground_truth,
    ) == (0.1, 0.0)
    assert score_response("plain text", ground_truth) == (0.0, 0.0)
    assert score_response("<tool_call>not json</tool_call>", ground_truth) == (
        0.0,
        0.0,
    )


def test_compare_preserves_list_multiplicity() -> None:
    repeated = [{"name": "a", "arguments": {"values": [1, 1]}}]
    single = [{"name": "a", "arguments": {"values": [1]}}]
    assert not compare_tool_calls(repeated, single)
