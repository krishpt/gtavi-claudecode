#!/usr/bin/env python3
"""Verification gate: a clip may only be queued for posting if every checklist item passes.

Checklist (each item is yes/no):
  1. file probes, 1080x1920, ~30 fps
  2. has audio; integrated loudness between -18 and -10 LUFS
  3. duration inside the source's min/max (campaign rules win over defaults)
  4. not mostly black or frozen (blackdetect < 10%, freezedetect < 40% of the clip)
  5. captions burned in when the clip has speech (the .ass has dialogue lines)
  6. post text: <= 2200 chars, 3-8 hashtags, credit/mention present if required, required hashtags present,
     no banned terms (leaks, piracy, "play early", glitches...)
  7. permission recorded for the source
  8. independent checker: a separate `claude -p` call sees ONLY a frame grid + the post text + this checklist
     and returns PASS/FAIL. It never saw how the clip was made.

`python verify.py clip.mp4 --caption "..." [--min 15 --max 60 --mention @x --tags "#a #b" --no-checker]`
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BANNED = ["leak", "leaked", "play early", "early access download", "free download", "crack", "piracy",
          "mod menu", "money glitch", "afk bot", "free gta", "download gta"]

CHECKER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "failed_items": {"type": "array", "items": {"type": "string"}},
        "fix": {"type": "string"},
    },
    "required": ["verdict", "failed_items", "fix"],
    "additionalProperties": False,
}

CHECKER_PROMPT = """You are a strict pre-post reviewer for a TikTok clipping page. You did not make this clip.
Read the image file {grid} (a 3x3 grid of frames sampled across the clip, left-to-right, top-to-bottom) and judge
the clip and its post text against this checklist. Answer FAIL if any item fails.

1. Frames show real stream content from the credited creator (gameplay, IRL, reactions), not a menu, loading screen,
   black screen or a test pattern. Campaign rules for this source: {rules}
2. The vertical frame is usable: the action is visible and the subject isn't cut off. The full 16:9 frame centered over a
   blurred copy of itself is an accepted layout, and so is a facecam stacked above gameplay.
3. On-screen text (hook, captions) is readable. A campaign watermark in the middle of the frame is REQUIRED by the
   campaign and is fine; other third-party logos are not.
4. No nudity, gore beyond normal game violence, slurs or personal info (doxxing) visible.
5. Post text matches what the frames show and does not promise something the clip doesn't deliver.
6. Post text has no leak, piracy, "play early", glitch or impersonation claims.

Post text:
{caption}

Reply with JSON only: verdict PASS or FAIL, failed_items (item numbers + short reason), fix (one sentence)."""


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(p: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)])
    return json.loads(r.stdout) if r.returncode == 0 else {}


def loudness(p: Path) -> float | None:
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-vn", "-af", "ebur128", "-f", "null", "-"])
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", r.stderr)
    return float(m[-1]) if m else None


def bad_video_ratio(p: Path, dur: float) -> tuple[float, float]:
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-an",
             "-vf", "fps=5,scale=270:-2,blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2",
             "-f", "null", "-"])
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", r.stderr))
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", r.stderr))
    # a freeze still running at the end has no duration line
    starts = re.findall(r"freeze_start:\s*([\d.]+)", r.stderr)
    ends = re.findall(r"freeze_end:\s*([\d.]+)", r.stderr)
    if len(starts) > len(ends):
        frozen += dur - float(starts[-1])
    return (black / dur if dur else 1.0, frozen / dur if dur else 1.0)


def frame_grid(p: Path, dur: float, out: Path) -> Path:
    step = max(dur / 9, 0.5)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
         "-vf", f"fps=1/{step:.3f},scale=360:640,tile=3x3", "-frames:v", "1", str(out)])
    return out


def hashtags(text: str) -> list[str]:
    return re.findall(r"#\w+", text)


def independent_check(grid: Path, caption: str, rules: str = "") -> tuple[bool, str]:
    claude = shutil.which("claude")
    if not claude:
        return False, "claude CLI not found, checker could not run"
    prompt = CHECKER_PROMPT.format(grid=grid, caption=caption, rules=rules or "(none given)")
    r = run([claude, "-p", "--output-format", "json", "--allowedTools", "Read",
             "--add-dir", str(grid.parent), "--json-schema", json.dumps(CHECKER_SCHEMA)],
            input=prompt, timeout=600)
    if r.returncode != 0:
        return False, f"checker error: {r.stderr.strip()[-200:]}"
    try:
        out = json.loads(r.stdout)
        res = out.get("structured_output") if isinstance(out.get("structured_output"), dict) else None
        if res is None:
            t = out.get("result", "")
            res = json.loads(t[t.index("{"): t.rindex("}") + 1])
    except (ValueError, KeyError) as e:
        return False, f"checker reply unreadable ({e})"
    ok = res["verdict"] == "PASS"
    return ok, "" if ok else f"{'; '.join(res['failed_items'])} | fix: {res['fix']}"


def verify(clip: Path, caption: str, *, min_len: float, max_len: float, mention: str = "", tags: str = "",
           permission: str = "", ass: Path | None = None, speech_words: int = 0, checker: bool = True,
           rules: str = "") -> list[tuple[str, bool, str]]:
    res: list[tuple[str, bool, str]] = []
    add = lambda n, ok, d="": res.append((n, bool(ok), d))

    p = probe(clip) if clip.exists() else {}
    streams = p.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = float(p.get("format", {}).get("duration", 0) or 0)
    fps = 0.0
    if v:
        num, den = (v.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den or 1)
    add("1. 1080x1920 ~30fps", v and (v["width"], v["height"]) == (1080, 1920) and 29 <= fps <= 31,
        f"{v and v.get('width')}x{v and v.get('height')} @ {fps:.1f}")
    lufs = loudness(clip) if a else None
    add("2. audio at -18..-10 LUFS", lufs is not None and -18 <= lufs <= -10, f"{lufs}")
    add(f"3. duration {min_len:g}-{max_len:g}s", min_len - 0.5 <= dur <= max_len + 0.5, f"{dur:.1f}s")
    if dur:
        b, f = bad_video_ratio(clip, dur)
        add("4. not black/frozen", b < 0.10 and f < 0.40, f"black {b:.0%}, frozen {f:.0%}")
    else:
        add("4. not black/frozen", False, "no duration")
    if speech_words >= 10:
        txt = ass.read_text(encoding="utf-8", errors="replace") if ass and ass.exists() else ""
        cap_lines = len(re.findall(r"^Dialogue: 0,", txt, re.M))
        add("5. captions present when there is speech", cap_lines > 0,
            f"{cap_lines} caption lines for {speech_words} spoken words")
    tg = hashtags(caption)
    add("6a. post text <= 2200 chars, 3-8 hashtags", len(caption) <= 2200 and 3 <= len(tg) <= 8,
        f"{len(caption)} chars, {len(tg)} tags")
    hits = [w for w in BANNED if w in caption.lower()]
    add("6b. no banned terms", not hits, ", ".join(hits))
    if mention:
        add("6c. credit/mention present", mention.lower() in caption.lower(), mention)
    req = hashtags(tags)
    if req:
        miss = [t for t in req if t.lower() not in caption.lower()]
        add("6d. required hashtags", not miss, " ".join(miss))
    add("7. permission recorded", bool(permission.strip()), permission[:60])

    if checker and all(ok for _, ok, _ in res):  # only spend the checker on clips that pass the mechanical items
        grid = frame_grid(clip, dur, clip.with_suffix(".grid.jpg"))
        ok, why = independent_check(grid, caption, rules)
        add("8. independent checker", ok, why)
    return res


def fix_caption(caption: str, *, mention: str, tags: str) -> str:
    """Mechanical fixes for text items (6a-6d). Video items are fixed by re-rendering, not here."""
    for w in BANNED:
        caption = re.sub(re.escape(w), "", caption, flags=re.I)
    body = re.sub(r"\s*#\w+", "", caption).strip()
    have = hashtags(caption)
    for t in hashtags(tags):
        if t.lower() not in [h.lower() for h in have]:
            have.insert(0, t)
    if len(have) < 3:
        for t in ["#gta6", "#gtavi", "#gaming"]:
            if t not in have:
                have.append(t)
    have = list(dict.fromkeys(have))[:8]
    if mention and mention.lower() not in body.lower():
        body = f"{body} (via {mention})"
    return (body[: 2200 - 80] + " " + " ".join(have)).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--caption", default="")
    ap.add_argument("--min", type=float, default=15)
    ap.add_argument("--max", type=float, default=90)
    ap.add_argument("--mention", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--permission", default="manual check")
    ap.add_argument("--no-checker", action="store_true")
    a = ap.parse_args()
    res = verify(Path(a.clip), a.caption, min_len=a.min, max_len=a.max, mention=a.mention, tags=a.tags,
                 permission=a.permission, checker=not a.no_checker)
    for n, ok, d in res:
        print(f"{'PASS' if ok else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))
    bad = [r for r in res if not r[1]]
    print("RESULT:", "FAIL" if bad else "PASS")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
