"""Show the reconstruction output files and let the user download them.

Two transports, chosen by ``COMMS_BACKEND`` (see ``cfg.uses_vm_comms``):

* ``shared_fs`` — legacy: discover meshes on the mounted SuGaR/PGSR output
  directories and serve them off local disk.
* ``vm_comms``  — decoupled (diagram step 13): fetch the finished mesh from the
  HESTIA ``/reconstructions`` API and stream it to the browser.
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, abort, redirect, render_template, send_file, url_for

from ..services.dataset_meta import read_job_id, read_scan_id
from ..services.hestia import fetch_file, get_reconstruction
from ..services.pipeline import PipelineStatus, read_status, status_from_job
from ..services.results import (
    friendly_name, list_outputs, list_pgsr_outputs, read_model, read_patched,
)
from ..services.vm_comms import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_ERROR,
    VmCommsError,
    cancel_job,
    get_job,
)
from ._helpers import cfg, json_err, json_ok

bp = Blueprint("results", __name__)


@bp.get("/pipeline/status")
def pipeline_status():
    """JSON status of the auto-triggered pipeline for the active dataset."""
    c = cfg()
    if not c.is_configured:
        return json_err("No dataset configured", http=400)

    if c.uses_vm_comms:
        job_id = read_job_id(c.indexed_dir)
        if not job_id:
            status = PipelineStatus(dataset=c.dataset_name)
        else:
            try:
                status = status_from_job(get_job(job_id), c.dataset_name)
            except VmCommsError as e:
                status = PipelineStatus(
                    dataset=c.dataset_name, status="error", error=str(e)
                )
    else:
        status = read_status(c.indexed_dir, c.dataset_name)
    return json_ok(**status.to_dict())


@bp.get("/results")
def page():
    c = cfg()
    if not c.is_configured:
        return redirect(url_for("welcome.welcome"))
    return render_template(
        "results.html",
        ds=c.display_name,
        model=read_model(c.in_mnt, c.dataset_name),
        uses_vm_comms=c.uses_vm_comms,
    )


@bp.post("/results/cancel")
def cancel_pipeline():
    """Explicit user-initiated cancel: stop the running vm_comms job for the
    active dataset. Only available in vm_comms mode — in shared_fs mode the
    pipeline is still triggered via filesystem flags (no equivalent abort).
    """
    c = cfg()
    if not c.is_configured:
        return json_err("No dataset configured", http=400)
    if not c.uses_vm_comms:
        return json_err("Cancel is only supported in vm_comms mode", http=400)

    job_id = read_job_id(c.indexed_dir)
    if not job_id:
        return json_err("No active job for this dataset.", http=404)
    try:
        fired = cancel_job(job_id)
    except VmCommsError as e:
        return json_err(f"HESTIA cancel failed: {e}", http=502)
    return json_ok(job_id=job_id, cancelled=fired)


# --- vm_comms (HESTIA /reconstructions) ------------------------------------
# Each reconstruction record carries up to four public URLs; map them to the
# same file-entry shape the front-end expects from the shared_fs path.
_REC_URL_FIELDS = [
    ("public_url_model", "model", "obj"),
    ("public_url_material", "material", "mtl"),
    ("public_url_texture", "texture", "texture"),
    ("public_url_glb", "glb", "glb"),
]


def _reconstruction_files(record: dict) -> list[dict]:
    """Turn a HESTIA reconstruction record into front-end file entries."""
    import datetime as _dt
    # Parse the ISO timestamp HESTIA returns so the front-end can show a real date.
    ts = 0
    raw_ts = record.get("timestamp")
    if raw_ts:
        try:
            ts = int(_dt.datetime.fromisoformat(raw_ts).timestamp())
        except (ValueError, TypeError):
            ts = 0

    files: list[dict] = []
    for field, key, kind in _REC_URL_FIELDS:
        url = record.get(field)
        if not url:
            continue
        name = os.path.basename(urlparse(url).path) or f"{key}.{kind}"
        files.append({
            "name": name, "relative": key, "kind": kind,
            "url": url, "size": 0, "modified": ts,
        })
    return files


def _fetch_reconstruction(c):
    """Fetch the reconstruction record for the active dataset's scan (may raise)."""
    scan_id = read_scan_id(c.input_dir)
    return get_reconstruction(scan_id) if scan_id else None


# --- shared_fs (mounted SuGaR / PGSR output dirs) --------------------------
def _results_root(c):
    model = read_model(c.in_mnt, c.dataset_name)
    if model == "pgsr":
        return c.pgsr_results_root, model
    if model == "fastpgsr":
        return c.fastpgsr_results_root, model
    return c.sugar_results_root, model


@bp.get("/results/files")
def files_json():
    """JSON listing for the front-end poll loop."""
    c = cfg()
    if not c.is_configured:
        return json_err("No dataset configured", http=400)

    if c.uses_vm_comms:
        try:
            rec = _fetch_reconstruction(c)
        except Exception as e:
            return json_err(f"HESTIA error: {e}", http=502)

        # Guard against showing a stale reconstruction from a previous run
        # *while a new one is still actually in flight*: if there's an active
        # job that hasn't reached a terminal status yet, only accept a
        # reconstruction uploaded AFTER that job started. ISO-8601 strings
        # sort correctly as plain strings, so a simple comparison is safe.
        #
        # Once the job reaches done/error/cancelled, this check is skipped
        # entirely — a terminal job is asserting "whatever reconstruction
        # exists now for this scan_id is the answer", even if that record
        # predates the job itself. This matters for scan_ids that intentionally
        # reuse an existing reconstruction instead of always producing a new
        # one (e.g. a demo dataset's worker short-circuiting real processing);
        # without this, a legitimately-finished job could have its own
        # correct result hidden purely because of a timestamp technicality.
        if rec:
            job_id = read_job_id(c.indexed_dir)
            if job_id:
                try:
                    job = get_job(job_id)
                    if job.status not in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED):
                        job_created_at = job.raw.get("created_at", "")
                        rec_ts = rec.get("timestamp", "")
                        if rec_ts and job_created_at and rec_ts < job_created_at:
                            rec = None  # reconstruction pre-dates current job
                except VmCommsError:
                    pass  # can't verify — show whatever HESTIA has

        files = _reconstruction_files(rec) if rec else []
        return json_ok(
            dataset=c.dataset_name,
            model=read_model(c.in_mnt, c.dataset_name),
            ready=bool(files),
            files=files,
        )

    root, model = _results_root(c)
    if model == "pgsr":
        files = list_pgsr_outputs(root, c.dataset_name)
    else:
        files = list_outputs(root, c.dataset_name)
    return json_ok(
        dataset=c.dataset_name,
        model=model,
        ready=bool(files),
        files=[asdict(f) for f in files],
    )


def _safe_resolve(root: Path, relative: str) -> Path:
    """Resolve ``root/relative`` and guarantee the result stays inside ``root``."""
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    return target


@bp.get("/results/file/<path:relative>")
def download_one(relative: str):
    c = cfg()
    if not c.is_configured:
        return json_err("No dataset configured", http=400)

    if c.uses_vm_comms:
        try:
            rec = _fetch_reconstruction(c)
        except Exception as e:
            return json_err(f"HESTIA error: {e}", http=502)
        if not rec:
            abort(404)
        match = next(
            (f for f in _reconstruction_files(rec) if f["relative"] == relative), None
        )
        if not match:
            abort(404)
        try:
            data = fetch_file(match["url"])
        except Exception as e:
            return json_err(f"HESTIA download failed: {e}", http=502)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=match["name"])

    root, model = _results_root(c)
    target = _safe_resolve(root, relative)
    data = read_patched(target, c.dataset_name)
    dl_name = target.name if model == "pgsr" else friendly_name(target.name, c.dataset_name)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=dl_name)


@bp.get("/results/zip")
def download_zip():
    c = cfg()
    if not c.is_configured:
        return json_err("No dataset configured", http=400)

    if c.uses_vm_comms:
        try:
            rec = _fetch_reconstruction(c)
        except Exception as e:
            return json_err(f"HESTIA error: {e}", http=502)
        files = _reconstruction_files(rec) if rec else []
        if not files:
            return json_err("No results to download yet.", http=404)
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.writestr(f["name"], fetch_file(f["url"]))
        except Exception as e:
            return json_err(f"HESTIA download failed: {e}", http=502)
        buf.seek(0)
        return send_file(
            buf, mimetype="application/zip", as_attachment=True,
            download_name=f"{c.dataset_name}.zip",
        )

    root, model = _results_root(c)
    if model == "pgsr":
        files = list_pgsr_outputs(root, c.dataset_name)
    else:
        files = list_outputs(root, c.dataset_name)
    if not files:
        return json_err("No results to download yet.", http=404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            src = _safe_resolve(root, f.relative)
            arc_name = src.name if model == "pgsr" else f.name
            zf.writestr(arc_name, read_patched(src, c.dataset_name))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{c.dataset_name}.zip",
    )
