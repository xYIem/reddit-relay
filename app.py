"""
Reddit Reply Relay
------------------
Deployed on Railway. Accepts POST /reply from WSL2 agent.
Uses Chrome browser cookies (REDDIT_COOKIES env var) — no login needed.
Cookies last ~2 years. Re-extract with extract_reddit_cookies.py when expired.

Auth: Bearer token in Authorization header (RELAY_SECRET env var).
"""

import os
import re
import json
import time
import urllib.parse
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

RELAY_SECRET    = os.environ["RELAY_SECRET"]
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "ylelvl")
REDDIT_UA       = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_session = None
_modhash = ""


def _safe_cookie_value(val: str) -> str:
    """Ensure cookie value is latin-1 safe (HTTP header requirement)."""
    try:
        val.encode("latin-1")
        return val
    except (UnicodeEncodeError, UnicodeDecodeError):
        return urllib.parse.quote(val, safe="")


def build_session() -> requests.Session:
    global _session, _modhash

    cookies_json = os.environ.get("REDDIT_COOKIES", "")
    if not cookies_json:
        raise RuntimeError("REDDIT_COOKIES env var not set")

    cookie_list = json.loads(cookies_json)
    s = requests.Session()
    s.headers.update({"User-Agent": REDDIT_UA})

    for c in cookie_list:
        s.cookies.set(
            c["name"],
            _safe_cookie_value(c["value"]),
            domain=c.get("domain", ".reddit.com"),
            path=c.get("path", "/"),
        )

    # Fetch modhash (CSRF token) needed for posting comments
    try:
        me = s.get("https://www.reddit.com/api/me.json", timeout=15)
        print(f"[relay] me.json status={me.status_code} len={len(me.content)} ct={me.headers.get('content-type','?')}")
        print(f"[relay] me.json body_start={me.text[:200]}")
        if me.status_code == 200 and me.text.strip().startswith("{"):
            data = me.json().get("data", {})
            _modhash = data.get("modhash", "")
            name = data.get("name", "?")
            print(f"[relay] Authenticated as: {name}, modhash={_modhash[:8]}...")
        else:
            print(f"[relay] me.json non-JSON response, skipping modhash")
    except Exception as e:
        print(f"[relay] me.json error: {e}")

    _session = s
    return s


def get_session() -> requests.Session:
    global _session
    if _session is None:
        build_session()
    return _session


def post_comment(post_url: str, comment_text: str) -> dict:
    m = re.search(r'/comments/([a-zA-Z0-9]+)/', post_url)
    if not m:
        return {"ok": False, "error": f"Cannot parse post ID from: {post_url}"}

    thing_id = "t3_" + m.group(1)
    s = get_session()

    resp = s.post(
        "https://www.reddit.com/api/comment",
        data={
            "thing_id": thing_id,
            "text": comment_text,
            "api_type": "json",
            "uh": _modhash,
        },
        headers={"X-Modhash": _modhash},
        timeout=20,
    )

    print(f"[relay] POST /api/comment status={resp.status_code} body={resp.text[:200]}")

    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        body = resp.json()
    except Exception:
        return {"ok": False, "error": f"Non-JSON: {resp.text[:200]}"}

    errors = body.get("json", {}).get("errors", [])
    if errors:
        return {"ok": False, "error": str(errors)}

    return {"ok": True, "thing_id": thing_id}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "user": REDDIT_USERNAME,
        "version": "cookies-v3",
        "has_cookies": bool(os.environ.get("REDDIT_COOKIES")),
    })


@app.route("/whoami")
def whoami():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {RELAY_SECRET}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        s = get_session()
        me = s.get("https://www.reddit.com/api/me.json", timeout=10)
        return jsonify({
            "http_status": me.status_code,
            "content_type": me.headers.get("content-type", "?"),
            "body_length": len(me.content),
            "body_start": me.text[:300],
            "modhash": _modhash[:8] if _modhash else "NOT_SET",
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/reply", methods=["POST"])
def reply():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {RELAY_SECRET}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()
    comment_text = body.get("comment_text", "").strip()

    if not post_url or not comment_text:
        return jsonify({"ok": False, "error": "post_url and comment_text required"}), 400

    try:
        result = post_comment(post_url, comment_text)
        return jsonify(result), (200 if result["ok"] else 502)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
