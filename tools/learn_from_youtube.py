"""Teach the coach from a Genji YouTube video: pull its transcript, have Claude extract
concrete tips, and add them (with timestamped citations) to knowledge/learned.json.

    python tools/learn_from_youtube.py https://www.youtube.com/watch?v=VIDEO_ID --creator "KarQ"
    python tools/learn_from_youtube.py --transcript saved.txt --url https://... --creator "Necros" --title "..."

Needs `pip install youtube-transcript-api` unless you pass --transcript. Runs on your Claude plan
through Claude Code by default; pass --backend api to use ANTHROPIC_API_KEY instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from genji_coach.analyzer import CATEGORIES  # noqa: E402
from genji_coach.knowledge import KnowledgeBase  # noqa: E402

LEARNED = ROOT / "knowledge" / "learned.json"

SCHEMA = {
    "type": "object",
    "properties": {
        "tips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "short_snake_case id"},
                    "category": {"type": "string", "enum": [c for c in CATEGORIES if c != "none"]},
                    "situation": {"type": "string", "description": "When this applies, phrased so it can be recognized from a screenshot."},
                    "tip": {"type": "string", "description": "The advice, at most 20 words, imperative."},
                    "timestamps": {"type": "array", "items": {"type": "string"}, "description": "mm:ss where the creator says it"},
                },
                "required": ["slug", "category", "situation", "tip", "timestamps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tips"],
    "additionalProperties": False,
}

PROMPT = """This is the transcript of a YouTube video by {creator} about playing Genji in Overwatch 2.
Extract every concrete, actionable Genji tip the creator actually states: positioning, dash and deflect use, \
Dragonblade, mechanics and combos, matchups, survival, ult economy.

Rules:
- Only include advice that is in the transcript. Do not add your own.
- Skip banter, generic motivation, and anything not about playing Genji.
- Skip tips that duplicate the existing knowledge base below unless the video adds a meaningfully new detail.
- Give the mm:ss timestamp(s) where each tip is said.

Existing knowledge base:
{kb}

Transcript (each line starts with [mm:ss]):
{transcript}
"""


def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url)
    if not m:
        raise SystemExit(f"Couldn't find a video id in {url}")
    return m.group(1)


def fetch_transcript(vid: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        raise SystemExit("Install the transcript fetcher first: pip install youtube-transcript-api\n"
                         "Or save the transcript yourself and pass --transcript FILE.")
    if hasattr(YouTubeTranscriptApi, "fetch"):  # 1.x
        snippets = [(s.start, s.text) for s in YouTubeTranscriptApi().fetch(vid)]
    else:  # 0.x
        snippets = [(s["start"], s["text"]) for s in YouTubeTranscriptApi.get_transcript(vid)]
    return "\n".join(f"[{int(t // 60):02d}:{int(t % 60):02d}] {text}" for t, text in snippets)


def extract_with_api(prompt: str, model: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason in ("refusal", "max_tokens"):
        raise SystemExit(f"Extraction stopped early ({response.stop_reason}); nothing saved.")
    return json.loads(next(b.text for b in response.content if b.type == "text"))["tips"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", help="YouTube video URL")
    p.add_argument("--url", dest="url_opt", help="video URL to cite when using --transcript")
    p.add_argument("--transcript", type=Path, help="use a saved transcript file instead of fetching")
    p.add_argument("--creator", required=True, help="who made the video, e.g. KarQ")
    p.add_argument("--title", default="", help="video title for the citation")
    p.add_argument("--backend", choices=["plan", "api"], default="plan")
    p.add_argument("--model", help="default: sonnet on the plan backend, claude-opus-5-5 on the API")
    args = p.parse_args()

    url = args.url or args.url_opt
    if not url:
        p.error("give a YouTube URL")
    vid = video_id(url)
    transcript = args.transcript.read_text(encoding="utf-8") if args.transcript else fetch_transcript(vid)
    kb = KnowledgeBase.load(ROOT / "knowledge")

    prompt = PROMPT.format(creator=args.creator, kb=kb.as_prompt(), transcript=transcript)
    print(f"Extracting tips from {len(transcript):,} characters of transcript...")
    if args.backend == "plan":
        from genji_coach.plan import ClaudePlanAnalyzer
        tips = ClaudePlanAnalyzer(kb, args.model or "sonnet", "medium").complete_json(prompt, SCHEMA)["tips"]
    else:
        tips = extract_with_api(prompt, args.model or "claude-opus-5-5")

    data = json.loads(LEARNED.read_text(encoding="utf-8")) if LEARNED.exists() else {"sources": [], "tips": []}
    source_id = f"yt_{vid}"
    data["sources"] = [s for s in data["sources"] if s["id"] != source_id] + [{
        "id": source_id, "creator": args.creator, "title": args.title or f"YouTube video {vid}",
        "url": f"https://www.youtube.com/watch?v={vid}", "kind": "YouTube transcript",
        "note": "Tips extracted from the video transcript by tools/learn_from_youtube.py.",
    }]
    data["tips"] = [t for t in data["tips"] if source_id not in t["sources"]]
    for t in tips:
        data["tips"].append({
            "id": f"{vid}_{t['slug']}"[:64], "category": t["category"], "situation": t["situation"],
            "tip": t["tip"], "sources": [source_id], "timestamps": t["timestamps"],
        })
        print(f"  + [{', '.join(t['timestamps'])}] {t['tip']}")
    LEARNED.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(tips)} tips from {args.creator} to {LEARNED.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
