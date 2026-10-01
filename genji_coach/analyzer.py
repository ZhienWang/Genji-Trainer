"""Sends frames to Claude and gets back a structured read of the game plus one coaching tip."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

import anthropic

from .capture import Frame
from .knowledge import KnowledgeBase

log = logging.getLogger(__name__)

SCREENS = ["in_match_alive", "dead_or_killcam", "hero_select", "menu_or_lobby",
           "scoreboard", "not_overwatch", "unknown"]
CATEGORIES = ["positioning", "dash", "deflect", "dragonblade", "mechanics",
              "matchups", "survival", "ult_economy", "none"]
URGENCY = ["now", "after_fight", "between_lives"]

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "screen": {"type": "string", "enum": SCREENS},
        "playing_genji": {"type": "boolean"},
        "situation": {"type": "string", "description": "One sentence: what is happening to the player right now."},
        "events": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short notable events visible in the frames, e.g. 'eliminated by Cassidy', 'dragonblade active', 'health low'.",
        },
        "ult_percent": {"type": "integer", "description": "Ultimate charge shown on the HUD, or -1 if not visible."},
        "tip": {
            "type": "object",
            "properties": {
                "should_speak": {"type": "boolean"},
                "spoken": {"type": "string", "description": "What to say aloud: at most 14 words, imperative, no hero-name filler."},
                "detail": {"type": "string", "description": "One or two sentences explaining why, for the overlay and the session log."},
                "category": {"type": "string", "enum": CATEGORIES},
                "kb_ids": {"type": "array", "items": {"type": "string"}},
                "urgency": {"type": "string", "enum": URGENCY},
                "confidence": {"type": "number", "description": "0 to 1: how sure you are the tip fits what the frames show."},
            },
            "required": ["should_speak", "spoken", "detail", "category", "kb_ids", "urgency", "confidence"],
            "additionalProperties": False,
        },
    },
    "required": ["screen", "playing_genji", "situation", "events", "ult_percent", "tip"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """You are a Genji coach watching an Overwatch 2 player's screen in real time. \
You receive the latest screenshot (and sometimes the one before it) plus notes on what you saw recently. \
Read the HUD and the scene, then decide whether one coaching tip would help right now.

How to read the frames:
- Genji's HUD shows Swift Strike (dash) and Deflect cooldowns bottom-right, ultimate charge bottom-center, health bottom-left.
- A killcam, a "You were eliminated" banner, or a respawn timer means the player is dead. That is the best moment to coach: explain what got them killed.
- The kill feed top-right shows eliminations. A green box of text labelled "Genji Coach" is this app's own overlay; ignore it.
- If the player is not on Genji, not in Overwatch, or in a menu, set should_speak false.

How to coach:
- Ground every tip in what the frames actually show. If you can't see a clear mistake or opportunity, set should_speak false. Silence is better than a generic tip.
- Prefer tips from the knowledge base below and list their ids in kb_ids. You may give an unlisted tip if the frames clearly call for it; leave kb_ids empty then.
- Don't repeat a tip listed under "Recently said" unless the same mistake happened again.
- urgency "now" is only for something the player can act on in the next few seconds (e.g. dash is up and an enemy is one-shot). Use "between_lives" for lessons from a death.
- Keep spoken tips short enough to hear mid-fight. Put the reasoning in detail.

Output fields: situation is one sentence on what is happening to the player; events are short notes like "eliminated by Cassidy" or "dragonblade active"; ult_percent is the HUD ultimate charge or -1 if not visible; tip.spoken is at most 14 words; tip.detail is one or two sentences of why; tip.confidence is 0 to 1.

Knowledge base (distilled from top Genji players and coaches; patch-sensitive items may be outdated):
{kb}
"""


@dataclass
class Tip:
    should_speak: bool
    spoken: str
    detail: str
    category: str
    kb_ids: list[str]
    urgency: str
    confidence: float


@dataclass
class Analysis:
    screen: str
    playing_genji: bool
    situation: str
    events: list[str]
    ult_percent: int
    tip: Tip
    usage: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict, usage: dict | None = None) -> "Analysis":
        tip = Tip(**data["tip"])
        return cls(
            screen=data["screen"] if data["screen"] in SCREENS else "unknown",
            playing_genji=bool(data["playing_genji"]),
            situation=data["situation"],
            events=list(data["events"]),
            ult_percent=int(data["ult_percent"]),
            tip=tip,
            usage=usage or {},
        )

    @property
    def in_match(self) -> bool:
        return self.screen in ("in_match_alive", "dead_or_killcam", "scoreboard")


def build_user_content(frames: list[Frame], recent_notes: list[str], recently_said: list[str]) -> list[dict]:
    """The per-frame user message: screenshot(s) plus short-term memory. Shared by both backends."""
    content: list[dict] = []
    for i, frame in enumerate(frames):
        label = "Latest frame" if i == len(frames) - 1 else "Previous frame"
        content.append({"type": "text", "text": f"{label} ({frame.label or 'live'}):"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(frame.jpeg).decode("ascii")},
        })
    notes = "\n".join(f"- {n}" for n in recent_notes[-6:]) or "- (nothing yet)"
    said = "\n".join(f"- {s}" for s in recently_said[-6:]) or "- (nothing yet)"
    content.append({"type": "text", "text": f"Recent observations:\n{notes}\n\nRecently said:\n{said}\n\nAnalyze the latest frame."})
    return content


class AnalysisError(Exception):
    pass


class PlanLimitPause(AnalysisError):
    """Raised instead of analyzing when the Claude plan's usage window is nearly used up."""

    def __init__(self, message: str, resume_at: float):
        super().__init__(message)
        self.resume_at = resume_at


class Analyzer:
    def __init__(self, kb: KnowledgeBase, model: str, effort: str, client: anthropic.Anthropic | None = None):
        self.kb = kb
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=1, timeout=45.0)
        # Stable system prompt with a cache breakpoint, so once the knowledge base grows past
        # the model's minimum cacheable size it bills at cache-read rates after the first frame.
        self.system = [{
            "type": "text",
            "text": SYSTEM_TEMPLATE.format(kb=kb.as_prompt()),
            "cache_control": {"type": "ephemeral"},
        }]

    def _supports_fallback(self) -> bool:
        return self.model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5"))

    def analyze(self, frames: list[Frame], recent_notes: list[str], recently_said: list[str]) -> Analysis:
        content = build_user_content(frames, recent_notes, recently_said)
        kwargs: dict = dict(
            model=self.model,
            max_tokens=4000,
            system=self.system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        )
        try:
            if self._supports_fallback():
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                if self.model.startswith("claude-haiku"):
                    del kwargs["output_config"]["effort"]  # Haiku 4.5 rejects effort
                response = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise SystemExit("Claude API key rejected. Set ANTHROPIC_API_KEY to a valid key.") from e
        except anthropic.RateLimitError as e:
            raise AnalysisError("rate limited; skipping this frame") from e
        except anthropic.APIStatusError as e:
            raise AnalysisError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnalysisError("network error reaching the Claude API") from e

        if response.stop_reason == "refusal":
            raise AnalysisError("model declined this frame")
        if response.stop_reason == "max_tokens":
            raise AnalysisError("analysis cut off at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise AnalysisError("no text in response")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise AnalysisError(f"invalid JSON from model: {e}") from e
        u = response.usage
        usage = {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return Analysis.from_json(data, usage)

    def review(self, prompt: str) -> str:
        """Plain-text completion, used for the end-of-session review."""
        try:
            response = self.client.messages.create(
                model=self.model, max_tokens=16000, messages=[{"role": "user", "content": prompt}])
        except anthropic.APIError as e:
            raise AnalysisError(str(e)) from e
        if response.stop_reason == "refusal":
            raise AnalysisError("the model declined")
        return "".join(b.text for b in response.content if b.type == "text").strip()
