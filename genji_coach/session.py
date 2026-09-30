"""Per-session log and the end-of-session report."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .analyzer import Analysis, AnalysisError
from .capture import Frame
from .coach import Delivery
from .knowledge import KnowledgeBase

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). Cache reads bill at 0.1x input, writes at 1.25x.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

REPORT_PROMPT = """Below is a log of one Overwatch session, one line per analyzed screenshot of a Genji player, \
followed by the tips the coach gave. Write a short post-session review for the player in Markdown:

1. **Top 3 things to work on**, most impactful first. For each: what kept happening (cite log timestamps), \
why it cost them, and a concrete drill or habit. Reference knowledge-base tip ids in parentheses where they apply.
2. **What went well** (one or two bullets, only if the log supports it).

Be specific to this log. If the log is too thin to judge something, say so rather than guessing.

Knowledge base:
{kb}

Session log:
{log}

Tips given:
{tips}
"""


class Session:
    def __init__(self, root: Path, save_frames: bool, model: str):
        self.dir = root / time.strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.save_frames = save_frames
        self.model = model
        self._log = (self.dir / "log.jsonl").open("a", encoding="utf-8")
        self.entries: list[dict] = []
        self.deliveries: list[tuple[str, Delivery]] = []
        self.deaths = 0
        self._last_screen = ""
        self.usage = Counter()

    def record(self, frame: Frame, analysis: Analysis, delivery: Delivery | None) -> None:
        if analysis.screen == "dead_or_killcam" and self._last_screen == "in_match_alive":
            self.deaths += 1
        self._last_screen = analysis.screen
        self.usage.update(analysis.usage)
        entry = {
            "t": frame.label or time.strftime("%H:%M:%S", time.localtime(frame.timestamp)),
            "screen": analysis.screen,
            "genji": analysis.playing_genji,
            "situation": analysis.situation,
            "events": analysis.events,
            "ult": analysis.ult_percent,
            "suggested": asdict(analysis.tip),
            "delivered": asdict(delivery) if delivery else None,
        }
        self.entries.append(entry)
        if delivery:
            self.deliveries.append((entry["t"], delivery))
        self._log.write(json.dumps(entry) + "\n")
        self._log.flush()
        if self.save_frames:
            name = entry["t"].replace(":", "-").replace(" ", "_")
            (self.dir / f"{len(self.entries):05d}_{name}.jpg").write_bytes(frame.jpeg)

    def cost_estimate(self) -> float | None:
        u = self.usage
        if u["api_equivalent_micro_usd"]:
            return u["api_equivalent_micro_usd"] / 1_000_000
        price = PRICES.get(self.model)
        if not price:
            return None
        inp, out = price
        return (u["input"] * inp + u["cache_read"] * inp * 0.1 + u["cache_write"] * inp * 1.25
                + u["output"] * out) / 1_000_000

    def write_report(self, kb: KnowledgeBase, reviewer: Callable[[str], str] | None,
                     on_plan: bool = False) -> Path:
        genji_frames = [e for e in self.entries if e["genji"]]
        by_category = Counter(d.category for _, d in self.deliveries)
        lines = [
            f"# Genji session review ({self.dir.name})",
            "",
            f"- Frames analyzed: {len(self.entries)} ({len(genji_frames)} on Genji)",
            f"- Deaths spotted: {self.deaths}",
            f"- Tips given: {len(self.deliveries)}"
            + (f" ({', '.join(f'{c} {n}' for c, n in by_category.most_common())})" if by_category else ""),
        ]
        cost = self.cost_estimate()
        if cost is not None and on_plan:
            lines.append(f"- Ran on your Claude plan (the same calls would cost about ${cost:.2f} on the API)")
        elif cost is not None:
            lines.append(f"- Estimated API cost: ${cost:.2f}")
        lines += ["", "## Tips you heard", ""]
        for t, d in self.deliveries:
            credit = "; ".join(filter(None, (kb.credit(k) for k in d.kb_ids)))
            lines.append(f"- **{t}** {d.spoken} _{d.detail}_" + (f" (source: {credit})" if credit else ""))
        if not self.deliveries:
            lines.append("- None this session.")

        if reviewer and len(genji_frames) >= 5:
            lines += ["", "## Coach's review", "", self._ai_review(kb, reviewer, genji_frames)]

        path = self.dir / "report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _ai_review(self, kb: KnowledgeBase, reviewer: Callable[[str], str], frames: list[dict]) -> str:
        log_text = "\n".join(
            f"[{e['t']}] {e['screen']}, ult {e['ult']}%: {e['situation']}"
            + (f" | events: {'; '.join(e['events'])}" if e["events"] else "")
            for e in frames
        )
        tips = "\n".join(f"[{t}] ({d.category}) {d.spoken}" for t, d in self.deliveries) or "(none)"
        try:
            return reviewer(REPORT_PROMPT.format(kb=kb.as_prompt(), log=log_text, tips=tips))
        except AnalysisError as e:
            log.warning("Couldn't write the AI review: %s", e)
            return "_Review unavailable: the Claude call failed._"

    def close(self) -> None:
        self._log.close()
