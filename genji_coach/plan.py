"""Runs frame analysis through your own Claude Code login, so it counts against your
Claude plan's usage limits (Pro, Max, Team) instead of billing an API key.

Each frame is one headless `claude -p` call with the screenshot passed inline and the
answer constrained to the same JSON schema the API backend uses.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .analyzer import (ANALYSIS_SCHEMA, SYSTEM_TEMPLATE, Analysis, AnalysisError, PlanLimitPause,
                       build_user_content)
from .capture import Frame
from .knowledge import KnowledgeBase

log = logging.getLogger(__name__)

LOGIN_HINT = ("Claude Code isn't logged in to your Claude account. Open a terminal, run `claude`, "
              "and sign in with your Claude.ai account, then start the coach again.")
# If these are set, Claude Code bills them instead of your plan, so the plan backend drops them.
API_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def strip_descriptions(schema: dict) -> dict:
    """The schema goes on the command line; keep it short and free of shell-sensitive text.
    The field guidance lives in the system prompt instead."""
    out = {k: v for k, v in schema.items() if k != "description"}
    if "properties" in out:
        out["properties"] = {k: strip_descriptions(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = strip_descriptions(out["items"])
    return out


PLAN_SCHEMA = json.dumps(strip_descriptions(ANALYSIS_SCHEMA), separators=(",", ":"))


def find_claude(configured: str = "") -> str:
    path = configured or shutil.which("claude")
    if not path:
        raise SystemExit("Claude Code isn't installed. Install it from https://claude.com/claude-code, "
                         "run `claude` once to sign in, then start the coach again.")
    return path


class ClaudePlanAnalyzer:
    def __init__(self, kb: KnowledgeBase, model: str, effort: str, pause_at: float = 0.9,
                 claude_path: str = "", run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.kb = kb
        self.model = model
        self.effort = effort
        self.pause_at = pause_at
        self.claude = find_claude(claude_path)
        self._run = run
        # A private working directory keeps the user's CLAUDE.md files and project settings out of the call.
        self._workdir = Path(tempfile.mkdtemp(prefix="genji_coach_"))
        self._system_file = self._workdir / "system.txt"
        self._system_file.write_text(SYSTEM_TEMPLATE.format(kb=kb.as_prompt()), encoding="utf-8")
        self._env = {k: v for k, v in os.environ.items() if k not in API_ENV_VARS}
        self.limits: dict = {}

    def _base_args(self) -> list[str]:
        return [
            self.claude, "-p",
            "--model", self.model,
            "--effort", self.effort,
            "--system-prompt-file", str(self._system_file),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
        ]

    def _call(self, args: list[str], stdin: str, timeout: float) -> list[dict]:
        try:
            proc = self._run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                             timeout=timeout, cwd=self._workdir, env=self._env)
        except subprocess.TimeoutExpired as e:
            raise AnalysisError(f"Claude Code took longer than {timeout:g}s") from e
        events: list[dict] = []
        try:  # --output-format json prints one document
            whole = json.loads(proc.stdout)
            events = whole if isinstance(whole, list) else [whole]
        except json.JSONDecodeError:
            pass
        for line in ([] if events else proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for e in events:
            if e.get("type") == "rate_limit_event":
                self.limits = e.get("rate_limit_info") or {}
        if not any(e.get("type") == "result" for e in events):
            err = (proc.stderr or proc.stdout or "").strip()
            if any(s in err.lower() for s in ("not logged in", "please run /login", "invalid api key", "authentication")):
                raise SystemExit(LOGIN_HINT)
            raise AnalysisError(f"Claude Code exited with code {proc.returncode}: {err[:300]}")
        return events

    def _check_result(self, result: dict) -> None:
        if not result.get("is_error"):
            return
        text = str(result.get("result") or result.get("errors") or result.get("subtype"))
        low = text.lower()
        if "login" in low or "authentication" in low or "api key" in low:
            raise SystemExit(LOGIN_HINT)
        if "limit" in low:
            raise PlanLimitPause(f"Claude plan limit reached: {text[:200]}", self._reset_time())
        raise AnalysisError(f"Claude Code error: {text[:300]}")

    def _reset_time(self) -> float:
        return float(self.limits.get("resetsAt") or time.time() + 15 * 60)

    def five_hour_usage(self) -> float | None:
        window = (self.limits.get("unifiedWindows") or {}).get("five_hour") or {}
        return window.get("utilization")

    def _guard_limits(self) -> None:
        if not self.limits or time.time() >= self._reset_time():
            return
        usage = self.five_hour_usage()
        if self.limits.get("status") == "rejected" or (usage is not None and usage >= self.pause_at):
            shown = f"{usage:.0%}" if usage is not None else "the limit"
            raise PlanLimitPause(
                f"Claude plan is at {shown} of its 5-hour window; pausing so you keep some for yourself",
                self._reset_time())

    def analyze(self, frames: list[Frame], recent_notes: list[str], recently_said: list[str]) -> Analysis:
        self._guard_limits()
        message = {"type": "user", "message": {"role": "user",
                                               "content": build_user_content(frames, recent_notes, recently_said)}}
        args = self._base_args() + ["--input-format", "stream-json", "--output-format", "stream-json",
                                    "--verbose", "--json-schema", PLAN_SCHEMA]
        events = self._call(args, json.dumps(message) + "\n", timeout=90)
        result = next(e for e in reversed(events) if e.get("type") == "result")
        self._check_result(result)
        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except json.JSONDecodeError as e:
                raise AnalysisError("Claude Code returned no structured output") from e
        u = result.get("usage") or {}
        usage = {
            "input": u.get("input_tokens", 0),
            "output": u.get("output_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0),
            # What the same calls would cost on the API. Not billed on a plan; shown for scale.
            "api_equivalent_micro_usd": round((result.get("total_cost_usd") or 0) * 1_000_000),
        }
        try:
            return Analysis.from_json(data, usage)
        except (KeyError, TypeError, ValueError) as e:
            raise AnalysisError(f"unexpected analysis shape: {e}") from e

    def review(self, prompt: str) -> str:
        args = self._base_args()
        args[args.index("--effort") + 1] = "medium"
        args += ["--output-format", "json"]
        events = self._call(args, prompt, timeout=300)
        result = next(e for e in reversed(events) if e.get("type") == "result")
        self._check_result(result)
        return str(result.get("result") or "").strip()

    def complete_json(self, prompt: str, schema: dict, effort: str = "medium") -> dict:
        """One-off structured answer to a text prompt (used by tools/learn_from_youtube.py)."""
        args = self._base_args()
        args[args.index("--effort") + 1] = effort
        args += ["--output-format", "json", "--json-schema",
                 json.dumps(strip_descriptions(schema), separators=(",", ":"))]
        events = self._call(args, prompt, timeout=600)
        result = next(e for e in reversed(events) if e.get("type") == "result")
        self._check_result(result)
        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except json.JSONDecodeError as e:
                raise AnalysisError("Claude Code returned no structured output") from e
        return data
