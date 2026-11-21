#!/usr/bin/env python3
"""Post to TikTok through a real browser (Playwright, persistent profile). No developer app or audit needed.

One-time:  python tiktok_browser.py login      (a browser opens; log in to the CLIPPING account, then close it)
Check:     python tiktok_browser.py whoami
Post:      python tiktok_browser.py post clip.mp4 "caption #tags"

Safety: before every post it reads which account is logged in and refuses blocked accounts (personal ones).
Selectors on TikTok's upload page change from time to time; on any failure it saves a screenshot next to the
profile folder and raises, so the clip stays queued and the daily report shows the error.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".tiktok-profile"
SHOTS = HERE / "browser_errors"
BLOCKED = {"krishpt16"} | {a.strip().lstrip("@").lower()
                           for a in os.environ.get("TIKTOK_BLOCKED_ACCOUNTS", "").split(",") if a.strip()}
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"


def _ctx(p, headless: bool):
    return p.chromium.launch_persistent_context(
        str(PROFILE), headless=headless, viewport={"width": 1366, "height": 900},
        args=["--disable-blink-features=AutomationControlled"])


def _shot(page, tag: str) -> str:
    SHOTS.mkdir(exist_ok=True)
    f = SHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
    try:
        page.screenshot(path=str(f), full_page=True)
    except Exception:
        pass
    return str(f)


def whoami(page) -> str:
    page.goto("https://www.tiktok.com/profile", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", page.url)
    if not m:
        raise RuntimeError(f"not logged in to TikTok in the autopilot browser (landed on {page.url}); "
                           "run `python tiktok_browser.py login`")
    return m.group(1).lower()


def _first(page, selectors: list[str], timeout=20000):
    end = time.time() + timeout / 1000
    while time.time() < end:
        for s in selectors:
            loc = page.locator(s)
            if loc.count() and loc.first.is_visible():
                return loc.first
        page.wait_for_timeout(500)
    raise RuntimeError(f"none of {selectors} appeared")


def post_video(path: Path, caption: str, expected: str = "", headless: bool = True) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _ctx(p, headless)
        page = ctx.new_page()
        try:
            me = whoami(page)
            if me in BLOCKED:
                raise RuntimeError(f"@{me} is a blocked (personal) account; not posting")
            if expected and me != expected.lstrip("@").lower():
                raise RuntimeError(f"logged in as @{me}, expected @{expected}; not posting")

            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            page.locator('input[type="file"]').first.set_input_files(str(path), timeout=60000)

            editor = _first(page, ['div[contenteditable="true"][role="combobox"]', 'div.public-DraftEditor-content',
                                   'div[contenteditable="true"]'], timeout=120000)
            editor.click()
            page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
            page.keyboard.press("Backspace")
            # type text and hashtags separately; TikTok opens a suggestion popup after each '#tag'
            for part in re.split(r"(\s+)", caption[:2200]):
                page.keyboard.type(part, delay=15)
                if part.startswith("#"):
                    page.wait_for_timeout(700)
                    page.keyboard.press("Escape")

            # wait until the upload finished processing and the Post button is enabled
            btn = _first(page, ['button[data-e2e="post_video_button"]', 'button:has-text("Post")'], timeout=60000)
            end = time.time() + 600
            while btn.is_disabled() and time.time() < end:
                page.wait_for_timeout(2000)
            if btn.is_disabled():
                raise RuntimeError("upload never finished processing")
            btn.click()

            # some accounts get a "continue to post?" content-check dialog
            try:
                _first(page, ['button:has-text("Post now")'], timeout=5000).click()
            except RuntimeError:
                pass
            page.wait_for_url(re.compile(r"tiktokstudio/content|/manage|upload.*success"), timeout=120000)

            page.goto(f"https://www.tiktok.com/@{me}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            link = page.locator('a[href*="/video/"]').first
            url = link.get_attribute("href") if link.count() else ""
            return {"status": "PUBLISHED", "account": me, "url": url}
        except Exception as e:
            raise RuntimeError(f"{e} (screenshot: {_shot(page, 'post')})") from None
        finally:
            ctx.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _ctx(p, headless=False)
            page = ctx.new_page()
            page.goto("https://www.tiktok.com/login")
            print("Log in to the CLIPPING account in the window, then close the window.")
            page.wait_for_event("close", timeout=0)
            ctx.close()
    elif cmd == "whoami":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _ctx(p, headless=True)
            print("@" + whoami(ctx.new_page()))
            ctx.close()
    elif cmd == "post" and len(sys.argv) >= 4:
        print(post_video(Path(sys.argv[2]), sys.argv[3], expected=os.environ.get("TIKTOK_EXPECTED_ACCOUNT", "")))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
