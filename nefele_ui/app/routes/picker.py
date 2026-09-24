from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, render_template, request, send_from_directory

from ..auth import DEMO_CANNED_PREVIEWS, DEMO_DATASET_NAMES
from ..services import vm_comms
from ..services.dataset_meta import (
    clear_job_id, read_job_id, read_scan_id, write_job_id,
)
from ..services.frames import clear_dir
from ..services.prompts import build_prompts, normalize_points, save_prompts
from ..services.results import read_model
from ..services.vm_comms import VmCommsError
from ..services.worker_client import run_preview_masks
from ._helpers import cfg, frames, json_err, json_ok

import logging
log = logging.getLogger(__name__)

bp = Blueprint("picker", __name__)


@bp.get("/pick")
def pick():
    return render_template("pick.html", frames=frames())


def _save_shared_fs(c, frame_path: Path, pts, labs, frame_idx: int):
    """Legacy path: write prompts.json and ask the worker for previews."""
    save_prompts(c.prompts_json, frame_path, pts, labs, frame_idx=frame_idx, obj_id=1)

    # Clear any previous preview images before asking for fresh ones.
    clear_dir(c.preview_dir, recursive=True)

    preview_files, worker_error = run_preview_masks(
        c.worker_url,
        c.worker_timeout,
        input_dir=c.input_dir,
        out_root=c.out_root,
        prompts_json=c.prompts_json,
        preview_dir=c.preview_dir,
        num_frames=6,
    )
    if worker_error and not preview_files:
        return json_err(worker_error, http=502)
    preview_urls = [f"/preview/{name}" for name in preview_files]
    return json_ok(path=str(c.prompts_json), previews=preview_urls)


def _save_vm_comms(c, frame_path: Path, pts, labs, frame_idx: int):
    """Decoupled path: create a vm_comms job and return immediately.

    Returns ``{"ok": true, "job_id": "...", "pending": true}`` so the browser
    can poll ``GET /save/status?job_id=...`` instead of blocking here.
    The preview images only exist once the SAM2 worker sets status=preview_ready,
    which can take tens of seconds — we must not block the HTTP request.

    Guard against orphan jobs: if a previous job for this scan is already in
    ``preview_ready`` waiting for a Confirm/Redo decision, we surface those
    previews immediately (no need to create a new job).

    Demo datasets never create a real job at all: there's no live fast-path
    on the worker, so a genuine job would take real SAM2 processing time
    regardless of dataset. Instead, always return a fixed set of previews
    from a real, already-completed run of that same dataset — the whole
    point of a demo account is that it looks and feels instant.
    """
    canned = DEMO_CANNED_PREVIEWS.get(c.dataset_name)
    if canned is not None:
        preview_urls = [f"/hestia-preview?{urlencode({'url': u})}" for u in canned]
        return json_ok(
            job_id="demo",
            previews=preview_urls,
            pending=False,
            resumed=True,
            message=(
                "Demo dataset — showing an existing example instead of "
                "running SAM2 on these points."
            ),
        )

    points_json = build_prompts(frame_path, pts, labs, frame_idx=frame_idx, obj_id=1)
    scan_id = read_scan_id(c.input_dir)
    model = read_model(c.in_mnt, c.dataset_name)

    pending = vm_comms.find_pending_for_scan(scan_id, c.dataset_name) if scan_id else None
    if pending and pending.preview:
        write_job_id(c.indexed_dir, pending.job_id)
        preview_urls = [
            f"/hestia-preview?{urlencode({'url': u})}" for u in pending.preview
        ]
        return json_ok(
            job_id=pending.job_id,
            previews=preview_urls,
            pending=False,
            resumed=True,
            message=(
                "Existing preview found — showing it instead of re-generating. "
                "Confirm to proceed or Redo to re-generate."
            ),
        )

    job = vm_comms.create_job(scan_id, c.dataset_name, model, points_json)
    write_job_id(c.indexed_dir, job.job_id)
    log.info("picker: job created job_id=%s — returning to browser for polling", job.job_id)
    # Return immediately. The browser polls /save/status until preview_ready.
    return json_ok(job_id=job.job_id, pending=True, previews=[])


@bp.post("/save")
def save():
    c = cfg()
    fr = frames()
    if not fr:
        return json_err("No frames found", http=400)

    try:
        data = request.get_json(force=True, silent=True) or {}
        try:
            frame_idx = int(data.get("frame_idx", 0))
        except (TypeError, ValueError):
            frame_idx = 0
        if frame_idx < 0 or frame_idx >= len(fr):
            frame_idx = 0
        pts, labs = normalize_points(data.get("points", {}), frame_idx=frame_idx)
        frame_path = Path(fr[frame_idx])

        if c.uses_vm_comms:
            return _save_vm_comms(c, frame_path, pts, labs, frame_idx)
        return _save_shared_fs(c, frame_path, pts, labs, frame_idx)
    except VmCommsError as e:
        return json_err(str(e), http=502)
    except Exception as e:
        return json_err(str(e), http=500)


@bp.get("/save/status")
def save_status():
    """Poll until the SAM2 worker has uploaded preview images.

    Returns:
      pending=True   — job is still being processed (points_submitted / previewing)
      pending=False  — previews are ready; ``previews`` contains the proxy URLs
      error=<msg>    — job ended in error or cancellation
    """
    job_id = request.args.get("job_id", "").strip()
    if not job_id:
        return json_err("job_id required", http=400)
    try:
        job = vm_comms.get_job(job_id)
    except VmCommsError as e:
        return json_err(str(e), http=502)

    if job.status == vm_comms.STATUS_CANCELLED:
        return json_err(
            job.error or "job was cancelled", http=200
        )
    if job.status == vm_comms.STATUS_ERROR:
        # Don't surface the error immediately — a new job for this dataset may
        # already be queued and will replace this one shortly.  Keep polling so
        # the UI shows the loading spinner until the new preview is ready.
        return json_ok(pending=True, status=job.status, previews=[])
    if job.status != vm_comms.STATUS_PREVIEW_READY:
        return json_ok(pending=True, status=job.status, previews=[])

    preview_urls = [
        f"/hestia-preview?{urlencode({'url': u})}" for u in (job.preview or [])
    ]
    return json_ok(pending=False, status=job.status, previews=preview_urls, job_id=job_id)


@bp.post("/confirm")
def confirm():
    c = cfg()
    if c.dataset_name in DEMO_DATASET_NAMES:
        # No real job was ever created for a demo dataset's preview — see
        # _save_vm_comms. Nothing to confirm against.
        return json_ok(msg="confirmed")
    if c.uses_vm_comms:
        job_id = read_job_id(c.indexed_dir)
        if not job_id:
            return json_err("No active job to confirm — save points first", http=409)
        try:
            vm_comms.submit_instructions(job_id, vm_comms.DECISION_CONFIRM)
        except VmCommsError as e:
            return json_err(str(e), http=502)
        return json_ok(msg="confirmed")

    try:
        cfg().done_flag.touch()
        return json_ok(msg="confirmed")
    except OSError as e:
        return json_err(str(e), http=500)


@bp.post("/restart")
def restart():
    c = cfg()
    if c.dataset_name in DEMO_DATASET_NAMES:
        return json_ok(msg="restarted")
    if c.uses_vm_comms:
        # Abandon the current job; the next /save creates a fresh one.
        try:
            job_id = read_job_id(c.indexed_dir)
            if job_id:
                vm_comms.submit_instructions(job_id, vm_comms.DECISION_REDO)
        except VmCommsError:
            pass  # best-effort — abandoning locally is what matters
        clear_job_id(c.indexed_dir)
        clear_dir(c.preview_dir)
        return json_ok(msg="restarted")

    try:
        c.prompts_json.unlink(missing_ok=True)
        clear_dir(c.preview_dir)
        c.done_flag.unlink(missing_ok=True)
        c.use_existing_flag.unlink(missing_ok=True)
        return json_ok(msg="restarted")
    except OSError as e:
        return json_err(str(e), http=500)


@bp.get("/frame")
def frame():
    fr = frames()
    try:
        idx = int(request.args.get("i", "0"))
    except ValueError:
        return json_err("Invalid frame index", http=400)
    if idx < 0 or idx >= len(fr):
        return json_err("Frame index out of range", http=404)
    fp = Path(fr[idx])
    return send_from_directory(fp.parent, fp.name)
