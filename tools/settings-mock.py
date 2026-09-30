#!/usr/bin/env python3
"""Serve the :8080 settings page on a computer, against captured device state.

    settings-mock.py CAPTURE.json [--port 8099] [--settings PATH]

The page is read from biscuit-settings.py on every request, so an edit shows up
on reload. GET /api/* answers from CAPTURE.json - produced on a device, as root,
by tools/settings-capture.py - and POST /api/* is accepted, merged into that
state where the shape is obvious, and answered with {"ok": true}. Nothing talks
to a device; there is no login.

For working on the page's layout and wording. It proves nothing about the
backends: anything that changes the device has to be tried on one.
"""
import argparse
import copy
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SETTINGS = os.path.join(HERE, "..", "device", "testing",
                                "device-amazon-biscuit", "biscuit-settings.py")


def load_page(path):
    src = open(path, encoding="utf-8").read()
    m = re.search(r'^PAGE = r"""(.*?)^"""', src, re.S | re.M)
    if not m:
        raise SystemExit("no PAGE in " + path)
    return m.group(1)


class Mock(BaseHTTPRequestHandler):
    state = {}
    settings = DEFAULT_SETTINGS
    posts = []

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, load_page(self.settings), "text/html; charset=utf-8")
        if url.path == "/logout":
            return self._send(200, "signed out (mock)", "text/plain")
        if url.path == "/n.js":
            js = os.path.join(os.path.dirname(self.settings), "nacl-fast.min.js")
            return self._send(200, open(js, "rb").read(), "application/javascript")
        if url.path == "/seal":
            # No key: the page then sends forms as typed, which the mock takes.
            return self._json(200, {"key": "", "challenge": ""})
        if url.path == "/__posts":
            return self._json(200, self.posts[-50:])
        full = url.path + ("?" + url.query if url.query else "")
        if full in self.state:
            return self._json(200, self.state[full])
        if url.path in self.state:
            return self._json(200, self.state[url.path])
        return self._json(404, {"error": "not captured: " + url.path})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            body = {}
        self.posts.append({"path": url.path, "body": body})
        cur = self.state.get(url.path)
        # Shallow merge of scalar settings, so a toggle survives a reload.
        if isinstance(cur, dict) and isinstance(body, dict):
            for k, v in body.items():
                if k in cur and not isinstance(cur[k], (dict, list)):
                    cur[k] = v
        reply = {"ok": True, "message": "(mock) done"}
        if isinstance(cur, dict):
            reply.update(copy.deepcopy(cur))
        return self._json(200, reply)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--settings", default=DEFAULT_SETTINGS)
    a = ap.parse_args()
    Mock.state = json.load(open(a.capture, encoding="utf-8"))
    extra = os.path.splitext(a.capture)[0] + ".extra.json"
    if os.path.exists(extra):
        # Endpoints a capture predates, filled in by hand while designing them.
        Mock.state.update(json.load(open(extra, encoding="utf-8")))
    Mock.settings = os.path.abspath(a.settings)
    print("settings mock on http://127.0.0.1:%d/  (page from %s)" % (a.port, Mock.settings), flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Mock).serve_forever()


if __name__ == "__main__":
    main()
