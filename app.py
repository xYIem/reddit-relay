"""
Reddit Reply Relay
------------------
Deployed on Railway (clean IP). Accepts POST /reply from WSL2 agent,
logs into Reddit via old.reddit.com (no OAuth app needed), posts the comment.

Auth: bearer token in Authorization header (RELAY_SECRET env var).
"""

import os
import time
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

RELAY_SECRET    = os.environ["RELAY_SECRET"]
REDDIT_USERNAME = os.environ["REDDIT_USERNAME"]
REDDIT_PASSWORD = os.environ["REDDIT_PASSWORD"]
REDDIT_UA       = f"YlemRelayBot/1.0 by /u/{REDDIT_USERNAME}"

# ── Session cache ─────────────────────────────────────────────────────────────
_session = None  # requests.Session | None
_session_expiry: float = 0
SESSION_TTL = 55 * 60  # 55 min


def get_session() -> requests.Session:
    global _session, _session_expiry
    if _session and time.time() < _session_expiry:
        return _session

    s = requests.Session()
    s.headers.update({"User-Agent": REDDIT_UA})

    # Login via old.reddit.com (plain HTTP form, no OAuth)
    login_url = f"https://old.reddit.com/api/login/{REDDIT_USERNAME}"
    resp = s.post(login_url, data={
        "user": REDDIT_USERNAME,
        "passwd": REDDIT_PASSWORD,
        "api_type": "json",
        "rem": "true",
    }, timeout=15)

    print(f"[relay] login status={resp.status_code} body={resp.text[:200]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit login HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"Reddit login non-JSON response: {resp.text[:300]}")
    errors = body.get("json", {}).get("errors", [])
    if errors:
        raise RuntimeError(f"Reddit login errors: {errors}")

    # Grab modhash for CSRF
    me_resp = s.get("https://www.reddit.com/api/me.json", timeout=10)
    print(f"[relay] me.json status={me_resp.status_code} body={me_resp.text[:100]}")
    if me_resp.status_code != 200:
        raise RuntimeError(f"me.json HTTP {me_resp.status_code}: {me_resp.text[:200]}")
    me = me_resp.json()
    modhash = me.get("data", {}).get("modhash", "")
    s.headers.update({"X-Modhash": modhash})

    _session = s
    _session_expiry = time.time() + SESSION_TTL
    print(f"[relay] Logged in as {REDDIT_USERNAME}, modhash={modhash[:8]}...")
    return s


def post_comment(post_url: str, comment_text: str) -> dict:
    import re
    m = re.search(r'/comments/([a-z0-9]+)/', post_url)
    if not m:
        return {"ok": False, "error": "Could not parse post ID from URL"}

    thing_id = "t3_" + m.group(1)
    s = get_session()

    resp = s.post("https://www.reddit.com/api/comment", data={
        "thing_id": thing_id,
        "text": comment_text,
        "api_type": "json",
    }, timeout=15)

    body = resp.json()
    errors = body.get("json", {}).get("errors", [])
    if errors:
        return {"ok": False, "error": str(errors)}
    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}"}

    return {"ok": True}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "user": REDDIT_USERNAME})


@app.route("/debug-login")
def debug_login():
    """Test Reddit login and return raw response — remove after debugging."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {RELAY_SECRET}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    import requests as req_lib
    s = req_lib.Session()
    s.headers.update({"User-Agent": REDDIT_UA})
    try:
        r = s.post(
            f"https://old.reddit.com/api/login/{REDDIT_USERNAME}",
            data={"user": REDDIT_USERNAME, "passwd": REDDIT_PASSWORD, "api_type": "json", "rem": "true"},
            timeout=15,
        )
        return jsonify({"status": r.status_code, "body": r.text[:500], "cookies": {c.name: c.value for c in s.cookies}})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/reply", methods=["POST"])
def reply():
    # Auth check
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
        status = 200 if result["ok"] else 502
        return jsonify(result), status
    except Exception as e:
        # Session may be stale — invalidate and retry once
        global _session, _session_expiry
        _session = None
        _session_expiry = 0
        try:
            result = post_comment(post_url, comment_text)
            status = 200 if result["ok"] else 502
            return jsonify(result), status
        except Exception as e2:
            return jsonify({"ok": False, "error": str(e2)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
