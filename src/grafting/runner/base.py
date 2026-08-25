from __future__ import annotations

from pathlib import Path


class BaseRunner:

    def __init__(
        self,
        *,
        output_root: Path,
        workers: int = 1,
    ) -> None:
        self.output_root = output_root
        self.workers = workers
        self.output_root.mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        pass
