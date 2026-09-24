"""Landing page shown as the app's entry point.

Renders context-aware action buttons:
  - dataset configured + prompts.json exists → Continue / Results / New dataset
  - dataset configured + no prompts yet      → Continue (to picker) / New dataset
  - nothing configured                       → Get started

Also owns the dataset-level reconstruction-model toggle (SuGaR / PGSR), which
was hoisted out of the Setup page so it can be changed after the data is
already on disk — the same frames work for both methods.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..services.dataset_meta import write_model
from ..services.results import read_model
from ._helpers import cfg, frames, json_err, json_ok
from flask import render_template

bp = Blueprint("welcome", __name__)


@bp.get("/welcome")
def welcome():
    c = cfg()
    model = read_model(c.in_mnt, c.dataset_name) if c.is_configured else "sugar"
    return render_template(
        "welcome.html",
        is_configured=c.is_configured,
        dataset_name=c.display_name,
        has_prompts=c.prompts_json.is_file() if c.is_configured else False,
        has_frames=bool(frames()),
        model=model,
    )


@bp.post("/welcome/model")
def set_model():
    """Update the active dataset's .model file (sugar | pgsr | fastpgsr)."""
    c = cfg()
    if not c.is_configured:
        return json_err("No active dataset.", http=400)

    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if model not in ("sugar", "pgsr", "fastpgsr"):
        return json_err("model must be 'sugar', 'pgsr', or 'fastpgsr'.")

    write_model(c.in_mnt / c.dataset_name, model)
    return json_ok(model=model)
