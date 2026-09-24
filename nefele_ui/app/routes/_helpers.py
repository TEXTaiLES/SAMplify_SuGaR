"""Shared route helpers."""

from __future__ import annotations

import dataclasses
from typing import Any

from flask import current_app, jsonify, session

from ..auth import current_user_id, is_demo_user
from ..config import DEMO_DATASET_NAME, Config, read_user_active_dataset
from ..services.frames import resolve_frames


def cfg() -> Config:
    """Build this request's effective Config: the process-wide base settings
    (mounts, worker URL, ...) plus the current visitor's own dataset choice.

    Resolution order for the dataset name:
      1. ``DATASET_NAME`` env var — operator pin for single-tenant deployments.
      2. this browser session's own choice (set by ``app.activate_dataset``).
      3. this user's last choice, persisted per-user on disk (so it survives
         across their other browsers/devices/logins).
      4. the shared demo dataset, but only as a *default* for the one demo
         account (auth.is_demo_user) — they can still start a fresh dataset
         normally, which then wins here via step 2/3 on later visits.
      5. unset -> setup mode.

    Every visitor gets their own dataset_name this way — nothing here is
    cached on the app or read from a global file, so one visitor's choice can
    never leak into another's.
    """
    base: Config = current_app.config["BASE_CONFIG"]
    if base.dataset_name:
        return base

    uid = current_user_id()
    name = session.get("dataset_name", "") or read_user_active_dataset(base.in_mnt, uid) or ""
    if not name and is_demo_user():
        name = DEMO_DATASET_NAME
    c = dataclasses.replace(base, user_id=uid, dataset_name=name)
    if c.is_configured:
        c.ensure_dirs()
    return c


def frames() -> list[str]:
    c = cfg()
    if not c.is_configured:
        return []
    return resolve_frames(c.input_dir, c.index_suffix)


def json_ok(**payload: Any):
    return jsonify({"ok": True, **payload})


def json_err(msg: str, http: int = 400):
    return jsonify({"ok": False, "error": msg}), http
