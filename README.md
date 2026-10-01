# Genji Coach

A desktop companion that watches your Overwatch 2 screen while you play Genji and coaches you, out loud and in a small on-screen box. It runs on your Claude subscription (through Claude Code, no API key needed) and uses Claude's vision to read each frame (HUD, kill feed, killcam) and picks tips from a knowledge base built from top Genji players and coaches. When you finish, it writes a session review with your top three things to work on.

## What it does

- **Live coaching.** Every 8 seconds it grabs a screenshot and asks Claude what's happening. Short call-outs ("dash is up, finish the Tracer") play mid-fight. Lessons ("you dashed in with no escape") wait until you're dead or on the killcam, so it doesn't talk over your fights.
- **Doesn't spam.** Tips are spaced at least 20 seconds apart, the same category stays quiet for 90 seconds, and low-confidence or generic tips get dropped.
- **Cites its sources.** Each tip names who it came from (for example "Necros (featured by KarQ)").
- **Replay review.** Point it at a recorded match (`--video`) or a folder of screenshots (`--frames`) to get the same review without playing live.
- **Learns from YouTube.** `tools/learn_from_youtube.py` pulls a Genji video's transcript, has Claude extract the concrete tips with timestamps, and adds them to the knowledge base.
- **Session reports** go to `sessions/<date-time>/report.md`, with a full per-frame log in `log.jsonl`.

## Staying within Blizzard's rules

The app only looks at screenshots, the same pixels you see. It never reads game memory, injects anything, or sends input to the game. Tips are spoken or shown in a separate window. Tools like this (screen capture plus advice) are in the same category as VOD-review and coaching apps. Don't modify it to automate input.

## Setup (Windows)

1. Install Python 3.11 or newer from python.org (tick "Add to PATH").
2. Install Claude Code from https://claude.com/claude-code, open a terminal, run `claude`, and sign in with your Claude account (Pro, Max or Team). That login is what the coach uses.
3. In Overwatch, set **Display Mode to Borderless Windowed**. Exclusive fullscreen hides the overlay and can make screen capture return black.
4. Double-click `run.bat`. The first run installs dependencies.

Manual equivalent:

```
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m genji_coach
```

Stop with Ctrl+C in the console window. The review is written when you stop.

## Usage

```
python -m genji_coach                       # coach live
python -m genji_coach --no-voice            # overlay only
python -m genji_coach --no-overlay          # voice only (works in exclusive fullscreen)
python -m genji_coach --video match.mp4     # review a recording (pip install opencv-python)
python -m genji_coach --frames screenshots/ # review a folder of screenshots
python -m genji_coach -v                    # print what it sees on every frame
python -m genji_coach --backend api         # use ANTHROPIC_API_KEY instead of your plan
```

Copy `config.example.toml` to `config.toml` to change the model, frame interval, voice speed, overlay corner, throttling, or to turn off mid-fight tips (`live_tips = false`).

## Plan usage and cost

By default every analyzed frame is one headless Claude Code call on your own login, so it uses your Claude plan's usage limits instead of costing money per call. Things to know:

- **It uses a lot of your limits.** One frame every 8 seconds is roughly 400 calls an hour while you're in a match (menus back off to every 20 seconds). On Pro that can use up a 5-hour window within one session; Max lasts much longer. The same limits are shared with your normal Claude chats and Claude Code.
- **It watches your usage.** The console shows how much of your 5-hour window is used, and coaching pauses at 90% (`plan_pause_at`) until the window resets, so you're never locked out of Claude.
- **Stretch it further** with `plan_model = "haiku"` (lightest on limits), a longer `interval_seconds`, or `live_tips = false`.
- **Speed:** each frame takes about 5 seconds on Sonnet, including starting Claude Code. That's fine for death reviews and between-fight lessons, but split-second call-outs will lag slightly.
- If `ANTHROPIC_API_KEY` is set on your PC, the coach hides it from Claude Code so your plan is used, not the key.

Session reports show what the same calls would have cost on the API, for scale.

**API key instead (`backend = "api"`).** Pay per token with no usage windows. Rough estimates at the default interval:

| Model | Per frame | Per hour of play |
|---|---|---|
| `claude-opus-5-5` (most accurate) | about $0.02 | about $8 to $10 |
| `claude-haiku-4-5` | about $0.005 | about $2 to $3 |

## Teaching it from more videos

```
pip install youtube-transcript-api
python tools/learn_from_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID" --creator "KarQ" --title "Genji guide"
```

New tips land in `knowledge/learned.json` with the video link and timestamps, and are used from the next session on. If a transcript can't be fetched, save it as text and pass `--transcript file.txt --url ... --creator ...`.

Good candidates: KarQ's and Spilo's Genji VOD reviews, Necros and Shadder2k educational streams, and the "Unranked to GM: Genji Only" series. The reference VODs already listed in the knowledge base haven't been mined yet.

## Knowledge base

`knowledge/genji_kb.json` holds 29 hand-curated tips across positioning, dash, deflect, Dragonblade, mechanics, matchups, survival and ult economy. Sources:

- Necros' hero-by-hero matchup guide, from a KarQ video, via [Dexerto](https://www.dexerto.com/overwatch/elite-genji-guide-reveals-how-overwatch-players-can-win-against-any-hero-1401489/)
- [In-Depth Genji Guide](https://us.forums.blizzard.com/en/overwatch/t/in-depth-genji-guide/326434) (Blizzard forums)
- [Advanced Genji Tips And Tricks](https://www.ibtimes.com/overwatch-2-advanced-genji-tips-tricks-3630063) (IBTimes)
- Widely taught fundamentals, marked `community`
- Reference VODs to mine next: [Shadder2k rank 1 gameplay](https://www.youtube.com/watch?v=PsbAmL_GM0s), [Shadder2k vs Necros](https://www.youtube.com/watch?v=n9Il0M1k7KQ), [Unranked to GM: Genji Only](https://www.youtube.com/playlist?list=PLGIR6it42MtlzfeiniT_odGjxt6psAFTo)

Tips marked `patch_sensitive` describe mechanics Blizzard has tuned before (for example what resets dash). Re-check them after balance patches.

## How it works

```
screen (mss) -> downscaled JPEG -> Claude vision, structured JSON
    (claude -p on your plan login, or the Claude API with a key)
    -> screen state, events, ult %, suggested tip
    -> Coach: confidence, timing and cooldown filters
    -> voice (pyttsx3) + overlay (Tk, click-through) + session log
    -> end of session: Claude writes the review from the log
```

| File | Role |
|---|---|
| `genji_coach/capture.py` | Screen, screenshot-folder and video frame sources |
| `genji_coach/analyzer.py` | Prompt, JSON schema, API backend |
| `genji_coach/plan.py` | Claude plan backend (headless Claude Code) and usage-limit pausing |
| `genji_coach/coach.py` | Decides which tips are said and when |
| `genji_coach/voice.py`, `overlay.py` | Output |
| `genji_coach/session.py` | Log, cost estimate, end-of-session review |
| `tools/learn_from_youtube.py` | Adds tips from video transcripts |

Run the tests with `python -m pytest tests`.
