"""Runtime configuration for the Point Picker UI.

All settings are read from environment variables so the same image can run in
different deployments (different datasets, different worker URLs, with or
without Directus auth) without code changes.

Per-user active-dataset state lives in a coordination file namespaced by the
visitor's identity (see ``read_user_active_dataset`` / ``write_user_active_dataset``
in ``services.uploads``), so it survives container restarts and browser
changes for that one user without ever being visible to anyone else.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The one pre-built dataset the demo accounts (auth.DEMO_USER_EMAILS) default
# to instead of starting in setup mode. This is only ever used as a
# *default* when that visitor has no active-dataset choice of their own yet
# (see routes._helpers.cfg) — they can still start a fresh dataset normally.
DEMO_DATASET_NAME = "u2a6f4bf6_dress_demo"

_NAMESPACED_NAME_RE = re.compile(r"^u[0-9a-f]{8}_(.+)$")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _user_active_dataset_path(in_mnt: Path, user_id: str) -> Path:
    return in_mnt / f".active_dataset.{user_id}"


def read_user_active_dataset(in_mnt: Path, user_id: str) -> Optional[str]:
    f = _user_active_dataset_path(in_mnt, user_id)
    if not f.is_file():
        return None
    name = f.read_text(encoding="utf-8").strip()
    return name or None


@dataclass(frozen=True)
class Config:
    dataset_name: str          # empty string means "setup mode"
    user_id: str               # current visitor's identity; namespaces state so
                                # different users never see/clobber each other's data
    in_mnt: Path               # /data/in
    out_root: Path             # /data/out
    index_suffix: str
    worker_url: str
    worker_timeout: float
    auth_enabled: bool
    debug: bool
    sugar_results_root: Path   # mount of the SuGaR obj_outputs/ directory
    pgsr_results_root: Path    # mount of the PGSR outputs/ directory
    fastpgsr_results_root: Path # mount of the Fast-PGSR outputs/ directory
    comms_backend: str         # "shared_fs" (legacy) | "vm_comms" (HESTIA API)
    poll_interval: float       # seconds between vm_comms job polls

    @property
    def is_configured(self) -> bool:
        return bool(self.dataset_name)

    @property
    def uses_vm_comms(self) -> bool:
        """True when the UI talks to the worker through the HESTIA vm_comms API
        instead of the legacy shared-filesystem handshake."""
        return self.comms_backend == "vm_comms"

    @property
    def ds_name(self) -> str:
        return self.dataset_name

    @property
    def display_name(self) -> str:
        """``dataset_name`` with its ``u<hash>_`` namespace prefix stripped,
        for showing to whoever is viewing it. Matches the prefix shape
        regardless of *whose* hash it is — a viewer can legitimately see a
        dataset namespaced under someone else's id (e.g. the demo account
        viewing DEMO_DATASET_NAME) and this is purely cosmetic, not an
        ownership check, so there's no reason to restrict it to "my own"
        prefix. Everything that touches disk, HESTIA, or vm_comms must keep
        using ``dataset_name`` itself."""
        m = _NAMESPACED_NAME_RE.match(self.dataset_name)
        return m.group(1) if m else self.dataset_name

    @property
    def input_dir(self) -> Path:
        return self.in_mnt / self.dataset_name

    @property
    def indexed_dir(self) -> Path:
        return self.out_root / f"{self.dataset_name}{self.index_suffix}"

    @property
    def prompts_json(self) -> Path:
        return self.indexed_dir / "prompts.json"

    @property
    def done_flag(self) -> Path:
        return self.indexed_dir / "__picker_done.flag"

    @property
    def use_existing_flag(self) -> Path:
        return self.indexed_dir / "__use_existing.flag"

    @property
    def preview_dir(self) -> Path:
        return self.indexed_dir / "preview"

    def ensure_dirs(self) -> None:
        if self.is_configured:
            self.indexed_dir.mkdir(parents=True, exist_ok=True)
            self.preview_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    """Build the process-wide base Config from the environment.

    This is deliberately the *only* place ``DATASET_NAME`` is read — it pins
    an entire deployment to one dataset for single-tenant setups (e.g. a
    throwaway dev container). It is NOT how per-user active-dataset
    selection works on the shared multi-user deployment: that is resolved
    per-request in ``routes._helpers.cfg()`` from the visitor's own session
    and per-user coordination file, layered on top of this base config, so
    one visitor's choice can never leak into another's. See
    ``read_user_active_dataset`` / ``app.activate_dataset``.
    """
    in_mnt = Path(os.environ.get("IN_MNT", "/data/in"))
    in_mnt.mkdir(parents=True, exist_ok=True)
    out_root = Path(os.environ.get("OUT", "/data/out"))
    out_root.mkdir(parents=True, exist_ok=True)

    dataset_name = os.environ.get("DATASET_NAME", "").strip()

    cfg = Config(
        dataset_name=dataset_name,
        user_id="",
        in_mnt=in_mnt,
        out_root=out_root,
        index_suffix=os.environ.get("INDEX_SUFFIX", "_indexed"),
        worker_url=os.environ.get("WORKER_URL", "http://sam2:5001").rstrip("/"),
        worker_timeout=float(os.environ.get("WORKER_TIMEOUT", "600")),
        auth_enabled=_env_bool("AUTH_ENABLED", default=False),
        debug=_env_bool("FLASK_DEBUG", default=False),
        sugar_results_root=Path(os.environ.get("SUGAR_RESULTS_ROOT", "/data/results/sugar")),
        pgsr_results_root=Path(os.environ.get("PGSR_RESULTS_ROOT", "/data/results/pgsr")),
        fastpgsr_results_root=Path(os.environ.get("FASTPGSR_RESULTS_ROOT", "/data/results/fastpgsr")),
        comms_backend=os.environ.get("COMMS_BACKEND", "shared_fs").strip().lower(),
        poll_interval=float(os.environ.get("VM_COMMS_POLL_INTERVAL", "2")),
    )
    cfg.ensure_dirs()
    return cfg
