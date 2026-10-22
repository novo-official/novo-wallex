"""Versioned prompt templates. The version is the file name plus a hash of its text, logged with every call."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str

    @property
    def version(self) -> str:
        return f"{self.name}@{hashlib.sha256(self.text.encode()).hexdigest()[:8]}"


def load_prompt(name: str) -> Prompt:
    return Prompt(name, (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip())
