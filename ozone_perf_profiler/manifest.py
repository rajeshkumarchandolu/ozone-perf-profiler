"""One directory per run: manifest.json (answers, never secrets) plus each phase's outputs."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class RunDir:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, out_dir: Path, name: str) -> RunDir:
        if not RUN_NAME.fullmatch(name):
            raise ValueError(f"run name {name!r}: use letters, digits, '.', '_' and '-'")
        path = out_dir / name
        path.mkdir(parents=True, exist_ok=False)
        return cls(path)

    @property
    def name(self) -> str:
        return self.path.name

    def write_json(self, name: str, data: Any) -> Path:
        return self.write_text(name, json.dumps(data, indent=2, default=_encode) + "\n")

    def write_text(self, name: str, text: str, mode: int | None = None) -> Path:
        """Atomic: a reader never sees a half-written file."""
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text)
        if mode is not None:
            tmp.chmod(mode)
        tmp.replace(path)
        return path

    def update_manifest(self, phase: str, answers: dict[str, Any]) -> None:
        path = self.path / "manifest.json"
        manifest = json.loads(path.read_text()) if path.exists() else {"run_name": self.name,
                                                                         "created": utc_stamp()}
        manifest[phase] = answers
        self.write_json("manifest.json", manifest)
