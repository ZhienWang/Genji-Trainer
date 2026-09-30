"""Decides which of Claude's suggested tips actually get said, and when.

Claude suggests a tip on most frames; hearing all of them would be noise. The coach
holds lessons until you're dead or out of a fight, and spaces tips out.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analyzer import Analysis, Tip

MIN_CONFIDENCE = 0.6
PENDING_TTL = 60.0        # a held lesson goes stale after this long
REPEAT_TIP_SECONDS = 240.0


@dataclass
class Delivery:
    spoken: str
    detail: str
    category: str
    kb_ids: list[str]
    urgency: str


class Coach:
    def __init__(self, min_gap: float, category_cooldown: float, live_tips: bool = True):
        self.min_gap = min_gap
        self.category_cooldown = category_cooldown
        self.live_tips = live_tips
        self._last_spoken = float("-inf")
        self._last_by_category: dict[str, float] = {}
        self._last_by_tip: dict[str, float] = {}
        self._pending: tuple[Tip, float] | None = None
        self.said: list[str] = []

    def _on_cooldown(self, tip: Tip, now: float) -> bool:
        if now - self._last_by_category.get(tip.category, float("-inf")) < self.category_cooldown:
            return True
        return any(now - self._last_by_tip.get(k, float("-inf")) < REPEAT_TIP_SECONDS for k in tip.kb_ids)

    def _deliver(self, tip: Tip, now: float) -> Delivery:
        self._last_spoken = now
        self._last_by_category[tip.category] = now
        for k in tip.kb_ids:
            self._last_by_tip[k] = now
        self.said.append(tip.spoken)
        return Delivery(tip.spoken, tip.detail, tip.category, tip.kb_ids, tip.urgency)

    def consider(self, analysis: Analysis, now: float) -> Delivery | None:
        if self._pending and now - self._pending[1] > PENDING_TTL:
            self._pending = None

        if not analysis.in_match:
            return None

        tip = analysis.tip
        usable = (analysis.playing_genji and tip.should_speak and tip.spoken.strip()
                  and tip.category != "none" and tip.confidence >= MIN_CONFIDENCE)
        alive_in_fight = analysis.screen == "in_match_alive"

        if usable and alive_in_fight and tip.urgency != "now":
            # A lesson, not a call-out: hold it for the next quiet moment.
            if not self._pending or tip.confidence >= self._pending[0].confidence:
                self._pending = (tip, now)
            usable = False
        if usable and tip.urgency == "now" and not self.live_tips:
            usable = False

        if now - self._last_spoken < self.min_gap:
            return None

        if not alive_in_fight and analysis.playing_genji:
            # Quiet moment (dead, killcam, scoreboard): a fresh death lesson wins over a held one.
            for candidate in ([tip] if usable else []) + ([self._pending[0]] if self._pending else []):
                if not self._on_cooldown(candidate, now):
                    self._pending = None
                    return self._deliver(candidate, now)
            return None

        if usable and not self._on_cooldown(tip, now):
            return self._deliver(tip, now)
        return None
