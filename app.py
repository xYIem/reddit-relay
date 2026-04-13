"""
Reddit Reply Relay
------------------
Deployed on Railway. Accepts POST /reply from WSL2 agent.
Uses browser cookies extracted from Windows Chrome (REDDIT_COOKIES env var).
No login needed — cookies persist for ~2 years.

Auth: Bearer token in Authorization header (RELAY_SECRET env var).
"""

import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

RELAY_SECRET    = os.environ["RELAY_SECRET"]
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "ylelvl")
REDDIT_UA       = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── Session (built from injected cookies, no login) ───────────────────────────
_session = None
_modhash = ""


def build_session():
    global _session, _modhash
    cookies_json = os.environ.get("REDDIT_COOKIES", "")
    if not cookies_json:
        raise RuntimeError("REDDIT_COOKIES env var not set")

    cookie_list = json.loads(cookies_json)
    s = requests.Session()
    s.headers.update({"User-Agent": REDDIT_UA})

    for c in cookie_list:
        # Encode value to latin-1 safe (requests HTTP header requirement)
        val = c["value"]
        try:
            val.encode("latin-1")
        except (UnicodeEncodeError, UnicodeDecodeError):
            import urllib.parse
            val = urllib.parse.quote(val, safe="")
        s.cookies.set(
            c["name"], val,
            domain=c.get("domain", ".reddit.com"),
            path=c.get("path", "/"),
        )

    # Fetch modhash (CSRF token) for comment posting
    me = s.get("https://www.reddit.com/api/me.json", timeout=15)
    print(f"[relay] me.json status={me.status_code}")
    if me.status_code == 200:
        data = me.json().get("data", {})
        _modhash = data.get("modhash", "")
        name = data.get("name", "?")
        print(f"[relay] Authenticated as: {name}, modhash={_modhash[:8]}...")
    else:
        print(f"[relay] me.json failed: {me.text[:200]}")

    _session = s
    return s


def get_session():
    global _session
    if _session is None:
        build_session()
    return _session


def post_comment(post_url: str, comment_text: str) -> dict:
    m = re.search(r'/comments/([a-zA-Z0-9]+)/', post_url)
    if not m:
        return {"ok": False, "error": f"Could not parse post ID from URL: {post_url}"}

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

    print(f"[relay] comment POST status={resp.status_code} body={resp.text[:200]}")

    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        body = resp.json()
    except Exception:
        return {"ok": False, "error": f"Non-JSON response: {resp.text[:200]}"}

    errors = body.get("json", {}).get("errors", [])
    if errors:
        return {"ok": False, "error": str(errors)}

    return {"ok": True, "thing_id": thing_id}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "user": REDDIT_USERNAME, "version": "cookies-v2", "has_cookies": bool(os.environ.get("REDDIT_COOKIES"))})


@app.route("/whoami")
def whoami():
    """Check which Reddit account is authenticated."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {RELAY_SECRET}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        s = get_session()
        me = s.get("https://www.reddit.com/api/me.json", timeout=10)
        return jsonify({"status": me.status_code, "data": me.json().get("data", {})})
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
