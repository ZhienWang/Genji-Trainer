"""Loads the Genji knowledge base (curated tips plus anything learned from videos)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

KB_FILES = ("genji_kb.json", "learned.json")


@dataclass
class Source:
    id: str
    creator: str
    title: str
    url: str
    kind: str = ""
    note: str = ""


@dataclass
class Tip:
    id: str
    category: str
    situation: str
    tip: str
    sources: list[str]
    patch_sensitive: bool = False
    timestamps: list[str] = field(default_factory=list)


@dataclass
class KnowledgeBase:
    sources: dict[str, Source]
    tips: dict[str, Tip]

    @classmethod
    def load(cls, directory: Path) -> "KnowledgeBase":
        sources: dict[str, Source] = {}
        tips: dict[str, Tip] = {}
        for name in KB_FILES:
            path = directory / name
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for s in data.get("sources", []):
                sources[s["id"]] = Source(**s)
            for t in data.get("tips", []):
                tips[t["id"]] = Tip(**t)
        kb = cls(sources, tips)
        kb.validate()
        return kb

    def validate(self) -> None:
        if not self.tips:
            raise ValueError("Knowledge base has no tips")
        for tip in self.tips.values():
            if not tip.sources:
                raise ValueError(f"Tip {tip.id} cites no source")
            missing = [s for s in tip.sources if s not in self.sources]
            if missing:
                raise ValueError(f"Tip {tip.id} cites unknown sources: {missing}")

    def credit(self, tip_id: str) -> str:
        """Short human-readable attribution, e.g. 'Necros (featured by KarQ)'."""
        tip = self.tips.get(tip_id)
        if not tip:
            return ""
        return ", ".join(self.sources[s].creator for s in tip.sources)

    def as_prompt(self) -> str:
        """Compact, deterministic rendering for the system prompt (stable so it caches)."""
        lines = []
        for tip in sorted(self.tips.values(), key=lambda t: (t.category, t.id)):
            flag = " [patch-sensitive]" if tip.patch_sensitive else ""
            lines.append(f"- {tip.id} ({tip.category}){flag}: WHEN {tip.situation}. TIP: {tip.tip}")
        return "\n".join(lines)
