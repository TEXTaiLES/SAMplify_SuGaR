"""HESTIA integration routes — browse scans, load images, upload results."""

from __future__ import annotations

import threading
from pathlib import Path

from flask import Blueprint, render_template, request

from .. import activate_dataset
from ..auth import current_user_id
from ..services.dataset_meta import write_scan_meta
from ..services.hestia import download_scan, list_scans, upload_reconstruction
from ..services.pipeline import request_kill
from ..services.uploads import (
    owns_dataset,
    sanitize_dataset_name,
    user_scoped_name,
    write_user_active_dataset,
)
from ._helpers import cfg, json_err, json_ok

bp = Blueprint("hestia", __name__)

# Track active downloads so the UI can poll progress. Keyed by the (already
# per-user-namespaced) dataset name rather than scan_id — HESTIA scan_id is
# shared institutional data, so two different users loading the *same* scan
# concurrently must still get their own independent progress entry.
# { dataset_name: {"status": "downloading"|"done"|"error", "downloaded": N, "error": str} }
_downloads: dict = {}
_downloads_lock = threading.Lock()


@bp.get("/hestia")
def page():
    return render_template("hestia.html")


@bp.get("/hestia/scans")
def scans_json():
    """Return JSON list of unique scans from HESTIA robot-images."""
    try:
        scans = list_scans()
    except Exception as e:
        return json_err(f"HESTIA API error: {e}", http=502)
    return json_ok(
        scans=[
            {
                "scan_id": s.scan_id,
                "image_count": s.image_count,
                "timestamp": s.timestamp,
                "sample_filename": s.sample_filename,
            }
            for s in scans
        ]
    )


@bp.post("/hestia/load")
def load_scan():
    """Start downloading a scan in the background; return immediately.

    ``scan_id`` names a HESTIA scan, which is shared institutional data — any
    user may load it. The local ``dataset_name`` this becomes is namespaced
    per user so that two people loading the same scan never share a download,
    a local dataset folder, or (later) a vm_comms job/preview.
    """
    data = request.get_json(silent=True) or request.form
    scan_id = (data.get("scan_id") or "").strip()
    if not scan_id:
        return json_err("scan_id required", http=400)

    model = (data.get("model") or "sugar").strip()
    if model not in ("sugar", "pgsr", "fastpgsr"):
        model = "sugar"

    uid = current_user_id()
    # Use caller-supplied name if valid, else fall back to auto-name.
    custom_name = (data.get("dataset_name") or "").strip()
    raw_name = custom_name if sanitize_dataset_name(custom_name) else f"scan_{scan_id[:8]}"
    dataset_name = user_scoped_name(uid, raw_name)

    with _downloads_lock:
        if _downloads.get(dataset_name, {}).get("status") == "downloading":
            return json_ok(status="downloading", message="Already in progress")
        _downloads[dataset_name] = {"status": "downloading", "downloaded": 0, "error": None}

    c = cfg()
    request_kill(c.in_mnt)
    dest = c.in_mnt / dataset_name
    in_mnt = c.in_mnt

    def _run():
        count = 0

        def _progress(n):
            with _downloads_lock:
                _downloads[dataset_name]["downloaded"] = n

        try:
            count = download_scan(scan_id, dest, on_progress=_progress)
        except Exception as e:
            with _downloads_lock:
                _downloads[dataset_name] = {"status": "error", "downloaded": count, "error": str(e)}
            return

        # Persist scan id + model choice alongside the dataset so the picker
        # can build a vm_comms job for it later.
        try:
            write_scan_meta(dest, scan_id, model)
        except Exception:
            pass

        # Persist this user's choice on disk. This runs in a background
        # thread with no request context, so it can't touch Flask's
        # session — /hestia/load/status finishes the activation (session +
        # per-user file) once it observes status == "done", back in a real
        # request context.
        try:
            write_user_active_dataset(in_mnt, uid, dataset_name)
        except OSError:
            pass
        with _downloads_lock:
            _downloads[dataset_name] = {"status": "done", "downloaded": count, "error": None}

    threading.Thread(target=_run, daemon=True).start()
    return json_ok(status="downloading", dataset=dataset_name, scan_id=scan_id)


@bp.get("/hestia/load/status")
def load_status():
    """Poll download progress for a given (per-user) dataset name.

    Runs in a real request context, unlike the background download thread,
    so this is also where a finished download gets activated for the current
    visitor's session — session state can't be touched from that thread.
    """
    dataset_name = request.args.get("dataset", "").strip()
    uid = current_user_id()
    if not dataset_name or not owns_dataset(uid, dataset_name):
        return json_err("No download found for that dataset", http=404)
    with _downloads_lock:
        info = _downloads.get(dataset_name)
    if not info:
        return json_err("No download found for that dataset", http=404)
    if info["status"] == "done":
        activate_dataset(dataset_name)
    return json_ok(**info, dataset=dataset_name)


@bp.post("/hestia/upload")
def upload_result():
    """Upload a finished reconstruction OBJ to HESTIA /reconstructions."""
    data = request.get_json(silent=True) or request.form
    scan_id = (data.get("scan_id") or "").strip()
    obj_path = Path((data.get("obj_path") or "").strip())
    mtl_path_raw = (data.get("mtl_path") or "").strip()
    tex_path_raw = (data.get("texture_path") or "").strip()

    if not scan_id or not obj_path:
        return json_err("scan_id and obj_path are required", http=400)
    if not obj_path.is_file():
        return json_err(f"obj_path not found: {obj_path}", http=404)

    mtl_path = Path(mtl_path_raw) if mtl_path_raw else None
    tex_path = Path(tex_path_raw) if tex_path_raw else None

    try:
        result = upload_reconstruction(scan_id, obj_path, mtl_path, tex_path)
    except Exception as e:
        return json_err(f"Upload failed: {e}", http=502)
    return json_ok(**result)
