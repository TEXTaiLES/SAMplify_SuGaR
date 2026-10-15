"""HESTIA API client — list robot-image scans and download them locally."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from requests_toolbelt import MultipartEncoder

logger = logging.getLogger(__name__)

_API_BASE = os.environ.get("HESTIA_API_URL", "https://api.textailes.athenarc.gr").rstrip("/")
_API_KEY = os.environ.get("HESTIA_API_KEY", "")
# MinIO is on localhost:9001 from the API server's view.
# Override HESTIA_MINIO_URL if MinIO is exposed on a different address.
_MINIO_URL = os.environ.get("HESTIA_MINIO_URL", "").rstrip("/")

ROBOT_IMAGES_EP = f"{_API_BASE}/robot-images"
RECONSTRUCTIONS_EP = f"{_API_BASE}/reconstructions"


@dataclass
class ScanInfo:
    scan_id: str
    image_count: int
    timestamp: str
    sample_filename: str


def _headers() -> dict:
    return {"Authorization": f"Bearer {_API_KEY}"}


def _download_url(public_url: str) -> str:
    """Rewrite internal localhost:9001 MinIO Console URL to the S3 API on port 9000.

    The HESTIA API stores images in MinIO and exposes their URL as
    http://localhost:9001/... (the Console port). The actual S3-compatible
    download endpoint is on port 9000. Override with HESTIA_MINIO_URL if needed.
    """
    if _MINIO_URL:
        parsed = urlparse(public_url)
        return f"{_MINIO_URL}{parsed.path}"
    api_host = urlparse(_API_BASE).hostname or "api.textailes.athenarc.gr"
    # 9001 = MinIO Console (web UI), 9000 = MinIO S3 API (actual files)
    return public_url.replace("localhost:9001", f"{api_host}:9000")


def scan_exists(scan_id: str) -> bool:
    """Return True if HESTIA already has any robot-image rows under ``scan_id``."""
    r = requests.get(
        ROBOT_IMAGES_EP,
        headers=_headers(),
        params={"scan_id": scan_id, "per_page": 1},
        timeout=10,
    )
    r.raise_for_status()
    return bool(r.json())


def list_scans() -> List[ScanInfo]:
    """Fetch all robot-image records and group them into unique scans."""
    scans: Dict[str, dict] = {}
    page = 1
    while True:
        r = requests.get(
            ROBOT_IMAGES_EP,
            headers=_headers(),
            params={"page": page, "per_page": 100},
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for img in batch:
            sid = img["scan_id"]
            if sid not in scans:
                scans[sid] = {
                    "image_count": 0,
                    "timestamp": img.get("timestamp", ""),
                    "sample_filename": img.get("filename", ""),
                }
            scans[sid]["image_count"] += 1
        page += 1

    return [
        ScanInfo(
            scan_id=sid,
            image_count=info["image_count"],
            timestamp=info["timestamp"],
            sample_filename=info["sample_filename"],
        )
        for sid, info in sorted(
            scans.items(), key=lambda x: x[1]["timestamp"], reverse=True
        )
    ]


def download_scan(scan_id: str, dest_dir: Path, on_progress=None) -> int:
    """Download all images for scan_id into dest_dir. Returns count saved."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    page = 1
    total = 0
    while True:
        r = requests.get(
            ROBOT_IMAGES_EP,
            headers=_headers(),
            params={"scan_id": scan_id, "page": page, "per_page": 100},
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for img in batch:
            filename = img["filename"]
            url = _download_url(img["public_url"])
            save_path = dest_dir / filename
            if save_path.exists():
                total += 1
                if on_progress:
                    on_progress(total)
                continue
            try:
                ir = requests.get(url, headers=_headers(), stream=True, timeout=60)
                ir.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in ir.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                total += 1
                if on_progress:
                    on_progress(total)
                logger.info("downloaded %s", filename)
            except Exception as e:
                logger.warning("failed to download %s: %s", filename, e)
        page += 1
    return total


class _LazyFile:
    """A file-like object that opens lazily and reports its remaining length,
    so MultipartEncoder can stream the upload instead of buffering every
    image's bytes (and holding every file descriptor open) at once — the old
    code opened all N files up front before sending anything."""

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.pos = 0
        self._fh = None

    @property
    def len(self) -> int:
        # MultipartEncoder reads `len` as the bytes still left to send
        return self.size - self.pos

    def read(self, size: int = -1) -> bytes:
        if self._fh is None:
            self._fh = open(self.path, "rb")
        data = self._fh.read(size)
        self.pos += len(data)
        if self.pos >= self.size:
            self.close()
        return data

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def upload_robot_images(image_paths: List[Path], scan_id: str | None = None) -> dict:
    """POST local images to HESTIA /robot-images (diagram step 1b).

    HESTIA's RobotImageResource expects every file under the multipart field
    ``file`` plus a *required* ``metadata_map`` JSON form field (filename →
    metadata). When ``scan_id`` is omitted HESTIA generates one and returns it.

    Returns the parsed body: ``{message, scan_id, uploaded_files}``.

    HESTIA processes each file essentially sequentially (MinIO upload + DB
    insert + two blocking Kafka flushes per image), so a real dataset's
    upload can legitimately take minutes — the long read timeout here is
    deliberate, not a bug. The 30s connect timeout still fails fast if
    HESTIA itself is unreachable.
    """
    paths = [Path(p) for p in image_paths]
    lazy_files = [_LazyFile(p) for p in paths]
    fields: list = [("metadata_map", json.dumps({p.name: {} for p in paths}))]
    if scan_id:
        fields.append(("scan_id", scan_id))
    fields += [("file", (p.name, lf, "image/jpeg")) for p, lf in zip(paths, lazy_files)]

    try:
        encoder = MultipartEncoder(fields=fields)
        r = requests.post(
            ROBOT_IMAGES_EP,
            headers={**_headers(), "Content-Type": encoder.content_type},
            data=encoder,
            timeout=(30, 3600),
        )
        r.raise_for_status()
        return r.json()
    finally:
        for lf in lazy_files:
            lf.close()


def upload_reconstruction(scan_id: str, obj_path: Path, mtl_path: Path | None, texture_path: Path | None) -> dict:
    """Upload a finished OBJ reconstruction to HESTIA /reconstructions.

    HESTIA's ReconstructionResource reads ``request.files.getlist('file')`` and
    classifies each by extension, so every file must be sent under the single
    multipart field name ``file`` (not model_file/material_file/texture_file).
    """
    files: list = []
    open_handles: list = []
    try:
        fh = open(obj_path, "rb")
        open_handles.append(fh)
        files.append(("file", (obj_path.name, fh, "application/octet-stream")))

        if mtl_path and mtl_path.exists():
            fh2 = open(mtl_path, "rb")
            open_handles.append(fh2)
            files.append(("file", (mtl_path.name, fh2, "application/octet-stream")))

        if texture_path and texture_path.exists():
            fh3 = open(texture_path, "rb")
            open_handles.append(fh3)
            files.append(("file", (texture_path.name, fh3, "image/jpeg")))

        r = requests.post(
            RECONSTRUCTIONS_EP,
            headers=_headers(),
            files=files,
            data={"scan_id": scan_id},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()
    finally:
        for fh in open_handles:
            fh.close()


def get_reconstruction(scan_id: str) -> Optional[dict]:
    """GET /reconstructions?scan_id= — the newest reconstruction record for a
    scan, or None if the pipeline hasn't produced one yet.

    HESTIA's ReconstructionResource returns rows ``ORDER BY timestamp DESC``,
    so the first row is the most recent. Each row carries the public URLs:
    ``public_url_model`` (.obj), ``public_url_material`` (.mtl),
    ``public_url_texture`` (.png/.jpg) and ``public_url_glb`` (.glb).
    """
    r = requests.get(
        RECONSTRUCTIONS_EP,
        headers=_headers(),
        params={"scan_id": scan_id, "per_page": 50},
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def get_reconstruction_by_id(scan_id: str, object_id: str) -> Optional[dict]:
    """Like :func:`get_reconstruction`, but returns the specific row matching
    ``object_id`` instead of assuming the newest one is correct.

    Needed when a scan_id is intentionally reused by multiple, independently
    processed local datasets (e.g. a demo dataset reusing a real HESTIA scan
    that an unrelated production dataset also happens to reference) —
    "newest for this scan_id" is then no longer a safe way to identify a
    *specific* dataset's own result, since anyone re-processing the other
    dataset would upload a newer row for the same scan_id.
    """
    r = requests.get(
        RECONSTRUCTIONS_EP,
        headers=_headers(),
        params={"scan_id": scan_id, "per_page": 50},
        timeout=20,
    )
    r.raise_for_status()
    for row in r.json():
        if row.get("object_id") == object_id:
            return row
    return None


def fetch_file(public_url: str) -> bytes:
    """Download a HESTIA/MinIO file (Bearer-authed, streamed) and return its bytes."""
    r = requests.get(_download_url(public_url), headers=_headers(), stream=True, timeout=60)
    r.raise_for_status()
    return r.content
