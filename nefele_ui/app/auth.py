"""Directus-backed auth gate.

Pattern: long-lived refresh_token in an HttpOnly cookie, short-lived
access_token cached in the Flask session. Each request checks the session
first — only when the cached access_token is missing or expired do we call
Directus ``/auth/refresh``. This avoids burning a refresh per poll request,
which (a) wastes round-trips and (b) races whenever two parallel requests
(e.g. ``results.js`` polling ``/results/files`` and ``/pipeline/status`` in
parallel) try to use the same refresh_token at the same time and Directus
rotates it — one wins, the other gets 401 and bounces to login.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote

import requests
from flask import g, redirect, request, session

# Match the original auth.py — hardcoded so an empty/unset env var can never
# produce a relative URL and cause ERR_TOO_MANY_REDIRECTS on /welcome.
DIRECTUS_URL = "https://textailes.athenarc.gr"
COOKIE_NAME = "textailes_refresh_token"
LOGIN_URL = "https://textailes.athenarc.gr/archive/user/login"
APP_BASE = "https://nephele.textailes.athenarc.gr"
#local
# APP_BASE = "http://nephele.textailes.athenarc.gr:8093"

PUBLIC_PATHS = {"/health", "/hestia-preview"}
PUBLIC_PREFIXES = ("/static/",)

# Refresh slightly before the token's stated expiry so a long-running request
_REFRESH_SKEW_SEC = 30


def redirect_to_login():
    # Strip the incoming ``redirect_url`` query param so chained redirects
    # don't keep stacking it (was: /welcome?redirect_url=http://nephele/welcome?redirect_url=... → ERR_TOO_MANY_REDIRECTS).
    from urllib.parse import urlencode
    cleaned = {k: v for k, v in request.args.items() if k != "redirect_url"}
    next_path = request.path
    if cleaned:
        next_path = f"{next_path}?{urlencode(cleaned)}"
    redirect_url = APP_BASE.rstrip("/") + next_path
    return redirect(f"{LOGIN_URL}?redirect_url={quote(redirect_url, safe=':/?=&')}")


def directus_refresh(refresh_token: str):
    """Exchange a refresh_token for fresh tokens. Returns the ``data`` dict
    (``{access_token, refresh_token, expires}``) or None on failure.

    Directus returns ``expires`` in **milliseconds**.
    """
    try:
        r = requests.post(
            f"{DIRECTUS_URL}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=8,
        )
        if r.status_code != 200:
            print("refresh failed:", r.status_code, r.text[:200], flush=True)
            return None
        data = r.json()
        return data.get("data") or data
    except Exception as e:
        print("refresh exception:", e, flush=True)
        return None


def _store_tokens(tokens: dict) -> None:
    """Cache access_token + expiry in the signed-cookie session, and stash a
    rotated refresh_token (if any) for after_request to push back as a cookie.
    """
    access = tokens.get("access_token")
    expires_ms = tokens.get("expires")  # Directus returns ms; may also be None
    try:
        expires_sec = float(expires_ms) / 1000.0 if expires_ms else 900.0
    except (TypeError, ValueError):
        expires_sec = 900.0
    session["access_token"] = access
    session["access_expires_at"] = time.time() + expires_sec
    g.access_token = access

    new_refresh = tokens.get("refresh_token")
    if new_refresh and new_refresh != request.cookies.get(COOKIE_NAME):
        g.new_refresh_token = new_refresh


def _cached_access_token() -> str | None:
    """Return the session-cached access_token if it is still valid, else None."""
    tok = session.get("access_token")
    exp = session.get("access_expires_at")
    if not tok or not exp:
        return None
    if time.time() >= float(exp) - _REFRESH_SKEW_SEC:
        return None
    return tok


# Directus accounts that default to a pre-built demo dataset instead of
# starting in setup mode. Each gets its OWN dataset_name — even though they
# share the same underlying HESTIA scan (dress_test_hestia) and are free to
# reuse its reconstruction — so that two demo accounts using the Picker at
# the same time never share job/prompts state: that state is keyed entirely
# by dataset_name (see routes._helpers.cfg / config.py), and giving two
# different visitors the identical dataset_name would reopen exactly the
# cross-user collision the rest of this isolation work fixed.
DEMO_DATASETS = {
    "mednight.athenarc@sample.com": "u2a6f4bf6_dress_demo",
    "mednight.certh@sample.com": "uffb726bf_dress_demo",
}

# The dataset_name values from DEMO_DATASETS, as a set for membership checks
# (e.g. routes.results bypassing its staleness guard for these datasets —
# a demo account should always see their already-ready result, even while
# they're separately playing with the Picker on a brand new, still-running
# job for the same dataset).
DEMO_DATASET_NAMES = set(DEMO_DATASETS.values())

# Pins each demo dataset to ONE specific, known-correct reconstruction
# object_id, rather than trusting "whatever's newest for this scan_id".
# dress_test_hestia is intentionally reused by both demo datasets AND an
# unrelated real production dataset (scan_dress_test_fast_pgsr) — confirmed
# live: 5 reconstruction rows share this scan_id, only one of them
# (b3652139-...) is the actual demo mesh (u2a6f4bf6_dress_demo_tsdf_fusion_
# post.obj), the other 4 are the production dataset's own runs. "Newest"
# happens to be correct right now only because the production dataset
# hasn't been reprocessed more recently than the demo's own run — that's not
# safe to rely on going forward. See services.hestia.get_reconstruction_by_id.
DEMO_RECONSTRUCTION_IDS = {
    "u2a6f4bf6_dress_demo": "b3652139-bf07-48dc-9674-fa90cd0c91b0",
    "uffb726bf_dress_demo": "b3652139-bf07-48dc-9674-fa90cd0c91b0",
}

# Canned SAM2 preview image URLs the Picker returns for a demo dataset,
# regardless of what points a demo account actually clicks. There is no live
# fast-path on the worker — a real vm_comms job would still take real
# processing time — so for demo accounts we skip creating one at all and
# just reuse the previews from a genuine, already-completed run of that same
# dataset. These are real HESTIA storage URLs, proxied through
# /hestia-preview the same way a real job's previews would be.
DEMO_CANNED_PREVIEWS = {
    "u2a6f4bf6_dress_demo": [
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000000.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000048.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000104.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000185.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000225.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/7e17a493-70d1-4dfc-847a-f317c0192f65/000399.jpg",
    ],
    "uffb726bf_dress_demo": [
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000000.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000108.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000117.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000179.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000330.jpg",
        "https://api.textailes.athenarc.gr/storage/nefele/3481f85b-afc3-4ec0-8eb9-85806505f997/000428.jpg",
    ],
}


def _ensure_user_id(access_token: str) -> None:
    """Cache the Directus account id + email in the session so per-user
    isolation (active dataset, dataset naming) can key off a real identity
    instead of an anonymous per-browser session. No-op once already cached —
    neither value changes for a given account."""
    if session.get("user_id"):
        return
    try:
        r = requests.get(
            f"{DIRECTUS_URL}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,email"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json().get("data") or {}
            uid = data.get("id")
            if uid:
                session["user_id"] = uid
                session["user_email"] = data.get("email") or ""
    except Exception as e:
        print("users/me fetch failed:", e, flush=True)


def current_user_id() -> str:
    """Stable identifier for the current visitor, used to namespace their
    datasets/state so different users never collide or see each other's data.

    With auth enabled this is the Directus account id. Without auth (local /
    dev deployments) there is no real identity, so fall back to a random id
    persisted in the session cookie — stable for that browser, isolated from
    every other browser.
    """
    uid = session.get("user_id")
    if uid:
        return uid
    uid = session.get("anon_id")
    if not uid:
        import uuid
        uid = f"anon-{uuid.uuid4().hex[:16]}"
        session["anon_id"] = uid
    return uid


def demo_dataset_for_current_user() -> str:
    """The demo dataset this visitor should default to, or '' if they're not
    one of the demo accounts. Auth-only: there's no email to check without
    it, so this is always '' when auth is disabled."""
    return DEMO_DATASETS.get(session.get("user_email", ""), "")


def init_auth(app):
    @app.before_request
    def auth_gate():
        path = request.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return None

        g.new_refresh_token = None

        # Fast path: a valid cached access_token means no Directus round-trip.
        tok = _cached_access_token()
        if tok:
            g.access_token = tok
            _ensure_user_id(tok)
            return None

        # Slow path: either first hit, or the cached token expired. We need
        # the refresh_token cookie to mint a new access_token.
        refresh_token = request.cookies.get(COOKIE_NAME)
        if not refresh_token:
            session.clear()
            return redirect_to_login()

        tokens = directus_refresh(refresh_token)
        if not tokens:
            # Refresh failed → drop any stale session and force a real login.
            session.clear()
            return redirect_to_login()

        _store_tokens(tokens)
        _ensure_user_id(tokens.get("access_token") or "")
        return None

    @app.after_request
    def apply_refresh_cookie(resp):
        new_refresh = getattr(g, "new_refresh_token", None)
        if not new_refresh:
            return resp

        host = request.host.split(":")[0]
        domain = ".textailes.athenarc.gr" if host.endswith("textailes.athenarc.gr") else None
        is_https = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"

        resp.set_cookie(
            COOKIE_NAME,
            new_refresh,
            httponly=True,
            secure=is_https,
            samesite="Lax",
            path="/",
            domain=domain,
        )
        return resp
