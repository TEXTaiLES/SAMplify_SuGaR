#!/usr/bin/env python3
"""SAM2 worker — vm_comms poller.

The decoupled counterpart to ``worker_server.py``. Instead of waiting for the
UI to call it over HTTP (which required a shared filesystem), this polls the
HESTIA ``vm_comms`` API for jobs, renders preview masks, waits for the user's
instructions, runs the reconstruction pipeline, and reports progress back.

Implements the worker side of the vm_comms contract — steps 3, 5, 6, 9, 10, 12
of ``docs/vm_comms_contract.md`` (the contract doc lives in the nefele_ui
repo). The UI side is ``nefele_ui/app/services/vm_comms.py``.

Placement: this runs as a HOST process on the SAM VM — NOT inside a
container. ``run_pipeline.sh`` is a host-level orchestrator (it does
``docker compose up/exec``), and preview is rendered inside the GPU ``sam2``
container via ``docker compose exec``. The poller host needs only Python +
``requests`` + the docker CLI; the GPU stays in the containers it drives.

Run:

    python3 SAMplify_SuGaR/SAM2/app/worker_poller.py

Dispatch modes — chosen automatically:
* ``KAFKA_BROKER`` *set*    → Kafka consumer of ``nefele_job_created`` (push).
* ``KAFKA_BROKER`` *unset*  → HTTP polling of ``POST /nefele/claim`` (default).

Environment
-----------
HESTIA_API_URL        base URL of the HESTIA API
HESTIA_API_KEY        bearer token
VM_COMMS_POLL_INTERVAL  seconds between claim polls (default 5; polling mode only)
IN_MNT / OUT          host data dirs (bind-mounted into sam2 as /data/in,/out)
INDEX_SUFFIX          indexed-dir suffix (default _indexed)
PIPELINE_SCRIPT       path to run_pipeline.sh (full reconstruction)
COMPOSE_DIR           dir holding docker-compose.yml (default: PIPELINE_SCRIPT's dir)
SAM2_SERVICE          compose service name of the GPU worker (default 'sam2')

Optional Kafka mode
-------------------
KAFKA_BROKER                bootstrap.servers (e.g. kafka:29092). When set,
                            ``run_kafka_consumer`` runs instead of polling.
KAFKA_TOPIC_JOB_CREATED     topic (default ``nefele_job_created`` — HESTIA's name)
KAFKA_TOPIC_JOB_UPDATED     topic (default ``nefele_job_modified`` — HESTIA's name)
KAFKA_GROUP_ID              consumer group (default ``sam-worker``) — the
                            group **is** the claim mechanism.
Requires ``confluent_kafka`` installed on the host; if missing, the poller
logs a warning and falls back to polling.

NOTE: the HESTIA /vm-comms endpoints do not exist yet — this is written
against the agreed contract. Only the HTTP details here change once the
backend is built.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s worker_poller: %(message)s"
)
log = logging.getLogger(__name__)

# Load .env from the repo root (two levels up from this file: SAM2/app/ → SAM2/ → repo/).
# Variables already in the environment take precedence — explicit exports win over .env.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.is_file():
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            if _k and _k not in os.environ:   # don't overwrite explicit exports
                os.environ[_k] = _v.strip()
    log.info("loaded .env from %s", _ENV_FILE)

API_BASE = os.environ.get("HESTIA_API_URL", "https://api.textailes.athenarc.gr").rstrip("/")
API_KEY = os.environ.get("HESTIA_API_KEY", "")
VM_COMMS_EP = f"{API_BASE}/nefele"
ROBOT_IMAGES_EP = f"{API_BASE}/robot-images"
RECONSTRUCTIONS_EP = f"{API_BASE}/reconstructions"

POLL_INTERVAL = float(os.environ.get("VM_COMMS_POLL_INTERVAL", "5"))
IN_MNT = Path(os.environ.get("IN_MNT", "/data/in"))
OUT = Path(os.environ.get("OUT", "/data/out"))
INDEX_SUFFIX = os.environ.get("INDEX_SUFFIX", "_indexed")

# Validate paths exist (worker_poller runs on host, not in container)
if not IN_MNT.exists():
    log.error("IN_MNT path does not exist: %s (set it in .env)", IN_MNT)
    raise RuntimeError(f"IN_MNT path does not exist: {IN_MNT}. Set IN_MNT in .env to the host input directory.")
if not OUT.exists():
    log.error("OUT path does not exist: %s (set it in .env)", OUT)
    raise RuntimeError(f"OUT path does not exist: {OUT}. Set OUT in .env to the host output directory.")
if not API_KEY:
    log.error("HESTIA_API_KEY is empty (set it in .env)")
    raise RuntimeError("HESTIA_API_KEY is empty. Set it in .env to authenticate with HESTIA.")

REPO_ROOT = Path(__file__).resolve().parent.parent          # .../SAM2
PIPELINE_SCRIPT = Path(os.environ.get("PIPELINE_SCRIPT", REPO_ROOT.parent / "scripts" / "run_pipeline.sh"))

# The poller runs on the SAM VM host and drives the dockerised pipeline.
# COMPOSE_DIR holds docker-compose.yml; SAM2 preview runs inside the `sam2`
# GPU container via `docker compose exec`.
COMPOSE_DIR = Path(os.environ.get("COMPOSE_DIR", PIPELINE_SCRIPT.parent))
SAM2_SERVICE = os.environ.get("SAM2_SERVICE", "sam2")
# Mount points inside the sam2 container (see samplify_sugar/docker-compose.yml):
# the host IN_MNT/OUT are bind-mounted there as these paths.
CONTAINER_IN = "/data/in"
CONTAINER_OUT = "/data/out"

# Host-side directories where the reconstruction pipeline writes its output
# meshes.  These are *not* the same as OUT (which is the SAM2 I/O directory).
#   PGSR:     <repo>/PGSR/outputs/<dataset>/mesh/*.obj
#   Fast-PGSR:<repo>/FASTPGSR/outputs/<dataset>/mesh/*.obj
#   SuGaR:    <repo>/SUGAR/SuGaR/outputs/<group>/<dataset>/refined_mesh/data/*.obj
_REPO = REPO_ROOT.parent                 # SAMplify_SuGaR root
PGSR_RESULTS_ROOT  = Path(os.environ.get("PGSR_RESULTS_ROOT",  _REPO / "PGSR" / "outputs"))
FASTPGSR_RESULTS_ROOT = Path(os.environ.get("FASTPGSR_RESULTS_ROOT", _REPO / "FASTPGSR" / "outputs"))
SUGAR_RESULTS_ROOT = Path(os.environ.get("SUGAR_RESULTS_ROOT", _REPO / "SUGAR" / "SuGaR" / "outputs"))

# Demo scan_ids: skip real SAM2/COLMAP/reconstruction for these and return
# canned previews + an instant "done" instead. Scoped tightly by exact
# scan_id match so this never fires for a real user's dataset. A completed
# reconstruction for the scan_id must already exist in HESTIA — nefele_ui's
# /results page fetches it independently via scan_id, so the demo job never
# needs to touch the reconstruction record itself.
DEMO_SCAN_IDS = {
    s.strip() for s in os.environ.get("DEMO_SCAN_IDS", "u2a6f4bf6_dress_demo").split(",")
    if s.strip()
}
DEMO_FIXTURES_DIR = Path(os.environ.get("DEMO_FIXTURES_DIR", str(REPO_ROOT / "app" / "demo_fixtures")))

# --- mesh patching helpers (mirror of services/results.py) -------------------
# These ensure that when we rename SuGaR's long filenames to {dataset}.obj/mtl/png
# the internal cross-references (mtllib, map_Kd) are updated to match, so the
# downloaded trio loads correctly in Blender / MeshLab without manual editing.
_MTLLIB_RE = re.compile(rb"^(mtllib\s+)(\S+)", re.MULTILINE)
_MAP_RE    = re.compile(rb"^(\s*map_\w+(?:\s+-\w+\s+\S+)*\s+)(\S+)", re.MULTILINE)

def _patch_obj(data: bytes, dataset: str) -> bytes:
    """Rewrite every `mtllib <x>.mtl` line to `mtllib <dataset>.mtl`."""
    target = f"{dataset}.mtl".encode()
    return _MTLLIB_RE.sub(lambda m: m.group(1) + target, data)

def _patch_mtl(data: bytes, dataset: str) -> bytes:
    """Rewrite every `map_*` texture reference to `<dataset>.<ext>`."""
    stem = dataset.encode()
    def _repl(m: "re.Match[bytes]") -> bytes:
        suffix = Path(m.group(2).decode("latin-1")).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg"}:
            return m.group(1) + stem + suffix.encode("ascii")
        return m.group(0)
    return _MAP_RE.sub(_repl, data)

# vm_comms status constants — must match the contract / vm_comms.py.
S_POINTS_SUBMITTED = "points_submitted"
S_PREVIEWING = "previewing"
S_PREVIEW_READY = "preview_ready"
S_RUNNING = "running"
S_DONE = "done"
S_ERROR = "error"
S_CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised when the UI cancels the current job (status=cancelled) so the
    job lifecycle can unwind cleanly without being reported as an error."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


# --- vm_comms calls --------------------------------------------------------
def claim_job() -> Optional[dict]:
    """Step 5: atomically claim a points_submitted job. None if the queue is empty."""
    r = requests.post(
        f"{VM_COMMS_EP}/claim",
        headers=_headers(),
        params={"status": S_POINTS_SUBMITTED},
        timeout=20,
    )
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def get_job(job_id: str) -> dict:
    r = requests.get(f"{VM_COMMS_EP}/{job_id}", headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def post_preview(job_id: str, preview_files: List[Path]) -> None:
    """Step 6: upload preview images; the server sets status=preview_ready.

    Files go under the multipart field ``file`` — the same convention HESTIA's
    robot-images / reconstructions resources use for binary uploads.
    """
    files = [("file", (p.name, open(p, "rb"), "image/png")) for p in preview_files]
    try:
        r = requests.post(
            f"{VM_COMMS_EP}/{job_id}/preview", headers=_headers(), files=files, timeout=120
        )
        r.raise_for_status()
    finally:
        for _, (_, fh, _) in files:
            fh.close()


def post_status(job_id: str, *, stage: str, stage_index: int, message: str,
                status: str, error: Optional[str] = None) -> None:
    """Step 10: report pipeline progress via PATCH (HESTIA mutates rows with PATCH)."""
    body = {
        "stage": stage,
        "stage_index": stage_index,
        "message": message,
        "status": status,
        "error": error,
    }
    try:
        r = requests.patch(
            f"{VM_COMMS_EP}/{job_id}", headers=_headers(), json=body, timeout=20
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("status post failed (job %s): %s", job_id, e)


# --- step 3: fetch the scan's images --------------------------------------
def download_scan(scan_id: str, dest_dir: Path) -> int:
    """Download every robot-image for a scan into dest_dir. Returns count."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    page, total = 1, 0
    while True:
        r = requests.get(
            ROBOT_IMAGES_EP, headers=_headers(),
            params={"scan_id": scan_id, "page": page, "per_page": 100}, timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for img in batch:
            save_path = dest_dir / img["filename"]
            if save_path.exists():
                total += 1
                continue
            ir = requests.get(img["public_url"], headers=_headers(), stream=True, timeout=60)
            ir.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in ir.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
            total += 1
        page += 1
    return total


# --- preview rendering (video_predict.py, inside the sam2 container) -------
def render_preview(job: dict, input_dir: Path, indexed_dir: Path) -> List[Path]:
    """Write prompts.json, then run video_predict.py --preview INSIDE the sam2
    GPU container via ``docker compose exec`` — the poller host has no CUDA.

    ``input_dir``/``indexed_dir`` are host paths; the worker writes/reads them
    there, but the command runs with the container's mount points (the host
    IN_MNT/OUT appear as /data/in, /data/out inside ``sam2``).
    """
    dataset = job["dataset_name"]
    prompts_json = indexed_dir / "prompts.json"
    preview_dir = indexed_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    for f in preview_dir.glob("*"):
        if f.is_file():
            f.unlink()
    prompts_json.write_text(json.dumps(job["points_json"]), encoding="utf-8")

    # Container-side paths for the exec'd command.
    c_indexed = f"{CONTAINER_OUT}/{dataset}{INDEX_SUFFIX}"
    c_input = f"{CONTAINER_IN}/{dataset}"
    inner = (
        f"PROMPTS_JSON={c_indexed}/prompts.json AUTO_INDEX=1 "
        f"INPUT={c_input} OUT={CONTAINER_OUT} QUIET=0 "
        f"python3 /workspace/app/video_predict.py --preview "
        f"--preview-num-frames 6 --preview-out {c_indexed}/preview"
    )
    cmd = ["docker", "compose", "exec", "-T", SAM2_SERVICE, "bash", "-lc", inner]
    p = subprocess.run(cmd, cwd=str(COMPOSE_DIR),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"preview failed:\n{p.stdout[-1000:]}")

    previews: List[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        previews.extend(sorted(preview_dir.rglob(ext)))
    return previews


def demo_preview_files(scan_id: str) -> List[Path]:
    """Canned stand-in for render_preview() on a DEMO_SCAN_IDS scan: a fixed
    set of pre-fetched preview images checked into DEMO_FIXTURES_DIR/<scan_id>/,
    uploaded via the same post_preview() call a real preview uses."""
    d = DEMO_FIXTURES_DIR / scan_id
    files: List[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        files.extend(sorted(d.glob(ext)))
    if not files:
        raise RuntimeError(f"no canned preview fixtures for demo scan_id={scan_id!r} in {d}")
    return files


# --- full reconstruction pipeline -----------------------------------------
def force_kill_pipeline(proc: subprocess.Popen) -> None:
    """Tear down a running pipeline subprocess + its docker children.

    ``proc`` is the bash that exec'd ``run_pipeline.sh``. Three layers must die:

    1. The whole host process *group* (run_pipeline.sh, the nested
       run_*_pipeline_with_sam.sh, docker CLI clients, tee, …). Killing just
       ``proc`` orphans the children, which is why cancel used to appear to
       work while training kept running. Requires ``start_new_session=True``
       on the Popen so the group is ours to kill.
    2. One-off ``docker compose run`` containers. ``docker compose stop <svc>``
       does NOT stop those — they aren't the service container — so we find
       them via their compose labels and ``docker kill`` them. The ancestor
       filter also catches the plain ``docker run pgsr:local`` fallback.
    3. The training process exec'd inside the long-lived sam2 service
       (``compose exec`` children survive their client). pkill inside the
       container, never kill the container itself.

    All operations are best-effort — we suppress every error so a partial
    teardown still leaves the worker free to claim the next job.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except Exception as e:
        log.warning("killpg(pipeline) failed: %s — falling back to terminate", e)
        try:
            proc.kill()
        except Exception:
            pass

    # Kill compose-run one-off containers for the pipeline services, plus any
    # container started from the pgsr:local fallback image.
    filters = [f"label=com.docker.compose.service={s}"
               for s in ("pgsr", "sugar", "colmap")]
    filters.append("ancestor=pgsr:local")
    for flt in filters:
        try:
            ids = subprocess.run(
                ["docker", "ps", "-q", "--filter", flt],
                cwd=str(COMPOSE_DIR), timeout=15, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            ).stdout.split()
            if ids:
                subprocess.run(
                    ["docker", "kill", *ids],
                    cwd=str(COMPOSE_DIR), timeout=30, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    # SAM2 runs inside the long-lived service container via `compose exec` —
    # kill the training script inside it, not the container.
    try:
        subprocess.run(
            ["docker", "compose", "exec", "-T", SAM2_SERVICE,
             "bash", "-c", "pkill -f video_predict.py"],
            cwd=str(COMPOSE_DIR), timeout=15, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def run_pipeline(job: dict, dataset: str, indexed_dir: Path) -> None:
    """Run run_pipeline.sh and stream its status file to vm_comms (step 10).

    run_pipeline.sh writes ``__pipeline_status.json`` into the indexed dir at
    every stage; we tail it and forward each update to the job row. We also
    poll the HESTIA job row on the same tick so a ``cancelled`` status raised
    by the UI tears the pipeline down within one POLL_INTERVAL.
    """
    job_id = job["job_id"]
    status_file = indexed_dir / "__pipeline_status.json"
    status_file.unlink(missing_ok=True)

    # run_pipeline.sh expects the dataset positionally + prompts.json on disk
    # (render_preview wrote it). It no longer touches any ui container.
    # start_new_session puts the script in its own process group so
    # force_kill_pipeline can killpg() the whole tree (nested bash + docker
    # CLI clients), not just the top-level bash.
    proc = subprocess.Popen(
        ["bash", str(PIPELINE_SCRIPT), dataset],
        env={**os.environ, "DATASET_NAME": dataset},
        cwd=str(PIPELINE_SCRIPT.parent),
        start_new_session=True,
    )
    while proc.poll() is None:
        time.sleep(POLL_INTERVAL)
        # Cancel check first — if the UI cancelled, kill the pipeline and bail.
        try:
            current = get_job(job_id)
        except requests.RequestException:
            current = None
        if current and current.get("status") == S_CANCELLED:
            log.info("job %s cancelled by UI — killing pipeline", job_id)
            force_kill_pipeline(proc)
            raise JobCancelled(job_id)
        if status_file.is_file():
            try:
                s = json.loads(status_file.read_text(encoding="utf-8"))
                post_status(
                    job_id,
                    stage=str(s.get("stages", ["?"])[max(s.get("current", 0), 0)]),
                    stage_index=int(s.get("current", -1)),
                    message=str(s.get("message", "")),
                    status=S_RUNNING,
                )
            except (json.JSONDecodeError, OSError, IndexError):
                pass
    if proc.returncode != 0:
        raise RuntimeError(f"pipeline exited {proc.returncode}")


def upload_reconstruction(job: dict, dataset: str) -> None:
    """Step 12: POST the finished OBJ/MTL/PNG to HESTIA /reconstructions.

    HESTIA's ReconstructionResource reads ``request.files.getlist('file')`` and
    classifies each file by extension, so every file is sent under the single
    multipart field name ``file``.

    Output directories (not the SAM2 I/O dir):
      PGSR:  PGSR_RESULTS_ROOT/<dataset>/mesh/*.obj
      SuGaR: SUGAR_RESULTS_ROOT/**/<dataset>/**/refined_mesh/data/*.obj
             (the raw variant — _postprocessed files are skipped)

    SuGaR files are renamed to {dataset}.obj/mtl/png and their internal
    cross-references (mtllib / map_Kd) are patched to match, so the downloaded
    trio loads correctly in Blender without manual editing.

    PGSR files keep their original names but are prefixed with {dataset}_.
    """
    # Determine which model produced the output.
    model_file = IN_MNT / dataset / ".model"
    model = "sugar"
    if model_file.is_file():
        m = model_file.read_text(encoding="utf-8").strip()
        if m in ("sugar", "pgsr", "fastpgsr"):
            model = m
    log.info("upload_reconstruction: dataset=%s model=%s", dataset, model)

    files: list = []

    if model in ("pgsr", "fastpgsr"):
        # PGSR / Fast-PGSR write to <RESULTS_ROOT>/<dataset>/mesh/ with the
        # same layout, so the harvesting below is shared — only the results
        # root differs by model.
        results_root = FASTPGSR_RESULTS_ROOT if model == "fastpgsr" else PGSR_RESULTS_ROOT
        search_root = results_root / dataset
        if not search_root.exists():
            raise RuntimeError(
                f"{model} results dir not found: {search_root}"
            )
        all_obj = sorted(search_root.rglob("*.obj"), key=lambda p: p.stat().st_mtime)
        if not all_obj:
            raise RuntimeError(f"no .obj produced by {model} pipeline for {dataset}")
        # Prefer the textured OBJ (has MTL+PNG) over the plain one.
        textured = [p for p in all_obj if "textured" in p.stem]
        obj = textured[-1] if textured else all_obj[-1]
        mtl = obj.with_suffix(".mtl")
        tex = next(
            (obj.parent / f"{obj.stem}{e}" for e in (".png", ".jpg")
             if (obj.parent / f"{obj.stem}{e}").exists()), None
        )
        # Prefix names with dataset so downloads are identifiable.
        def _pref(name: str) -> str:
            return name if name.startswith(f"{dataset}_") else f"{dataset}_{name}"
        files = [("file", (_pref(obj.name), open(obj, "rb"), "application/octet-stream"))]
        if mtl.exists():
            files.append(("file", (_pref(mtl.name), open(mtl, "rb"), "application/octet-stream")))
        if tex:
            mime = "image/jpeg" if tex.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            files.append(("file", (_pref(tex.name), open(tex, "rb"), mime)))

    else:  # sugar
        # SuGaR writes deep under SUGAR_RESULTS_ROOT — search for dataset dir.
        # Skip _postprocessed variants (intermediate cleaned-up mesh).
        candidates = [
            p for p in SUGAR_RESULTS_ROOT.rglob("*.obj")
            if dataset in p.parts and "_postprocessed" not in p.stem
        ]
        if not candidates:
            raise RuntimeError(f"no .obj produced by SuGaR pipeline for {dataset}")
        obj = max(candidates, key=lambda p: p.stat().st_mtime)
        mtl = obj.with_suffix(".mtl")
        tex = next(
            (obj.parent / f"{obj.stem}{e}" for e in (".png", ".jpg")
             if (obj.parent / f"{obj.stem}{e}").exists()), None
        )
        # Rename to {dataset}.obj/mtl/png and patch internal cross-references
        # so the trio is self-consistent (mtllib / map_Kd lines updated).
        obj_data = _patch_obj(obj.read_bytes(), dataset)
        files = [("file", (f"{dataset}.obj", io.BytesIO(obj_data), "application/octet-stream"))]
        if mtl.exists():
            mtl_data = _patch_mtl(mtl.read_bytes(), dataset)
            files.append(("file", (f"{dataset}.mtl", io.BytesIO(mtl_data), "application/octet-stream")))
        if tex:
            tex_ext = tex.suffix.lower()
            mime = "image/jpeg" if tex_ext in (".jpg", ".jpeg") else "image/png"
            files.append(("file", (f"{dataset}{tex_ext}", io.BytesIO(tex.read_bytes()), mime)))

    log.info("uploading %d file(s) to HESTIA: %s",
             len(files), [f[1][0] for f in files])
    try:
        r = requests.post(
            RECONSTRUCTIONS_EP, headers=_headers(),
            files=files, data={"scan_id": job["scan_id"]}, timeout=300,
        )
        r.raise_for_status()
        log.info("reconstruction uploaded for scan %s", job.get("scan_id"))
    finally:
        for _, (_, fh, _) in files:
            if hasattr(fh, "close"):
                fh.close()


# --- main job lifecycle ----------------------------------------------------
def handle_job(job: dict) -> None:
    job_id = job["job_id"]
    dataset = job["dataset_name"]

    # Claim-time guard: skip jobs that already reached a terminal state while
    # queued. In Kafka mode a job can be cancelled (or finished/errored) while
    # this worker is busy on a previous job; its notification is still sitting
    # in the topic. The consumer re-reads current state via get_job() right
    # before this call, so job["status"] here is authoritative. Without this
    # check we would run download + preview before the mid-pipeline /
    # confirm-wait cancel-checks ever fire — i.e. process a cancelled job.
    status = (job.get("status") or "").strip()
    if status in (S_CANCELLED, S_DONE, S_ERROR):
        log.info("skipping job %s (dataset=%s) — already %s", job_id, dataset, status)
        return

    input_dir = IN_MNT / dataset
    indexed_dir = OUT / f"{dataset}{INDEX_SUFFIX}"
    indexed_dir.mkdir(parents=True, exist_ok=True)
    scan_id = (job.get("scan_id") or "").strip()
    is_demo = scan_id in DEMO_SCAN_IDS
    log.info("claimed job %s (dataset=%s scan=%s)%s", job_id, dataset, job.get("scan_id"),
              " [DEMO]" if is_demo else "")

    try:
        if is_demo:
            # Demo scan_id: skip real SAM2 inference, upload a fixed set of
            # already-rendered preview images instead. Same post_preview()
            # call a real preview uses, so the contract (multipart upload,
            # server sets status=preview_ready) is unchanged.
            post_status(job_id, stage="preview", stage_index=0,
                        message="Generating previews", status=S_PREVIEWING)
            previews = demo_preview_files(scan_id)
            post_preview(job_id, previews)
            log.info("job %s: [DEMO] uploaded %d canned preview images", job_id, len(previews))
        else:
            # Step 3: make sure the scan's images are on local disk.
            if job.get("scan_id") and not any(input_dir.glob("*")):
                n = download_scan(job["scan_id"], input_dir)
                log.info("downloaded %d images for scan %s", n, job["scan_id"])

            # Step 6: render and upload previews.
            previews = render_preview(job, input_dir, indexed_dir)
            post_preview(job_id, previews)
            log.info("uploaded %d preview images for job %s", len(previews), job_id)

        # Step 9: wait for the user's decision.
        while True:
            time.sleep(POLL_INTERVAL)
            current = get_job(job_id)
            if current.get("status") == S_CANCELLED:
                raise JobCancelled(job_id)
            instr = current.get("instructions") or {}
            decision = instr.get("decision")
            if decision == "redo":
                job["points_json"] = instr.get("points_json", job["points_json"])
                if is_demo:
                    post_preview(job_id, demo_preview_files(scan_id))
                    log.info("job %s: [DEMO] redo — re-uploaded canned preview", job_id)
                else:
                    log.info("job %s: redo — re-rendering preview", job_id)
                    previews = render_preview(job, input_dir, indexed_dir)
                    post_preview(job_id, previews)
                continue
            if decision in ("confirm", "use_existing"):
                break

        if is_demo:
            # Skip real COLMAP/SuGaR/PGSR entirely. A completed reconstruction
            # for this scan_id already exists in HESTIA; nefele_ui's /results
            # page fetches it independently by scan_id, so the demo job only
            # needs to reach status=done without erroring — it doesn't need
            # to touch the reconstruction record itself.
            post_status(job_id, stage="running", stage_index=0,
                        message="Training reconstruction", status=S_RUNNING)
            time.sleep(2)
            post_status(job_id, stage="done", stage_index=99,
                        message="reconstruction complete", status=S_DONE)
            log.info("job %s done [DEMO]", job_id)
            return

        # Resolve the reconstruction model and (re)write the .model file that
        # run_pipeline.sh reads. Priority:
        #   1. FORCE_MODEL env — operator override, fixed model for every job.
        #   2. the job's model field from HESTIA — authoritative for vm_comms.
        #   3. an existing .model file — fallback only for local runs where the
        #      job carries no model.
        # The .model file is refreshed every run, so a stale value can never
        # override HESTIA's choice (previously the file won, which silently
        # pinned datasets to whatever model their first job used).
        _VALID = ("sugar", "pgsr", "fastpgsr")
        model_file = input_dir / ".model"
        force_model = (os.environ.get("FORCE_MODEL") or "").strip()
        if force_model in _VALID:
            model, source = force_model, "FORCE_MODEL override"
        else:
            model = (job.get("model") or "").strip()
            source = "from HESTIA job"
            if model not in _VALID and model_file.is_file():
                model = model_file.read_text(encoding="utf-8").strip()
                source = "from .model file (job had none)"
            if model not in _VALID:
                model, source = "sugar", "default"
        model_file.write_text(model, encoding="utf-8")
        log.info("job %s: model=%s (%s)", job_id, model, source)

        # Steps 10 + 12: run the pipeline and publish the result.
        run_pipeline(job, dataset, indexed_dir)
        upload_reconstruction(job, dataset)
        post_status(job_id, stage="done", stage_index=99,
                    message="reconstruction complete", status=S_DONE)
        log.info("job %s done", job_id)

    except JobCancelled:
        # Status is already 'cancelled' on the server (set by the UI's POST
        # /cancel) — don't overwrite it with 'error'. Just unwind cleanly so
        # the worker frees up for the next job.
        log.info("job %s cancelled — worker unwound cleanly", job_id)
    except Exception as e:
        log.exception("job %s failed", job_id)
        post_status(job_id, stage="", stage_index=-1,
                    message="worker error", status=S_ERROR, error=str(e))


def run_polling_loop() -> None:
    """HTTP-polling job dispatch. Hits POST /vm-comms/claim until a job is
    returned (atomic, server-side `FOR UPDATE SKIP LOCKED`)."""
    log.info("worker_poller (polling mode) — POST %s/claim every %ss",
             VM_COMMS_EP, POLL_INTERVAL)
    while True:
        try:
            job = claim_job()
        except requests.RequestException as e:
            log.warning("claim poll failed: %s", e)
            job = None
        if job is None:
            time.sleep(POLL_INTERVAL)
            continue
        handle_job(job)


def run_kafka_consumer() -> None:
    """Event-driven dispatch. Subscribes to the ``nefele_job_created`` topic; the
    Kafka consumer-group is the claim mechanism (each message to one consumer).

    The message body is just a notification — read full job state from HESTIA
    via GET /vm-comms/<job_id>.
    """
    try:
        from confluent_kafka import Consumer
    except ImportError:
        log.error("KAFKA_BROKER is set but confluent_kafka is not installed "
                  "(pip install confluent_kafka). Falling back to polling.")
        return run_polling_loop()

    broker = os.environ["KAFKA_BROKER"]
    # Matches TOPIC_NEFELE_JOB_CREATED in HESTIA's services/messaging.py.
    # Override with KAFKA_TOPIC_JOB_CREATED if the topic name ever changes.
    topic = os.environ.get("KAFKA_TOPIC_JOB_CREATED", "nefele_job_created")
    group = os.environ.get("KAFKA_GROUP_ID", "sam-worker")

    consumer = Consumer({
        "bootstrap.servers": broker,
        "group.id": group,
        # "latest" → on a fresh start (no committed offset) only consume NEW
        # messages, never replay history.  "earliest" caused old jobs to be
        # re-processed whenever the consumer group lost its offset (e.g. after
        # a MAX_POLL_EXCEEDED rebalance killed the previous process mid-run).
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        # Allow up to 2 hours between poll() calls so a long-running pipeline
        # (SAM2 + COLMAP + SuGaR/PGSR can take 60-90 min) doesn't trigger a
        # MAX_POLL_EXCEEDED rebalance and lose the committed offset.
        "max.poll.interval.ms": 7200000,
        # Keep the broker heartbeat alive even while handle_job() blocks the
        # poll loop (heartbeats run on a background thread).
        "session.timeout.ms": 60000,
        "heartbeat.interval.ms": 20000,
    })
    consumer.subscribe([topic])
    log.info("worker_poller (Kafka mode) — broker=%s topic=%s group=%s",
             broker, topic, group)

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                log.error("Kafka error: %s", msg.error())
                continue
            try:
                event = json.loads(msg.value().decode("utf-8"))
            except Exception as e:
                log.error("ignoring malformed Kafka message: %s", e)
                continue
            job_id = event.get("job_id") or event.get("id")
            if not job_id:
                log.warning("nefele_job_created event lacks job_id: %s", event)
                continue
            try:
                job = get_job(job_id)
            except requests.RequestException as e:
                log.error("GET job %s failed: %s — skipping", job_id, e)
                continue
            handle_job(job)
    finally:
        consumer.close()


def ensure_sam2_running() -> None:
    """Bring the sam2 GPU container up if it is not already running.

    The worker uses ``docker compose exec sam2`` for preview rendering, which
    requires the container to be running *before* the exec call.  Starting it
    here at worker boot means the user never has to remember to do it manually.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            cwd=str(COMPOSE_DIR), capture_output=True, text=True, timeout=15,
        )
        running = result.stdout.split()
        if SAM2_SERVICE not in running:
            log.info("sam2 container not running — starting it now (docker compose up -d %s)", SAM2_SERVICE)
            subprocess.run(
                ["docker", "compose", "up", "-d", SAM2_SERVICE],
                cwd=str(COMPOSE_DIR), timeout=120, check=True,
            )
            log.info("sam2 container started.")
        else:
            log.info("sam2 container already running.")
    except Exception as e:
        log.warning("could not ensure sam2 is running: %s — continuing anyway", e)


def main() -> None:
    """Pick the dispatch mode from env: ``KAFKA_BROKER`` → consumer, otherwise polling."""
    ensure_sam2_running()
    if os.environ.get("KAFKA_BROKER"):
        run_kafka_consumer()
    else:
        run_polling_loop()


if __name__ == "__main__":
    main()
