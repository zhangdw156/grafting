from __future__ import annotations

from typing import Any


def get_openai_client(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    **kwargs: Any,
) -> Any:
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries if max_retries is not None else 2,
        **kwargs,
    )


def get_anthropic_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        **kwargs,
    )
