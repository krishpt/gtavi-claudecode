#!/usr/bin/env python3
"""Serve autopilot/outbox on 127.0.0.1:8765 for the web uploader (TikTok Studio in the browser).

GET /index.json     -> queued items that have an outbox file: [{file, url, post_text, source, vod}]
GET /<file>.mp4     -> the clip
Only binds to localhost. CORS is limited to https://www.tiktok.com.
"""
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTBOX = HERE / "outbox"
STATE = HERE / "state.json"
ORIGIN = os.environ.get("OUTBOX_ALLOW_ORIGIN", "https://www.tiktok.com")
PORT = int(os.environ.get("OUTBOX_PORT", "8765"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(OUTBOX), **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/index.json":
            items = []
            try:
                queue = json.loads(STATE.read_text()).get("queue", [])
            except (OSError, ValueError):
                queue = []
            for q in queue:
                p = Path(q.get("outbox") or "")
                if p.parent == OUTBOX and p.exists():
                    items.append({"file": p.name, "url": f"http://127.0.0.1:{PORT}/{p.name}",
                                  "post_text": q["post_text"], "source": q["source"], "vod": q["vod"]})
            body = json.dumps(items).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.split("?")[0].endswith(".mp4"):
            self.send_error(404)
            return
        super().do_GET()

    def list_directory(self, path):
        self.send_error(404)


if __name__ == "__main__":
    OUTBOX.mkdir(exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
