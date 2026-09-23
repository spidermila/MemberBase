"""Login through Keycloak (OIDC authorization code + PKCE), the logged-in
person on every request, and permission and step-up checks."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any
from urllib.parse import urlencode, urlsplit

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, Flask, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

from memberbase import people
from memberbase.permissions import permissions_for

bp = Blueprint("auth", __name__)
oauth = OAuth()

# Keycloak required actions a person can start from their profile.
KC_ACTIONS = {"UPDATE_PASSWORD", "CONFIGURE_TOTP", "webauthn-register", "webauthn-register-passwordless"}


@dataclass
class Me:
    person: people.Person
    roles: set[str]
    permissions: set[str]

    @property
    def dn(self) -> str:
        return self.person.dn

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def init_app(app: Flask) -> None:
    cfg = app.config
    oauth.init_app(app)
    oauth.register(
        "keycloak",
        client_id=cfg["OIDC_CLIENT_ID"],
        client_secret=cfg["OIDC_CLIENT_SECRET"],
        server_metadata_url=(
            f"{cfg['KEYCLOAK_INTERNAL_URL']}/realms/{cfg['KEYCLOAK_REALM']}/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid", "code_challenge_method": "S256"},
    )
    app.before_request(load_me)
    app.register_blueprint(bp)


def public_keycloak_url() -> str:
    """Keycloak's browser-facing base URL for the current request."""
    return current_app.config["KEYCLOAK_PUBLIC_URL"].format(
        scheme=request.scheme, hostname=request.host.rsplit(":", 1)[0]
    )


def load_me() -> Response | None:
    """Re-read the logged-in person on every request, so a deactivation or a
    role change applies at once."""
    g.me = None
    member_id = session.get("member_id")
    if member_id is None or request.endpoint == "static":
        return None
    person = people.find_person(member_id, None)
    if person is None or person.status != "active":
        session.clear()
        flash("Váš účet není aktivní.", "danger")
        return redirect(url_for("main.index"))
    app_roles = people.roles_of(person.dn)
    mb_roles = {r.split(":", 1)[1] for r in app_roles if r.startswith("memberbase:")}
    g.me = Me(person, app_roles, permissions_for(mb_roles))
    return None


def me() -> Me:
    return g.me


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if g.get("me") is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def require(permission: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        @login_required
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not g.me.can(permission):
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def step_up() -> Response | None:
    """Step-up for sensitive actions: unless the login is recent, return a
    redirect to log in again; the person then repeats the action."""
    if time.time() - session.get("auth_time", 0) <= current_app.config["STEP_UP_SECONDS"]:
        return None
    flash("Tato akce vyžaduje nedávné přihlášení. Přihlaste se znovu a akci opakujte.", "warning")
    return redirect(url_for("auth.login", next=_safe_next(request.referrer), reauth=1))


def recent_login(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return step_up() or view(*args, **kwargs)

    return wrapper


def _safe_next(target: str | None) -> str:
    """Only same-site relative paths, never an open redirect."""
    if not target:
        return url_for("main.index")
    parts = urlsplit(target)
    if parts.netloc and parts.netloc != request.host:
        return url_for("main.index")
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return url_for("main.index")
    return path + (f"?{parts.query}" if parts.query else "")


@bp.route("/login")
def login() -> Response:
    session["next"] = _safe_next(request.args.get("next"))
    extra: dict[str, Any] = {}
    kc_action = request.args.get("kc_action")
    if kc_action in KC_ACTIONS:
        extra["kc_action"] = kc_action
    if request.args.get("reauth"):
        extra["max_age"] = 0
        extra["prompt"] = "login"
    resp = oauth.keycloak.authorize_redirect(url_for("auth.callback", _external=True), **extra)
    # The metadata comes from Keycloak's internal address; the browser needs
    # the public one. Tokens are still exchanged internally.
    resp.location = resp.location.replace(current_app.config["KEYCLOAK_INTERNAL_URL"], public_keycloak_url(), 1)
    return resp


@bp.route("/auth/callback")
def callback() -> str | Response:
    if "error" in request.args:
        # E.g. the person cancelled an account action in Keycloak.
        flash("Přihlášení nebo akce v účtu nebyla dokončena.", "warning")
        return redirect(session.pop("next", url_for("main.index")))
    # Keycloak issues the ID token under the host the browser used.
    issuer = f"{public_keycloak_url()}/realms/{current_app.config['KEYCLOAK_REALM']}"
    token = oauth.keycloak.authorize_access_token(claims_options={"iss": {"essential": True, "values": [issuer]}})
    claims = token["userinfo"]
    person = people.find_person(claims.get("crc_member_id", ""), None)
    if person is not None and person.status == "invited":
        people.activate_invited(person)
        person.status = "active"
    if person is None or person.status != "active":
        return render_template("errors/login_refused.html"), 403  # type: ignore[return-value]
    target = session.pop("next", url_for("main.index"))
    session.clear()
    session.permanent = True
    session["member_id"] = person.id
    session["auth_time"] = int(claims.get("auth_time", time.time()))
    session["id_token"] = token.get("id_token", "")
    return redirect(target)


@bp.route("/logout")
def logout() -> Response:
    id_token = session.get("id_token", "")
    session.clear()
    cfg = current_app.config
    end_session = f"{public_keycloak_url()}/realms/{cfg['KEYCLOAK_REALM']}/protocol/openid-connect/logout"
    params = {"post_logout_redirect_uri": url_for("main.index", _external=True), "client_id": cfg["OIDC_CLIENT_ID"]}
    if id_token:
        params["id_token_hint"] = id_token
    return redirect(f"{end_session}?{urlencode(params)}")
