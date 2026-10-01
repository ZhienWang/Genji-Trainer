"""Settings, loaded from an optional config.toml over built-in defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    backend: str = "plan"            # "plan" = your Claude subscription via Claude Code; "api" = API key
    plan_model: str = "sonnet"       # Claude Code model alias used with backend "plan"
    plan_pause_at: float = 0.9       # pause when the plan's 5-hour window is this full
    claude_path: str = ""            # path to the claude executable if it isn't on PATH
    model: str = "claude-opus-5-5"   # API model id used with backend "api"
    effort: str = "low"
    interval_seconds: float = 8.0
    idle_interval_seconds: float = 20.0
    monitor: int = 1
    frame_width: int = 1280
    voice: bool = True
    voice_rate: int = 185
    overlay: bool = True
    overlay_position: str = "top-right"
    overlay_seconds: float = 7.0
    min_seconds_between_tips: float = 20.0
    category_cooldown_seconds: float = 90.0
    live_tips: bool = True
    sessions_dir: str = "sessions"
    save_frames: bool = False
    knowledge_dir: str = "knowledge"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or ROOT / "config.toml"
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys in {path}: {', '.join(sorted(unknown))}")
        cfg = cls(**data)
        if cfg.backend not in ("plan", "api"):
            raise ValueError(f'backend must be "plan" or "api", not "{cfg.backend}"')
        return cfg

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else ROOT / p
