"""Synthetic inputs only: no production or third-party data."""

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def make_file(tmp_path: Path) -> Callable[..., Path]:
    def create(
        content: str | bytes,
        *,
        name: str = "input.csv",
        encoding: str = "utf-8",
    ) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode(encoding) if isinstance(content, str) else content)
        return path

    return create
