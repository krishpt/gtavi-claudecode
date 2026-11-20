---
name: autopilot
description: Run the hands-off GTA 6 clipping loop - check permitted channels for new VODs, clip them, queue and post to TikTok on a schedule. Use when the user says "autopilot", "run it automatically", "post for me", "set up the schedule", or asks what was posted.
---

# Autopilot

Scripts live in `${CLAUDE_PLUGIN_ROOT}/autopilot/` (`autoclip.py`, `clipper.py`, `tiktok.py`). Run them from that folder with the repo's `.venv/bin/python`.

## First-time setup

0. ffmpeg must include libass to burn captions. On macOS Homebrew's plain `ffmpeg` doesn't: `brew install ffmpeg-full && brew link --overwrite ffmpeg-full`. Check with `ffmpeg -filters | grep ass`.

1. Copy `config.example.json` to `config.json`. For each source, ask the user for the channel URL and **how they have
   permission** (the campaign link they joined, or "my own channel"). Leave `permission` empty if they have none. The
   loop then skips that channel. Never fill it in for them.
2. TikTok: walk them through creating a developer app (Login Kit + Content Posting API, scopes `video.upload`,
   `video.publish`), set `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`, then run
   `python tiktok.py auth`. Put the three variables in `autopilot/tiktok.env` (KEY=value lines) so the scheduled job sees them. Until TikTok audits the app, use `posting.mode: "draft"`.
3. Run `python autoclip.py once --dry-run` and show them the queued clips and captions.
4. Schedule it: on macOS `bash autopilot/install_schedule.sh` (launchd, every 30 min; `--remove` to stop). Linux: cron. Windows: Task Scheduler.

To find new campaign creators, use `campaign-finder`. To find trending streams worth asking permission for, use `stream-finder` and `trend-radar`.

## Day to day

- "What got posted?" → `python autoclip.py status` and the tail of `autoclip.log`.
- Campaign rules change → update that source's `notes`. The ranker reads them.
- Keep `max_posts_per_day` at 2-6. More than that reads as spam to TikTok and drops reach.

## Rules

- Only clip channels with a recorded permission. No leaks, no pre-release footage, no reposts without edits.
- Every clip gets a hook, captions and credit to the creator in the post text.
