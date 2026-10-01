import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from genji_coach.analyzer import AnalysisError, PlanLimitPause  # noqa: E402
from genji_coach.capture import folder_frames  # noqa: E402
from genji_coach.knowledge import KnowledgeBase  # noqa: E402
from genji_coach.plan import PLAN_SCHEMA, ClaudePlanAnalyzer  # noqa: E402

KB = KnowledgeBase.load(ROOT / "knowledge")
ANALYSIS = {
    "screen": "in_match_alive", "playing_genji": True, "situation": "Poking the enemy tank.",
    "events": [], "ult_percent": 30,
    "tip": {"should_speak": False, "spoken": "", "detail": "", "category": "none",
            "kb_ids": [], "urgency": "now", "confidence": 0.2},
}


def fake_run(events, returncode=0, stderr="", calls=None):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        out = "\n".join(json.dumps(e) for e in events)
        return subprocess.CompletedProcess(args, returncode, out, stderr)
    return run


def limits(utilization, status="allowed"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": time.time() + 3600,
        "unifiedWindows": {"five_hour": {"utilization": utilization}}}}


def result(structured=ANALYSIS, is_error=False, text=None):
    return {"type": "result", "subtype": "success", "is_error": is_error, "structured_output": structured,
            "result": text if text is not None else json.dumps(structured), "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 20, "output_tokens": 150, "cache_read_input_tokens": 5000}}


@pytest.fixture
def frame(tmp_path):
    Image.new("RGB", (1920, 1080)).save(tmp_path / "a.png")
    return next(folder_frames(tmp_path, 1280))


def test_schema_is_compact_and_shell_safe():
    assert "description" not in PLAN_SCHEMA
    assert not set(PLAN_SCHEMA) & set("<>|&^%!'")


def test_analyze_sends_image_and_parses_result(frame, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    calls = []
    a = ClaudePlanAnalyzer(KB, "sonnet", "low", claude_path="claude",
                           run=fake_run([limits(0.12), result()], calls=calls))
    analysis = a.analyze([frame], [], [])
    args, kwargs = calls[0]
    assert args[args.index("--model") + 1] == "sonnet" and "--json-schema" in args
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]  # the plan login must be used, not an API key
    sent = json.loads(kwargs["input"])
    assert any(b["type"] == "image" for b in sent["message"]["content"])
    assert analysis.situation == "Poking the enemy tank."
    assert analysis.usage["api_equivalent_micro_usd"] == 12300
    assert a.five_hour_usage() == 0.12


def test_pauses_near_plan_limit(frame):
    a = ClaudePlanAnalyzer(KB, "sonnet", "low", pause_at=0.9, claude_path="claude",
                           run=fake_run([limits(0.93), result()]))
    a.analyze([frame], [], [])  # this call reports 93%
    with pytest.raises(PlanLimitPause) as e:
        a.analyze([frame], [], [])
    assert e.value.resume_at > time.time()


def test_login_problem_exits_with_hint(frame):
    a = ClaudePlanAnalyzer(KB, "sonnet", "low", claude_path="claude",
                           run=fake_run([], returncode=1, stderr="Not logged in. Please run /login"))
    with pytest.raises(SystemExit, match="sign in"):
        a.analyze([frame], [], [])


def test_error_result_is_analysis_error(frame):
    a = ClaudePlanAnalyzer(KB, "sonnet", "low", claude_path="claude",
                           run=fake_run([result(None, is_error=True, text="Overloaded")]))
    with pytest.raises(AnalysisError):
        a.analyze([frame], [], [])


def test_review_uses_text_output():
    calls = []
    run = fake_run([{"type": "result", "is_error": False, "result": "## Top 3\n- aim"}], calls=calls)
    a = ClaudePlanAnalyzer(KB, "sonnet", "low", claude_path="claude", run=run)
    assert a.review("prompt") == "## Top 3\n- aim"
    args, kwargs = calls[0]
    assert kwargs["input"] == "prompt" and "--json-schema" not in args
