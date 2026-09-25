"""MemberBase („Evidence členů“): administration of the member directory."""

from typing import Any

from flask import Flask, g, render_template
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

from memberbase import auth, cli, directory, people
from memberbase.config import from_env
from memberbase.views import admin, main, members, requests

csrf = CSRFProtect()


def create_app(overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(from_env())
    app.config.update(overrides or {})
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[method-assign]
    csrf.init_app(app)
    auth.init_app(app)
    app.register_blueprint(main.bp)
    app.register_blueprint(members.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(requests.bp)
    cli.init_app(app)
    app.teardown_appcontext(directory.close)

    @app.context_processor
    def _globals() -> dict[str, Any]:
        ctx: dict[str, Any] = {"me": g.get("me"), "git_commit": app.config["GIT_COMMIT"], "apps": people.APPS}
        if app.debug:
            # Dev-only shortcuts to the other stack services; never shown when
            # running from the production image (no --debug there).
            ctx["keycloak_admin_url"] = (
                f"{auth.public_keycloak_url()}/admin/master/console/#/{app.config['KEYCLOAK_REALM']}/users"
            )
            ctx["mailpit_url"] = auth.public_mailpit_url()
        return ctx

    @app.after_request
    def _security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            f"form-action 'self' {auth.public_keycloak_url()}; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(403)
    def _forbidden(_exc: Exception) -> tuple[str, int]:
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def _not_found(_exc: Exception) -> tuple[str, int]:
        return render_template("errors/404.html"), 404

    @app.errorhandler(directory.Denied)
    def _denied(_exc: Exception) -> tuple[str, int]:
        return render_template("errors/403.html"), 403

    @app.route("/health")
    def health() -> str:
        return "ok"

    return app
