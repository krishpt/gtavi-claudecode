#!/usr/bin/env python3
"""Hands-off loop: watch allowed channels -> download new VODs -> clip -> post to TikTok.

  python autoclip.py once          # one pass (what the scheduler runs)
  python autoclip.py once --dry-run  # everything except posting
  python autoclip.py post          # only post queued clips (respects the daily cap)
  python autoclip.py status        # what's been seen, queued and posted

Config lives in config.json (copy config.example.json). State lives in state.json.
Only channels listed in config.json with a filled-in "permission" field are ever downloaded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clipper  # noqa: E402
import verify   # noqa: E402
import tiktok   # noqa: E402

CONFIG = HERE / "config.json"
STATE = HERE / "state.json"


def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, file=sys.stderr, flush=True)
    with open(HERE / "autoclip.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)

# ----------------------------------------------------------------------------- discover + download

def latest_vods(url: str, n: int) -> list[dict]:
    r = subprocess.run(["yt-dlp", "--flat-playlist", "--playlist-end", str(n), "-J", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  could not list {url}: {r.stderr.strip()[-300:]}")
        return []
    data = json.loads(r.stdout)
    entries = data.get("entries") or [data]
    return [{"id": e.get("id"), "url": e.get("url") or e.get("webpage_url"), "title": e.get("title", ""),
             "duration": e.get("duration")} for e in entries if e.get("id")]


def download(vod: dict, dest: Path, max_height: int) -> Path | None:
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["yt-dlp", "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]",
                        "--merge-output-format", "mp4", "--no-progress",
                        "-o", str(dest / "%(id)s.%(ext)s"), "--print", "after_move:filepath", vod["url"]],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  download failed: {r.stderr.strip()[-300:]}")
        return None
    return Path(r.stdout.strip().splitlines()[-1])

# ----------------------------------------------------------------------------- ranking via Claude Code

def rank_with_claude_code(cands_path: Path, n: int, min_len: float, max_len: float, extra: str) -> dict | None:
    """Ask the local `claude` CLI to pick clips. Uses your Claude plan, no API key needed."""
    claude = shutil.which("claude")
    if not claude:
        return None
    cands = json.loads(cands_path.read_text())
    listing = [{"candidate_id": i, "window_start": c["start"], "window_end": c["end"],
                "heuristic_score": c["score"], "loudness_peak_z": c["loud_peak"], "scene_cuts": c["scene_cuts"],
                "hype_word_hits": c["hype_hits"], "transcript": (c["text"] or "(no speech)")[:2500]}
               for i, c in enumerate(cands)]
    prompt = (clipper.SYSTEM_PROMPT + "\n" + extra +
              f"\nChoose up to {n} clips, each between {min_len:.0f}s and {max_len:.0f}s long. "
              "Reply with JSON only, matching the schema.\n\n" + json.dumps(listing, indent=1))
    r = subprocess.run([claude, "-p", "--output-format", "json",
                        "--json-schema", json.dumps(clipper.RANK_SCHEMA)],
                       input=prompt, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        log(f"  claude -p failed: {r.stderr.strip()[-300:]}")
        return None
    try:
        out = json.loads(r.stdout)
        if isinstance(out.get("structured_output"), dict):
            return out["structured_output"]
        text = out.get("result", "")
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, KeyError) as e:
        log(f"  could not parse Claude's picks ({e})")
        return None

# ----------------------------------------------------------------------------- clip one VOD

def clip_vod(src: Path, out_dir: Path, source: dict, cfg: dict) -> list[dict]:
    c = {**cfg["clipping"], **{k: source[k] for k in ("min_len", "max_len", "clips_per_vod") if k in source}}
    base = [sys.executable, str(HERE / "clipper.py"), "run", str(src), "--out", str(out_dir),
            "--clips", str(c["clips_per_vod"]), "--min-len", str(c["min_len"]), "--max-len", str(c["max_len"]),
            "--layout", c["layout"], "--whisper-model", c["whisper_model"]]
    if source.get("credit"):
        base += ["--credit", source["credit"]]

    ranker = c["ranker"]
    extra_args: list[str] = []
    if ranker == "claude-code":
        subprocess.run(base + ["--candidates-only"], check=True)
        picks = rank_with_claude_code(out_dir / ".work" / "candidates.json", c["clips_per_vod"],
                                      c["min_len"], c["max_len"], source.get("notes", ""))
        if picks:
            pf = out_dir / ".work" / "picks.json"
            pf.write_text(json.dumps(picks))
            extra_args = ["--picks", str(pf)]
        else:
            log("  Claude Code ranking unavailable, falling back to heuristic")
            extra_args = ["--no-claude"]
    elif ranker == "heuristic":
        extra_args = ["--no-claude"]
    # ranker == "api" -> clipper calls the Claude API itself (needs ANTHROPIC_API_KEY)
    subprocess.run(base + extra_args, check=True)
    return json.loads((out_dir / "metadata.json").read_text())


def gate(m: dict, out_dir: Path, source: dict, cfg: dict) -> tuple[bool, str, list]:
    """Verification loop: check -> fix text items -> re-check, max 3 rounds. Returns (passed, post_text, report)."""
    c = {**cfg["clipping"], **{k: source[k] for k in ("min_len", "max_len") if k in source}}
    clip = out_dir / m["file"]
    work = out_dir / ".work"
    ass = work / (Path(m["file"]).stem + ".ass")
    words = []
    for t in work.glob("*.transcript.json"):
        words = json.loads(t.read_text())
    spoken = sum(1 for w in words if m["start"] <= w["start"] < m["end"])
    mention = source.get("credit", "")
    tags = source.get("required_hashtags", "")
    caption = m["post_text"]
    report = []
    for rnd in range(1, 4):
        report = verify.verify(clip, caption, min_len=c["min_len"], max_len=c["max_len"], mention=mention,
                               tags=tags, permission=source.get("permission", ""), ass=ass,
                               speech_words=spoken, checker=cfg.get("verification", {}).get("checker", True))
        failed = [n for n, ok, _ in report if not ok]
        log(f"    verify round {rnd} {m['file']}: {'PASS' if not failed else 'FAIL ' + ', '.join(failed)}")
        if not failed:
            return True, caption, report
        if all(n.startswith("6") for n in failed):  # only text items failed: these can be fixed mechanically
            caption = verify.fix_caption(caption, mention=mention, tags=tags)
            continue
        break  # video, permission or checker failures can't be fixed by editing text
    return False, caption, report

# ----------------------------------------------------------------------------- posting

def posted_today(state) -> int:
    today = dt.date.today().isoformat()
    return sum(1 for p in state["posted"] if p["at"].startswith(today))


def post_queue(state, cfg, dry_run=False):
    pc = cfg["posting"]
    if pc["mode"] == "off":
        log("posting is off in config.json")
        return
    budget = pc["max_posts_per_day"] - posted_today(state)
    if budget <= 0:
        log(f"daily cap of {pc['max_posts_per_day']} reached")
        return
    for item in list(state["queue"])[:budget]:
        if dry_run:
            log(f"[dry-run] would post {item['file']}: {item['post_text']}")
            continue
        try:
            res = tiktok.post_video(Path(item["file"]), item["post_text"], mode=pc["mode"],
                                    privacy=pc.get("privacy", "PUBLIC_TO_EVERYONE"))
        except Exception as e:  # keep it queued and try next run
            log(f"post failed for {item['file']}: {e}")
            break
        state["queue"].remove(item)
        state["posted"].append({**item, "at": dt.datetime.now().isoformat(timespec="seconds"), "tiktok": res})
        save_state(state)
        log(f"posted ({pc['mode']}) {Path(item['file']).name} -> {res.get('status', res)}")

# ----------------------------------------------------------------------------- main loop

def cmd_once(args):
    cfg = load_json(CONFIG, None)
    if cfg is None:
        sys.exit("No config.json. Copy config.example.json to config.json and fill it in.")
    state = load_json(STATE, {"seen": {}, "queue": [], "posted": []})
    data = Path(cfg.get("data_dir") or HERE / "data").expanduser()

    for source in cfg["sources"]:
        name = source["name"]
        if not source.get("permission", "").strip():
            log(f"skipping {name}: no permission recorded in config.json")
            continue
        log(f"checking {name}")
        for vod in latest_vods(source["url"], cfg["clipping"].get("check_last_n", 3)):
            key = f"{name}:{vod['id']}"
            if key in state["seen"]:
                continue
            dur = vod.get("duration") or 0
            if dur and dur < cfg["clipping"]["min_len"] * 2:
                state["seen"][key] = {"skipped": "too short"}
                continue
            log(f"  new VOD {vod['id']} {vod['title'][:60]!r}")
            src = download(vod, data / "vods" / name, cfg["clipping"].get("max_height", 1080))
            if not src:
                continue
            out_dir = data / "clips" / name / vod["id"]
            try:
                clips = clip_vod(src, out_dir, source, cfg)
            except subprocess.CalledProcessError as e:
                log(f"  clipping failed: {e}")
                continue
            passed = 0
            for m in clips:
                ok, text, report = gate(m, out_dir, source, cfg)
                entry = {"file": str(out_dir / m["file"]), "post_text": text, "source": name, "vod": vod["id"],
                         "permission": source["permission"],
                         "checks": [f"{'PASS' if o else 'FAIL'} {n} {d}".strip() for n, o, d in report]}
                if ok:
                    state["queue"].append(entry)
                    passed += 1
                else:
                    entry["at"] = dt.datetime.now().isoformat(timespec="seconds")
                    state.setdefault("rejected", []).append(entry)
            state["seen"][key] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "clips": len(clips),
                                  "passed": passed}
            save_state(state)
            log(f"  {passed}/{len(clips)} clips passed verification and were queued")
            if cfg["clipping"].get("delete_vod_after", True):
                src.unlink(missing_ok=True)

    save_state(state)
    post_queue(state, cfg, dry_run=args.dry_run)


def cmd_post(args):
    cfg = load_json(CONFIG, None) or sys.exit("No config.json")
    post_queue(load_json(STATE, {"seen": {}, "queue": [], "posted": []}), cfg, dry_run=args.dry_run)


def cmd_report(args):
    """Summary for routine check-ins: last N days of VODs, verification, posts, errors."""
    s = load_json(STATE, {"seen": {}, "queue": [], "posted": []})
    since = (dt.datetime.now() - dt.timedelta(days=args.days)).isoformat()
    seen = [v for v in s["seen"].values() if v.get("at", "") >= since]
    posted = [p for p in s["posted"] if p["at"] >= since]
    rejected = [r for r in s.get("rejected", []) if r.get("at", "") >= since]
    errors = []
    logf = HERE / "autoclip.log"
    if logf.exists():
        for line in logf.read_text(encoding="utf-8").splitlines()[-2000:]:
            if line[1:20] >= since[:19].replace("T", " ") and any(w in line for w in ("failed", "error", "Error")):
                errors.append(line)
    print(f"# Autopilot report, last {args.days} day(s)")
    print(f"VODs processed: {len(seen)} | clips made: {sum(v.get('clips', 0) for v in seen)} | "
          f"passed verification: {sum(v.get('passed', 0) for v in seen)} | rejected: {len(rejected)} | "
          f"posted: {len(posted)} | still queued: {len(s['queue'])}")
    by_src = {}
    for p in posted:
        by_src[p["source"]] = by_src.get(p["source"], 0) + 1
    for k, v in by_src.items():
        print(f"  posted from {k}: {v}")
    for r in rejected[-5:]:
        print(f"  rejected {Path(r['file']).name}: " + "; ".join(c for c in r["checks"] if c.startswith("FAIL")))
    for e in errors[-10:]:
        print("  " + e)


def cmd_status(_):
    s = load_json(STATE, {"seen": {}, "queue": [], "posted": []})
    print(f"VODs seen: {len(s['seen'])}  queued clips: {len(s['queue'])}  posted: {len(s['posted'])} "
          f"(today: {posted_today(s)})")
    for q in s["queue"][:10]:
        print("  queued:", Path(q["file"]).name, "|", q["post_text"][:70])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("once", cmd_once), ("post", cmd_post), ("status", cmd_status), ("report", cmd_report)]:
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--days", type=int, default=1)
        p.set_defaults(func=fn)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
