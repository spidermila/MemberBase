"""Keycloak admin API, used for what the directory cannot do: invite and
reset emails, ending sessions, removing second factors. Authenticates with
MemberBase's own client (client-credentials grant)."""

from typing import Any
from urllib.parse import urlsplit

import requests
from flask import current_app, g, has_request_context

from memberbase import auth

MFA_CREDENTIAL_TYPES = {"otp", "webauthn", "webauthn-passwordless"}
INVITE_LIFESPAN_SECONDS = 72 * 3600
TIMEOUT = 10


class KeycloakError(Exception):
    pass


def _forwarded() -> dict[str, str]:
    """Make Keycloak build links (e.g. in invitation emails) for the host the
    admin is using. Token and admin call must agree, or the token's issuer
    does not match."""
    if not has_request_context():
        return {}
    url = urlsplit(auth.public_keycloak_url())
    port = url.port or (443 if url.scheme == "https" else 80)
    return {"X-Forwarded-Proto": url.scheme, "X-Forwarded-Host": url.netloc, "X-Forwarded-Port": str(port)}


def _realm_url() -> str:
    cfg = current_app.config
    return f"{cfg['KEYCLOAK_INTERNAL_URL']}/admin/realms/{cfg['KEYCLOAK_REALM']}"


def service_token() -> str:
    """MemberBase's own access token, fetched once per request."""
    if "kc_token" not in g:
        g.kc_token = _fetch_token()
    return g.kc_token


def _fetch_token() -> str:
    cfg = current_app.config
    resp = requests.post(
        f"{cfg['KEYCLOAK_INTERNAL_URL']}/realms/{cfg['KEYCLOAK_REALM']}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials"},
        auth=(cfg["OIDC_CLIENT_ID"], cfg["OIDC_CLIENT_SECRET"]),
        headers=_forwarded(),
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise KeycloakError(f"token: HTTP {resp.status_code}")
    return resp.json()["access_token"]


def _call(method: str, path: str, **kwargs: Any) -> requests.Response:
    try:
        resp = requests.request(
            method,
            _realm_url() + path,
            headers={"Authorization": f"Bearer {service_token()}", **_forwarded()},
            timeout=TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise KeycloakError(str(exc)) from exc
    if not resp.ok:
        raise KeycloakError(f"{method} {path}: HTTP {resp.status_code}")
    return resp


def user_id(email: str) -> str:
    """Keycloak's id for the person; the lookup imports them from LDAP."""
    users = _call("GET", "/users", params={"email": email, "exact": "true"}).json()
    if not users:
        raise KeycloakError("user not found")
    return users[0]["id"]


def send_invite(email: str, redirect_uri: str) -> None:
    _call(
        "PUT",
        f"/users/{user_id(email)}/execute-actions-email",
        params={
            "lifespan": INVITE_LIFESPAN_SECONDS,
            "client_id": current_app.config["OIDC_CLIENT_ID"],
            "redirect_uri": redirect_uri,
        },
        json=["UPDATE_PASSWORD"],
    )


def logout(email: str) -> None:
    _call("POST", f"/users/{user_id(email)}/logout")


def reset_mfa(email: str) -> int:
    """Remove every second factor. Returns how many were removed."""
    uid = user_id(email)
    creds = [c for c in _call("GET", f"/users/{uid}/credentials").json() if c["type"] in MFA_CREDENTIAL_TYPES]
    for cred in creds:
        _call("DELETE", f"/users/{uid}/credentials/{cred['id']}")
    return len(creds)
