"""Handle multipart image uploads from the setup screen.

Every accepted file is decoded with Pillow and re-encoded as JPEG. This keeps
the dataset folder uniform (SAM2 requires JPG) and lets users drop in PNG,
WebP, BMP, TIFF, etc. without thinking about format.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Iterable, List, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

log = logging.getLogger(__name__)

JPEG_QUALITY = 95

# Conservative whitelist for dataset names: letters, digits, dash, underscore,
# dot. Anything else is squashed to "_". Prevents path traversal and surprises.
_DATASET_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_dataset_name(raw: str) -> str:
    name = _DATASET_NAME_RE.sub("_", raw.strip())
    return name.lstrip(".")


def _user_tag(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]


def owns_dataset(user_id: str, name: str) -> bool:
    """True if ``name`` was namespaced for ``user_id`` by ``user_scoped_name``.

    Client-supplied dataset names (e.g. from the video-upload confirm/cancel
    steps) must be checked against this before touching files on disk or
    HESTIA/vm_comms state — otherwise one user could guess/reuse another
    user's dataset name and prune or cancel their in-progress upload.
    """
    return name.startswith(f"u{_user_tag(user_id)}_")


def user_scoped_name(user_id: str, raw: str) -> str:
    """Prefix a sanitized dataset name with a short, stable hash of the
    owner's identity.

    Dataset names double as the HESTIA ``scan_id`` and as the key for the
    (dataset-name-keyed) SuGaR/PGSR output mounts, none of which know about
    "users" at all. Namespacing the name itself — rather than the storage
    layout — is what keeps two different people from colliding on disk, in
    HESTIA, or in each other's vm_comms jobs, without touching any of that
    external, dataset-name-keyed machinery.
    """
    name = sanitize_dataset_name(raw)
    return f"u{_user_tag(user_id)}_{name}"


def _to_rgb(img: Image.Image) -> Image.Image:
    """Apply EXIF rotation and flatten alpha onto a white background."""
    img = ImageOps.exif_transpose(img)

    if img.mode == "RGB":
        return img

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    return img.convert("RGB")


def _unique_jpg(target_dir: Path, stem: str) -> Path:
    """Return a free path target_dir/<stem>.jpg, appending _1, _2, ... on collision."""
    dest = target_dir / f"{stem}.jpg"
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = target_dir / f"{stem}_{i}.jpg"
        if not candidate.exists():
            return candidate
        i += 1


def save_uploaded_images(
    target_dir: Path, files: Iterable[FileStorage]
) -> Tuple[List[str], List[dict]]:
    """Decode each upload and save as JPEG. Returns (saved_names, failed)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    failed: List[dict] = []

    for f in files:
        if not f or not f.filename:
            continue
        clean = secure_filename(f.filename) or "image"
        stem = Path(clean).stem or "image"

        try:
            img = Image.open(f.stream)
            img.load()
        except (UnidentifiedImageError, OSError) as e:
            log.warning("rejected upload %r: %s", clean, e)
            failed.append({"name": clean, "error": "not a readable image"})
            continue

        try:
            img = _to_rgb(img)
            dest = _unique_jpg(target_dir, stem)
            img.save(dest, "JPEG", quality=JPEG_QUALITY, optimize=True)
            saved.append(dest.name)
        except Exception as e:
            log.warning("failed to convert %r: %s", clean, e)
            failed.append({"name": clean, "error": "conversion failed"})

    return saved, failed


def write_user_active_dataset(in_mnt: Path, user_id: str, name: str) -> None:
    """Record this user's chosen dataset so it survives restarts and other
    browsers/devices for the same account — without ever being visible to,
    or overwritten by, a different user's choice."""
    (in_mnt / f".active_dataset.{user_id}").write_text(name + "\n", encoding="utf-8")


def assert_within_root(target: Path, root: Path) -> None:
    """Raise ValueError if ``target`` resolves outside ``root`` (catches
    symlink traversal too, unlike a plain string prefix check)."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Path {target!s} escapes its root {root!s}")
