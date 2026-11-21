#!/usr/bin/env python3
"""Minimal TikTok Content Posting API client (stdlib only).

One-time setup:
  1. Create an app at https://developers.tiktok.com, add "Login Kit" and "Content Posting API",
     request scopes user.info.profile, video.upload and video.publish, set a redirect URI (e.g. https://localhost/callback).
  2. export TIKTOK_CLIENT_KEY=...  TIKTOK_CLIENT_SECRET=...  TIKTOK_REDIRECT_URI=...
  3. python tiktok.py auth     (opens a login URL, paste back the URL you land on)

Until TikTok audits your app, direct posts can only be private (SELF_ONLY). "draft" mode sends the
video to your TikTok inbox instead, where you tap post. After the audit, "direct" posts publicly.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://open.tiktokapis.com/v2"
TOKEN_FILE = Path(os.environ.get("TIKTOK_TOKEN_FILE", Path(__file__).resolve().parent / "tiktok_token.json"))
CHUNK = 10 * 1024 * 1024
MAX_SINGLE = 64 * 1024 * 1024


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set (see tiktok.py docstring)")
    return v


def _post(url, data=None, form=None, token=None):
    headers = {}
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(data or {}).encode()
        headers["Content-Type"] = "application/json; charset=UTF-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TikTok {e.code}: {e.read().decode()[:500]}") from None
    err = out.get("error") or {}
    if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
        raise RuntimeError(f"TikTok error {err.get('code')}: {err.get('message')}")
    return out

# ----------------------------------------------------------------------------- auth

def auth_url(state: str) -> str:
    q = {"client_key": _env("TIKTOK_CLIENT_KEY"), "scope": "user.info.basic,user.info.profile,video.upload,video.publish",
         "response_type": "code", "redirect_uri": _env("TIKTOK_REDIRECT_URI"), "state": state}
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)


def _save(tok: dict):
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 0)) - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def exchange_code(code: str):
    tok = _post(f"{API}/oauth/token/", form={
        "client_key": _env("TIKTOK_CLIENT_KEY"), "client_secret": _env("TIKTOK_CLIENT_SECRET"),
        "code": code, "grant_type": "authorization_code", "redirect_uri": _env("TIKTOK_REDIRECT_URI")})
    _save(tok)


def access_token() -> str:
    if not TOKEN_FILE.exists():
        raise RuntimeError("not logged in: run `python tiktok.py auth`")
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() >= tok.get("expires_at", 0):
        new = _post(f"{API}/oauth/token/", form={
            "client_key": _env("TIKTOK_CLIENT_KEY"), "client_secret": _env("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        _save(new)
        tok = new
    return tok["access_token"]

# ----------------------------------------------------------------------------- account guard

# Accounts the autopilot must never post to, e.g. a personal account. Extend via TIKTOK_BLOCKED_ACCOUNTS (comma list).
BLOCKED = {"krishpt16"} | {a.strip().lstrip("@").lower() for a in os.environ.get("TIKTOK_BLOCKED_ACCOUNTS", "").split(",") if a.strip()}


def account_username(token: str) -> str:
    req = urllib.request.Request(f"{API}/user/info/?fields=open_id,username,display_name",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read()).get("data", {}).get("user", {})
    return (data.get("username") or "").lower()


def check_account(token: str):
    """Refuse to post unless we can confirm the logged-in account is NOT a blocked (personal) one."""
    try:
        name = account_username(token)
    except Exception as e:
        raise RuntimeError(f"could not confirm which TikTok account is logged in ({e}); not posting") from None
    if not name:
        raise RuntimeError("TikTok did not return a username; not posting")
    if name in BLOCKED:
        TOKEN_FILE.unlink(missing_ok=True)
        raise RuntimeError(f"@{name} is blocked (personal account). Token deleted, nothing posted.")

# ----------------------------------------------------------------------------- upload

def _source_info(size: int) -> dict:
    if size <= MAX_SINGLE:
        return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    count = size // CHUNK  # last chunk absorbs the remainder, as TikTok expects
    return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": CHUNK, "total_chunk_count": count}


def _upload(upload_url: str, path: Path, info: dict):
    size, csize, n = info["video_size"], info["chunk_size"], info["total_chunk_count"]
    with open(path, "rb") as f:
        for i in range(n):
            start = i * csize
            end = size - 1 if i == n - 1 else start + csize - 1
            f.seek(start)
            blob = f.read(end - start + 1)
            req = urllib.request.Request(upload_url, data=blob, method="PUT", headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            with urllib.request.urlopen(req, timeout=300):
                pass


def post_video(path: Path, caption: str, mode: str = "draft", privacy: str = "PUBLIC_TO_EVERYONE",
               wait: bool = True) -> dict:
    """mode: 'draft' (lands in your TikTok inbox) or 'direct' (posts with the given privacy)."""
    token = access_token()
    check_account(token)
    info = _source_info(path.stat().st_size)
    if mode == "direct":
        creator = _post(f"{API}/post/publish/creator_info/query/", token=token)["data"]
        options = creator.get("privacy_level_options", [])
        if privacy not in options:  # unaudited apps only get SELF_ONLY
            privacy = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
        body = {"post_info": {"title": caption[:2200], "privacy_level": privacy,
                              "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                              "video_cover_timestamp_ms": 1000},
                "source_info": info}
        init = _post(f"{API}/post/publish/video/init/", data=body, token=token)["data"]
    else:
        init = _post(f"{API}/post/publish/inbox/video/init/", data={"source_info": info}, token=token)["data"]
    _upload(init["upload_url"], path, info)
    result = {"publish_id": init["publish_id"], "mode": mode, "privacy": privacy if mode == "direct" else None}
    if wait:
        for _ in range(30):
            time.sleep(5)
            st = _post(f"{API}/post/publish/status/fetch/", data={"publish_id": init["publish_id"]},
                       token=token)["data"]
            result["status"] = st.get("status")
            if st.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
                if st.get("status") == "FAILED":
                    raise RuntimeError(f"TikTok processing failed: {st.get('fail_reason')}")
                break
    return result


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "auth":
        state = secrets.token_urlsafe(16)
        print("Open this, log in, then paste the full URL you get redirected to:\n\n" + auth_url(state) + "\n")
        back = urllib.parse.urlparse(input("> ").strip())
        q = urllib.parse.parse_qs(back.query)
        if q.get("state", [""])[0] != state:
            sys.exit("state mismatch, try again")
        exchange_code(q["code"][0])
        try:
            check_account(access_token())
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"saved token to {TOKEN_FILE}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "post":
        mode = sys.argv[4] if len(sys.argv) > 4 else "draft"
        print(post_video(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "", mode=mode))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
