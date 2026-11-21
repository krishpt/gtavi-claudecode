#!/usr/bin/env python3
"""GTA VI clipper: long gameplay recording -> ranked 9:16 TikTok clips with captions.

Pipeline:
  1. probe       ffprobe for duration / resolution
  2. transcribe  faster-whisper with word timestamps (skippable)
  3. signals     per-second loudness (ffmpeg ebur128) + scene cuts (ffmpeg scdet)
  4. candidates  sliding windows scored on loudness spikes, scene density, hype words
  5. rank        Claude picks the best N, tightens in/out points, writes title/caption/hashtags
                 (falls back to the heuristic score when no API key is available)
  6. render      ffmpeg: trim, 1080x1920 (blurred-fill or center-crop), burned-in
                 word-by-word captions, hook text, loudness-normalised audio
  7. package     metadata.json + UPLOAD.md per run. Nothing is posted automatically.

Usage:
  python clipper.py run my_session.mp4 --out out/ --clips 5
  python clipper.py run my_session.mp4 --no-transcribe --no-claude   # offline/heuristic only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

HYPE_WORDS = {
    "oh my god", "no way", "let's go", "lets go", "what", "bro", "dude", "holy", "insane",
    "crazy", "run", "go go", "watch this", "clip that", "clip it", "wanted", "cops", "five stars",
    "5 stars", "wasted", "busted", "heist", "chase", "crash", "jump", "explode", "boom", "yo",
    "wait", "how", "nah", "omg", "help", "behind you", "get in", "drive",
}

# ----------------------------------------------------------------------------- utils

def run(cmd: list[str], capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, check=False)


def log(msg: str):
    print(f"[clipper] {msg}", file=sys.stderr, flush=True)


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if out.returncode != 0:
        sys.exit(f"ffprobe failed: {out.stderr}")
    data = json.loads(out.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(data["format"]["duration"]),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "has_audio": a is not None,
    }

# ----------------------------------------------------------------------------- transcription

def transcribe(path: Path, model_size: str, cache: Path) -> list[dict]:
    """Return list of words: {start, end, word}. Cached to transcript.json."""
    if cache.exists():
        return json.loads(cache.read_text())
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed; skipping transcription")
        return []
    log(f"transcribing with whisper '{model_size}' (first run downloads the model)")
    words = []
    try:
        model = WhisperModel(model_size, device="auto", compute_type="auto")
        segments, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
        for seg in segments:
            for w in seg.words or []:
                words.append({"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()})
    except Exception as e:  # model download blocked, out of memory...
        log(f"transcription failed ({e.__class__.__name__}); continuing without captions")
        return []
    cache.write_text(json.dumps(words))
    log(f"transcribed {len(words)} words")
    return words

# ----------------------------------------------------------------------------- signals

def loudness_per_second(path: Path, duration: float) -> list[float]:
    """Momentary loudness (LUFS) averaged per second, via ffmpeg ebur128."""
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
               "-af", "ebur128=peak=none", "-f", "null", "-"])
    secs = [[] for _ in range(int(math.ceil(duration)) + 1)]
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)", out.stderr):
        t, val = float(m.group(1)), m.group(2)
        lufs = -70.0 if val == "-inf" else max(float(val), -70.0)
        i = int(t)
        if i < len(secs):
            secs[i].append(lufs)
    return [sum(s) / len(s) if s else -70.0 for s in secs]


def scene_cuts(path: Path, threshold: float = 10.0) -> list[float]:
    """Timestamps of hard scene changes (scdet score 0-100), on a downscaled copy for speed."""
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
               "-vf", f"fps=6,scale=320:-2,scdet=threshold={threshold}", "-f", "null", "-"])
    return [float(m.group(1)) for m in re.finditer(r"lavfi\.scd\.time:\s*([\d.]+)", out.stderr)]

# ----------------------------------------------------------------------------- candidates

@dataclass
class Candidate:
    start: float
    end: float
    score: float
    loud_peak: float
    scene_cuts: int
    hype_hits: int
    text: str = ""
    # filled by ranking
    title: str = ""
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    why: str = ""


def zscores(xs: list[float]) -> list[float]:
    if not xs:
        return []
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) or 1.0
    return [(x - mu) / sd for x in xs]


def words_between(words: list[dict], a: float, b: float) -> str:
    return " ".join(w["word"] for w in words if a <= w["start"] < b)


def find_candidates(duration: float, loud: list[float], cuts: list[float], words: list[dict],
                    win: float, step: float, keep: int) -> list[Candidate]:
    lz = zscores(loud)
    cands: list[Candidate] = []
    t = 0.0
    win = min(win, duration)
    while t + win <= duration + 0.01:
        a, b = t, t + win
        seg = lz[int(a):int(b)] or [0.0]
        top = sorted(seg, reverse=True)[: max(3, len(seg) // 10)]
        loud_peak = sum(top) / len(top)              # how intense the loudest moments are
        n_cuts = sum(1 for c in cuts if a <= c < b)
        text = words_between(words, a, b)
        low = text.lower()
        hype = sum(low.count(h) for h in HYPE_WORDS)
        speech_density = len(text.split()) / win     # words/sec
        # Peak early in the window = better hook. Reward loudness in the first 5s.
        hook_bonus = max(lz[int(a):int(a) + 5] or [0.0])
        score = (1.5 * loud_peak + 0.6 * hook_bonus + 0.15 * n_cuts
                 + 0.4 * min(hype, 8) + 0.5 * min(speech_density, 3))
        cands.append(Candidate(round(a, 2), round(b, 2), round(score, 3), round(loud_peak, 2),
                               n_cuts, hype, text))
        t += step
    # greedy non-overlapping selection
    cands.sort(key=lambda c: c.score, reverse=True)
    picked: list[Candidate] = []
    for c in cands:
        if all(c.end <= p.start or c.start >= p.end for p in picked):
            picked.append(c)
        if len(picked) >= keep:
            break
    return picked

# ----------------------------------------------------------------------------- Claude ranking

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["candidate_id", "start", "end", "title", "hook", "caption", "hashtags", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an editor for a GTA VI gameplay TikTok channel run by the person who recorded this footage.
You receive candidate windows from one long recording, each with signal stats and the transcript of the creator's
commentary. Pick the clips most likely to hold attention on TikTok and package them.

What makes a good clip: a clear payoff (chase, crash, heist moment, funny fail, surprising discovery), something
happening in the first 2 seconds, and commentary that adds personality. Skip menus, loading, quiet driving.

Rules:
- start/end are absolute seconds in the source. Stay inside the candidate's window, but you may tighten it so the
  clip opens on action and ends right after the payoff. Respect the min/max length you are given.
- hook: 3-7 words of on-screen text for the first seconds. No clickbait that the clip doesn't deliver.
- title: short, for your own file naming. caption: one or two lines for the TikTok post, in a natural creator voice.
- hashtags: 4-6, lowercase, without '#', mixing broad (gta6, gaming) and specific to the moment.
- Never claim the footage is from someone else, never mention leaks, and do not invent things not in the evidence.
"""


def rank_with_claude(cands: list[Candidate], n: int, min_len: float, max_len: float, model: str) -> list[Candidate]:
    import anthropic

    client = anthropic.Anthropic()
    listing = []
    for i, c in enumerate(cands):
        listing.append({
            "candidate_id": i, "window_start": c.start, "window_end": c.end,
            "heuristic_score": c.score, "loudness_peak_z": c.loud_peak,
            "scene_cuts": c.scene_cuts, "hype_word_hits": c.hype_hits,
            "transcript": c.text[:2500] or "(no speech)",
        })
    user = (f"Choose up to {n} clips. Each must be between {min_len:.0f}s and {max_len:.0f}s long.\n\n"
            + json.dumps(listing, indent=1))
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": RANK_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined the ranking request")
    text = next(b.text for b in resp.content if b.type == "text")
    return apply_picks(cands, json.loads(text)["clips"], n, min_len)


def apply_picks(cands: list[Candidate], picks: list[dict], n: int, min_len: float) -> list[Candidate]:
    out = []
    for p in picks[:n]:
        base = cands[p["candidate_id"]]
        s = max(base.start, float(p["start"]))
        e = min(base.end, float(p["end"]))
        if e - s < min_len:  # clamp back to a valid length inside the window
            e = min(base.end, s + min_len)
            s = max(base.start, e - min_len)
        c = Candidate(**{**asdict(base), "start": round(s, 2), "end": round(e, 2)})
        c.title, c.hook, c.caption = p["title"], p["hook"], p["caption"]
        c.hashtags = [h.lstrip("#").lower() for h in p["hashtags"]]
        c.why = p["why"]
        out.append(c)
    return out


def rank_heuristic(cands: list[Candidate], n: int) -> list[Candidate]:
    out = []
    for i, c in enumerate(cands[:n]):
        c.title = f"clip_{i + 1:02d}"
        c.hook = "Wait for it..."
        c.caption = "GTA VI moment"
        c.hashtags = ["gta6", "gtavi", "gaming", "gta", "fyp"]
        c.why = f"heuristic score {c.score}"
        out.append(c)
    return out

# ----------------------------------------------------------------------------- captions

def ass_time(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[dict], start: float, end: float, hook: str, out: Path, per_line: int = 3,
              credit: str = ""):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,86,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,2,80,80,560,1
Style: Credit,DejaVu Sans,40,&H40FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,2,80,80,120,1
Style: Hook,DejaVu Sans,72,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,0,0,3,18,0,8,80,80,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(3.0)},Hook,,0,0,0,,{ass_escape(hook.upper())}\n")
    if credit:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end - start)},Credit,,0,0,0,,{ass_escape(credit)}\n")
    ws = [w for w in words if start <= w["start"] < end]
    for i in range(0, len(ws), per_line):
        chunk = ws[i:i + per_line]
        c0 = chunk[0]["start"] - start
        c1 = (ws[i + per_line]["start"] if i + per_line < len(ws) else chunk[-1]["end"] + 0.3) - start
        c1 = min(c1, chunk[-1]["end"] - start + 0.6)
        # karaoke-style: each word highlights as it is spoken
        parts = []
        for w in chunk:
            dur_cs = max(int((w["end"] - w["start"]) * 100), 5)
            parts.append(f"{{\\k{dur_cs}}}{ass_escape(w['word'].upper())}")
        lines.append(f"Dialogue: 0,{ass_time(c0)},{ass_time(c1)},Cap,,0,0,0,,{' '.join(parts)}\n")
    out.write_text("".join(lines), encoding="utf-8")

# ----------------------------------------------------------------------------- render

def render(src: Path, c: Candidate, ass: Path, out: Path, layout: str, has_audio: bool,
           watermark: str = "", wm_width: float = 0.5, wm_y: float = 0.25):
    dur = c.end - c.start
    ass_f = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    if layout == "crop":
        vf = f"[0:v]scale=-2:1920,crop=1080:1920,setsar=1,ass='{ass_f}'[v]"
    else:  # blur: full 16:9 frame in the middle, blurred zoomed copy behind it
        vf = ("[0:v]split[a][b];"
              "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.08[bg];"
              "[b]scale=1080:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass='{ass_f}'[v]")
    inputs = ["-ss", f"{c.start:.2f}", "-t", f"{dur:.2f}", "-i", str(src)]
    if watermark:  # campaign watermark: horizontally centred, large, never at an edge
        inputs += ["-loop", "1", "-i", str(watermark)]
        w = int(1080 * wm_width)
        vf = (vf[:-3] + "[base];" +
              f"[1:v]scale={w}:-2,format=rgba[wm];"
              f"[base][wm]overlay=(W-w)/2:H*{wm_y}-h/2:shortest=1[v]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", vf, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-movflags", "+faststart", str(out)]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed for {out.name}: {r.stderr[-800:]}")

# ----------------------------------------------------------------------------- main

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "clip"


def cmd_fetch(args):
    """Download a VOD you are allowed to clip (your own, or a streamer's under a clipping program)."""
    if not args.permission:
        sys.exit("Refusing: pass --permission 'how you have it' (e.g. 'own channel' or 'StreamerX clipping campaign'). "
                 "Clipping other creators without permission gets videos removed and accounts struck.")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    r = run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4",
             "-o", str(out / "%(uploader)s_%(id)s.%(ext)s"), "--print", "after_move:filepath", args.url])
    if r.returncode != 0:
        sys.exit(r.stderr[-800:])
    path = r.stdout.strip().splitlines()[-1]
    Path(path + ".permission.txt").write_text(f"{args.url}\n{args.permission}\n")
    print(path)


def cmd_run(args):
    src = Path(args.video).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".work"
    work.mkdir(exist_ok=True)

    info = probe(src)
    log(f"source: {info['duration']:.0f}s {info['width']}x{info['height']} audio={info['has_audio']}")
    if info["duration"] < args.min_len:
        sys.exit(f"source is shorter than --min-len ({args.min_len}s)")

    words = [] if args.no_transcribe or not info["has_audio"] else \
        transcribe(src, args.whisper_model, work / f"{src.stem}.transcript.json")
    sig_cache = work / f"{src.stem}.signals.json"
    if sig_cache.exists():
        sig = json.loads(sig_cache.read_text())
        loud, cuts = sig["loud"], sig["cuts"]
    else:
        log("measuring loudness")
        loud = loudness_per_second(src, info["duration"]) if info["has_audio"] else [0.0] * int(info["duration"] + 1)
        log("detecting scene cuts")
        cuts = scene_cuts(src)
        sig_cache.write_text(json.dumps({"loud": loud, "cuts": cuts}))
    log(f"{len(cuts)} scene cuts")

    win = args.max_len + 15  # give Claude room to tighten
    cands = find_candidates(info["duration"], loud, cuts, words, win=win, step=args.step,
                            keep=max(args.clips * 3, 6))
    (work / "candidates.json").write_text(json.dumps([asdict(c) for c in cands], indent=1))
    log(f"{len(cands)} candidate windows")
    if args.candidates_only:
        print(work / "candidates.json")
        return

    picks = None
    if args.picks:  # picks written by hand or by Claude Code reading candidates.json (no API cost)
        picks = apply_picks(cands, json.loads(Path(args.picks).read_text())["clips"], args.clips, args.min_len)
        log(f"loaded {len(picks)} picks from {args.picks}")
    elif not args.no_claude:
        try:
            picks = rank_with_claude(cands, args.clips, args.min_len, args.max_len, args.model)
            log(f"Claude picked {len(picks)} clips")
        except Exception as e:  # missing key, network, refusal...
            log(f"Claude ranking unavailable ({e.__class__.__name__}: {e}); using heuristic ranking")
    if picks is None:
        picks = rank_heuristic(cands, args.clips)
        for c in picks:  # heuristic windows are win long; trim to max_len around the start
            c.end = round(min(c.end, c.start + args.max_len), 2)

    manifest = []
    for i, c in enumerate(picks, 1):
        name = f"{i:02d}_{slug(c.title)}"
        ass = work / f"{name}.ass"
        mp4 = out_dir / f"{name}.mp4"
        build_ass(words, c.start, c.end, c.hook, ass, credit=args.credit)
        log(f"rendering {mp4.name} ({c.end - c.start:.0f}s from {c.start:.0f}s)")
        render(src, c, ass, mp4, args.layout, info["has_audio"], args.watermark, args.wm_width, args.wm_y)
        manifest.append({
            "file": mp4.name, "source": src.name, "start": c.start, "end": c.end,
            "length_s": round(c.end - c.start, 1), "title": c.title, "hook": c.hook,
            "caption": c.caption, "hashtags": c.hashtags, "why": c.why,
            "post_text": f"{c.caption}" + (f" (via {args.credit})" if args.credit else "") + " "
                         + " ".join(f"#{h}" for h in c.hashtags),
            "status": "needs_review",
        })
    (out_dir / "metadata.json").write_text(json.dumps(manifest, indent=1))
    md = ["# Upload queue\n", "Review each clip before posting. Nothing here has been posted.\n",
          "When uploading, turn on TikTok's AI-generated/edited label only if you add AI visuals or voice.\n"]
    for m in manifest:
        md.append(f"\n## {m['file']} ({m['length_s']}s)\n\n- Source: {m['source']} {m['start']}s to {m['end']}s\n"
                  f"- Why: {m['why']}\n\nPost text:\n\n```\n{m['post_text']}\n```\n")
    (out_dir / "UPLOAD.md").write_text("".join(md))
    log(f"done: {len(manifest)} clips in {out_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="clip one recording")
    r.add_argument("video")
    r.add_argument("--out", default="out")
    r.add_argument("--clips", type=int, default=5)
    r.add_argument("--min-len", type=float, default=62, help="seconds; >60 so clips qualify for Creator Rewards")
    r.add_argument("--max-len", type=float, default=90)
    r.add_argument("--step", type=float, default=10, help="window slide in seconds")
    r.add_argument("--layout", choices=["blur", "crop"], default="blur")
    r.add_argument("--whisper-model", default="small", help="tiny/base/small/medium/large-v3")
    r.add_argument("--model", default="claude-opus-5-5")
    r.add_argument("--no-transcribe", action="store_true")
    r.add_argument("--no-claude", action="store_true")
    r.add_argument("--picks", help="JSON {clips:[...]} in the ranking schema; skips the API call")
    r.add_argument("--candidates-only", action="store_true", help="stop after writing .work/candidates.json")
    r.add_argument("--credit", default="", help="e.g. '@streamer' - burned in and added to the caption")
    r.add_argument("--watermark", default="", help="image burned in centred (clipping campaigns require it)")
    r.add_argument("--wm-width", type=float, default=0.5, help="watermark width as a fraction of 1080")
    r.add_argument("--wm-y", type=float, default=0.25, help="watermark centre height as a fraction of the frame")
    r.set_defaults(func=cmd_run)
    f = sub.add_parser("fetch", help="download a VOD you have permission to clip (needs yt-dlp)")
    f.add_argument("url")
    f.add_argument("--permission", required=False, help="how you have the right to clip this")
    f.add_argument("--out", default="vods")
    f.set_defaults(func=cmd_fetch)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
