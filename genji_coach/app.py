"""Entry point: python -m genji_coach [--video match.mp4 | --frames folder] [options]"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from .analyzer import Analysis, AnalysisError, Analyzer, PlanLimitPause
from .capture import Frame, ScreenSource, folder_frames, video_frames
from .coach import Coach, Delivery
from .config import Config
from .knowledge import KnowledgeBase
from .session import Session

log = logging.getLogger("genji_coach")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="genji_coach", description="Watches your Overwatch screen and coaches your Genji.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--video", type=Path, help="review a recorded match instead of watching live")
    src.add_argument("--frames", type=Path, help="review a folder of screenshots")
    p.add_argument("--every", type=float, default=5.0, help="seconds between sampled frames with --video (default 5)")
    p.add_argument("--config", type=Path, help="path to config.toml")
    p.add_argument("--backend", choices=["plan", "api"],
                   help="plan = your Claude subscription through Claude Code (default); api = ANTHROPIC_API_KEY")
    p.add_argument("--no-voice", action="store_true")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--no-report", action="store_true", help="skip the AI-written session review")
    p.add_argument("-v", "--verbose", action="store_true", help="print every frame's analysis")
    return p.parse_args(argv)


class Runner:
    def __init__(self, cfg: Config, kb: KnowledgeBase, analyzer: Analyzer, session: Session,
                 on_tip: Callable[[Delivery], None], verbose: bool = False):
        self.cfg = cfg
        self.kb = kb
        self.analyzer = analyzer
        self.session = session
        self.coach = Coach(cfg.min_seconds_between_tips, cfg.category_cooldown_seconds, cfg.live_tips)
        self.on_tip = on_tip
        self.verbose = verbose
        self.notes: list[str] = []
        self.stop = threading.Event()
        self.paused_until = float("-inf")
        self._usage_shown = -1

    def _report_plan_usage(self) -> None:
        usage_fn = getattr(self.analyzer, "five_hour_usage", None)
        usage = usage_fn() if usage_fn else None
        if usage is None:
            return
        step = int(usage * 10)  # announce each new 10% of the 5-hour window
        if step > self._usage_shown:
            self._usage_shown = step
            print(f"  (Claude plan: {usage:.0%} of your 5-hour usage window used)")

    def step(self, frame: Frame, now: float) -> Analysis | None:
        if now < self.paused_until:
            return None
        try:
            analysis = self.analyzer.analyze([frame], self.notes, self.coach.said)
        except PlanLimitPause as e:
            self.paused_until = now + max(60.0, e.resume_at - time.time())
            log.warning("%s. Resuming at %s.", e, time.strftime("%H:%M", time.localtime(e.resume_at)))
            return None
        except AnalysisError as e:
            log.warning("Skipped a frame: %s", e)
            return None
        self._report_plan_usage()
        self.notes.append(f"{frame.label}: {analysis.screen}; {analysis.situation}"
                          + (f" ({'; '.join(analysis.events)})" if analysis.events else ""))
        self.notes = self.notes[-12:]
        delivery = self.coach.consider(analysis, now)
        self.session.record(frame, analysis, delivery)
        if self.verbose:
            print(f"[{frame.label}] {analysis.screen} | {analysis.situation}")
        if delivery:
            self.on_tip(delivery)
        return analysis

    def run_live(self, source: ScreenSource) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            frame = source.grab()
            frame.label = time.strftime("%H:%M:%S")
            analysis = self.step(frame, started)
            busy = analysis is not None and analysis.in_match
            wait = self.cfg.interval_seconds if busy else self.cfg.idle_interval_seconds
            self.stop.wait(max(0.0, wait - (time.monotonic() - started)))

    def run_replay(self, frames: Iterable[Frame]) -> None:
        for frame in frames:
            if self.stop.is_set():
                break
            self.step(frame, frame.timestamp)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("comtypes").setLevel(logging.WARNING)  # Windows voice setup is chatty on first run
    cfg = Config.load(args.config)
    kb = KnowledgeBase.load(cfg.resolve(cfg.knowledge_dir))
    if args.backend:
        cfg.backend = args.backend
    if cfg.backend == "plan":
        from .plan import ClaudePlanAnalyzer
        analyzer = ClaudePlanAnalyzer(kb, cfg.plan_model, cfg.effort, cfg.plan_pause_at, cfg.claude_path)
        model_name = f"{cfg.plan_model} on your Claude plan"
    else:
        analyzer = Analyzer(kb, cfg.model, cfg.effort)
        model_name = f"{cfg.model} via API key"
    session = Session(cfg.resolve(cfg.sessions_dir), cfg.save_frames, cfg.model)

    replay = args.video or args.frames
    use_voice = cfg.voice and not args.no_voice and not replay
    use_overlay = cfg.overlay and not args.no_overlay and not replay

    voice = overlay = None
    if use_voice:
        from .voice import Voice
        voice = Voice(cfg.voice_rate)
    if use_overlay:
        from .overlay import Overlay
        overlay = Overlay(cfg.overlay_position, cfg.overlay_seconds)

    def on_tip(d: Delivery) -> None:
        credit = "; ".join(filter(None, (kb.credit(k) for k in d.kb_ids)))
        print(f"  >> {d.spoken}\n     {d.detail}" + (f"\n     (source: {credit})" if credit else ""))
        if voice:
            voice.say(d.spoken)
        if overlay:
            overlay.show(d.spoken, d.detail, credit)

    runner = Runner(cfg, kb, analyzer, session, on_tip, args.verbose)
    print(f"Genji Coach: {len(kb.tips)} tips from {len(kb.sources)} sources, using {model_name}.")

    try:
        if args.video:
            print(f"Reviewing {args.video}, one frame every {args.every:g}s...")
            runner.run_replay(video_frames(args.video, cfg.frame_width, args.every))
        elif args.frames:
            print(f"Reviewing screenshots in {args.frames}...")
            runner.run_replay(folder_frames(args.frames, cfg.frame_width))
        else:
            source = ScreenSource(cfg.monitor, cfg.frame_width)
            print(f"Watching monitor {cfg.monitor} every {cfg.interval_seconds:g}s. Press Ctrl+C to stop.")
            if overlay:
                worker = threading.Thread(target=runner.run_live, args=(source,), daemon=True)
                worker.start()
                # Tk owns the main thread, so turn Ctrl+C into a stop request it polls for.
                signal.signal(signal.SIGINT, lambda *_: runner.stop.set())
                overlay.run(runner.stop.is_set)
            else:
                runner.run_live(source)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop.set()
        if voice:
            voice.close()
        session.close()

    if session.entries:
        print("Writing session review...")
        path = session.write_report(kb, None if args.no_report else analyzer.review,
                                    on_plan=cfg.backend == "plan")
        print(f"Review saved to {path}")
    return 0
