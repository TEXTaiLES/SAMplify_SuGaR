"""Point Picker UI — Flask application package.

The :func:`create_app` factory builds a Flask app from environment-driven
config and registers the route blueprints.

Dataset state is intentionally NOT cached on the app: it's per-visitor (see
``routes._helpers.cfg``), so two people using the deployment at once each get
their own active dataset. When nothing is configured for a given visitor, the
welcome/home routes send them to ``/setup`` to pick a name and upload images;
:func:`activate_dataset` is what makes a freshly-created dataset "active" for
the visitor who just created it.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import secrets

from flask import Flask, session, url_for

from .auth import current_user_id, init_auth
from .config import Config, load_config
from .services.uploads import write_user_active_dataset


def activate_dataset(name: str) -> Config:
    """Make ``name`` the active dataset for the *current* visitor only.

    Must be called from within a request that has a session (never from a
    background thread — Flask's ``session`` needs an active request context).
    Stamps the choice into this browser's session and persists it per-user on
    disk so it survives across that same account's other browsers/devices.
    Never touches global app state, so it can't affect any other visitor.

    Defined (and imported by ``routes/setup.py`` and ``routes/hestia.py``)
    above the ``from .routes import ...`` below on purpose — those modules
    import this name at load time, so it must already exist on this package
    before ``.routes`` is imported, or that import fails with a circular
    ImportError.
    """
    from flask import current_app

    base: Config = current_app.config["BASE_CONFIG"]
    uid = current_user_id()
    session["dataset_name"] = name
    write_user_active_dataset(base.in_mnt, uid, name)
    c = dataclasses.replace(base, user_id=uid, dataset_name=name)
    c.ensure_dirs()
    return c


from .routes import (  # noqa: E402  (must follow activate_dataset — see above)
    home_bp,
    hestia_bp,
    picker_bp,
    preview_bp,
    results_bp,
    setup_bp,
    welcome_bp,
)


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("FLASK_DEBUG") == "1" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # Disable Flask's default 12h static cache + re-read Jinja templates on
    # every request so CSS/JS/HTML edits land on the next browser refresh
    # instead of leaving users stuck.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["BASE_CONFIG"] = cfg

    secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if cfg.auth_enabled:
        # auth.py caches the Directus access_token in the Flask session so we
        # don't hit /auth/refresh on every poll request — but that needs a
        # signed-cookie secret. Refuse to start without one rather than fall
        # back to Flask's "<no secret>" stub which would crash on the first
        # session.write.
        if not secret:
            raise RuntimeError(
                "AUTH_ENABLED=1 but FLASK_SECRET_KEY is not set — "
                "generate one (e.g. `python -c 'import secrets;print(secrets.token_hex(32))'`) "
                "and add it to .env."
            )
        app.secret_key = secret
        init_auth(app)
    else:
        # No login here, but per-visitor session state (the anonymous id used
        # to namespace datasets, the active-dataset pointer) still needs a
        # signed session cookie. It only has to be stable for this process's
        # lifetime, not survive restarts, so a random key is fine.
        app.secret_key = secret or secrets.token_hex(32)

    app.register_blueprint(home_bp)
    app.register_blueprint(hestia_bp)
    app.register_blueprint(picker_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(results_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(welcome_bp)

    # Cache-buster: append ?v=<mtime> to every static URL so a stale browser
    # tab fetches fresh JS/CSS the moment the file on disk changes.
    @app.context_processor
    def _inject_versioned_url_for():
        def versioned_url_for(endpoint: str, **values):
            if endpoint == "static":
                filename = values.get("filename")
                if filename:
                    fp = os.path.join(app.static_folder, filename)
                    try:
                        values["v"] = int(os.stat(fp).st_mtime)
                    except OSError:
                        pass
            return url_for(endpoint, **values)
        return {"url_for": versioned_url_for}

    return app
