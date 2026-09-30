import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from genji_coach.analyzer import ANALYSIS_SCHEMA, Analysis, AnalysisError, Analyzer  # noqa: E402
from genji_coach.app import Runner  # noqa: E402
from genji_coach.capture import folder_frames  # noqa: E402
from genji_coach.coach import Coach  # noqa: E402
from genji_coach.config import Config  # noqa: E402
from genji_coach.knowledge import KnowledgeBase  # noqa: E402
from genji_coach.session import Session  # noqa: E402

KB = KnowledgeBase.load(ROOT / "knowledge")


def analysis(screen="in_match_alive", speak=True, urgency="now", category="dash",
             kb_ids=("dash_save_escape",), confidence=0.9, genji=True):
    return Analysis.from_json({
        "screen": screen, "playing_genji": genji, "situation": "s", "events": [], "ult_percent": 40,
        "tip": {"should_speak": speak, "spoken": f"{category} tip", "detail": "d", "category": category,
                "kb_ids": list(kb_ids), "urgency": urgency, "confidence": confidence},
    })


def test_kb_loads_and_every_tip_is_cited():
    assert len(KB.tips) >= 25
    for tip in KB.tips.values():
        assert tip.sources and all(s in KB.sources for s in tip.sources)
    assert "Necros" in KB.credit("rein_shatter")


def test_kb_prompt_is_deterministic():
    assert KB.as_prompt() == KnowledgeBase.load(ROOT / "knowledge").as_prompt()


def test_coach_speaks_urgent_tip_then_throttles():
    c = Coach(min_gap=20, category_cooldown=90)
    assert c.consider(analysis(), now=0) is not None
    assert c.consider(analysis(category="deflect", kb_ids=()), now=5) is None  # global gap
    assert c.consider(analysis(kb_ids=()), now=30) is None  # dash category cooldown
    assert c.consider(analysis(category="deflect", kb_ids=()), now=30) is not None


def test_coach_holds_lessons_until_death():
    c = Coach(min_gap=20, category_cooldown=90)
    lesson = analysis(urgency="between_lives", category="positioning", kb_ids=("off_angle",))
    assert c.consider(lesson, now=0) is None
    quiet = analysis(screen="dead_or_killcam", speak=False)
    d = c.consider(quiet, now=10)
    assert d is not None and d.category == "positioning"


def test_coach_ignores_low_confidence_and_other_heroes():
    c = Coach(min_gap=0, category_cooldown=0)
    assert c.consider(analysis(confidence=0.3), now=0) is None
    assert c.consider(analysis(genji=False), now=1) is None
    assert c.consider(analysis(screen="menu_or_lobby"), now=2) is None


def test_live_tips_off_suppresses_urgent():
    c = Coach(min_gap=0, category_cooldown=0, live_tips=False)
    assert c.consider(analysis(), now=0) is None


class FakeClient:
    """Stands in for anthropic.Anthropic and records the request."""

    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = []
        self.payload = payload
        self.stop_reason = stop_reason
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=1500, output_tokens=120,
                                  cache_read_input_tokens=3000, cache_creation_input_tokens=0),
        )


PAYLOAD = {
    "screen": "dead_or_killcam", "playing_genji": True, "situation": "Killed by Cassidy after dashing in.",
    "events": ["eliminated by Cassidy"], "ult_percent": 62,
    "tip": {"should_speak": True, "spoken": "Keep dash to escape unless the combo kills.",
            "detail": "You dashed onto a full-health target and had no way out.", "category": "dash",
            "kb_ids": ["dash_save_escape"], "urgency": "between_lives", "confidence": 0.85},
}


@pytest.fixture
def frames(tmp_path):
    folder = tmp_path / "shots"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (1920, 1080), (i * 40, 30, 30)).save(folder / f"{i}.png")
    return list(folder_frames(folder, 1280))


def test_frames_are_downscaled_jpeg(frames):
    assert frames[0].jpeg[:2] == b"\xff\xd8"
    from io import BytesIO
    assert Image.open(BytesIO(frames[0].jpeg)).size == (1280, 720)


def test_analyzer_request_shape(frames):
    client = FakeClient(PAYLOAD)
    a = Analyzer(KB, "claude-opus-5-5", "low", client=client).analyze(frames[:1], ["note"], ["said"])
    call = client.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["schema"] is ANALYSIS_SCHEMA
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any(b["type"] == "image" for b in call["messages"][0]["content"])
    assert a.screen == "dead_or_killcam" and a.tip.kb_ids == ["dash_save_escape"]


def test_haiku_drops_effort(frames):
    client = FakeClient(PAYLOAD)
    Analyzer(KB, "claude-haiku-4-5", "low", client=client).analyze(frames[:1], [], [])
    assert "effort" not in client.calls[0]["output_config"] and "fallbacks" not in client.calls[0]


def test_refusal_is_an_analysis_error(frames):
    with pytest.raises(AnalysisError):
        Analyzer(KB, "claude-opus-5-5", "low", client=FakeClient(PAYLOAD, "refusal")).analyze(frames[:1], [], [])


def test_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(ANALYSIS_SCHEMA)


def test_replay_end_to_end(frames, tmp_path):
    cfg = Config(sessions_dir=str(tmp_path / "sessions"))
    client = FakeClient(PAYLOAD)
    session = Session(cfg.resolve(cfg.sessions_dir), False, cfg.model)
    heard = []
    runner = Runner(cfg, KB, Analyzer(KB, cfg.model, cfg.effort, client=client), session, heard.append)
    runner.run_replay(f.__class__(f.jpeg, i * 30.0, f"0:{i * 30:02d}") for i, f in enumerate(frames))
    session.close()
    assert len(session.entries) == 3
    assert len(heard) == 1  # same tip repeated; coach says it once
    report = session.write_report(KB, None).read_text()
    assert "Keep dash to escape" in report and "Necros" not in report  # dash_save_escape cites forum + community
    assert "Estimated API cost" in report
    assert (session.dir / "log.jsonl").read_text().count("\n") == 3


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('modle = "x"\n')
    with pytest.raises(ValueError):
        Config.load(p)
    p.write_text('interval_seconds = 4.0\nvoice = false\n')
    cfg = Config.load(p)
    assert cfg.interval_seconds == 4.0 and cfg.voice is False
