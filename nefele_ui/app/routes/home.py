"""Landing page: choose to reuse existing prompts or create new ones.

Redirects to /setup when the app hasn't been bound to a dataset yet or when
the bound dataset's folder has no frames.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

from ._helpers import cfg, frames, json_err

bp = Blueprint("home", __name__)


@bp.get("/")
def root():
    """Root always lands on the branded welcome screen; from there the user
    picks Continue / Results / New dataset based on what's configured."""
    return redirect(url_for("welcome.welcome"))


@bp.get("/home")
def index():
    c = cfg()
    if not c.is_configured:
        return redirect(url_for("welcome.welcome"))
    if not frames():
        return redirect(url_for("setup.setup"))
    return render_template(
        "home.html",
        ds=c.display_name,
        nframes=len(frames()),
        exists=c.prompts_json.is_file(),
        prompts=str(c.prompts_json),
        done=str(c.done_flag),
        use_existing=str(c.use_existing_flag),
    )


@bp.post("/use_existing")
def use_existing():
    """Reuse the saved prompts.json and jump straight to /results.

    Touching the flags triggers pipeline_watcher.sh, so the user lands on
    the results page just as COLMAP / SuGaR are about to start. The stage
    indicator there shows live progress.
    """
    c = cfg()
    c.use_existing_flag.touch()
    c.done_flag.touch()
    return redirect(url_for("results.page"))


@bp.post("/create_new")
def create_new():
    c = cfg()
    try:
        c.prompts_json.unlink(missing_ok=True)
        c.use_existing_flag.unlink(missing_ok=True)
    except OSError as e:
        return json_err(str(e), http=500)
    return redirect(url_for("picker.pick"))
