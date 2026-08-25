from __future__ import annotations

from collections.abc import Callable
from functools import wraps


def exit_with_code[**P](func: Callable[P, int]) -> Callable[P, None]:
    """Propagate a Hydra task function's integer result to the shell."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        code = func(*args, **kwargs)
        if code:
            raise SystemExit(code)

    return wrapper
