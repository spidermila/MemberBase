import time
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from memberbase import auth, people
from tests.conftest import login

# Metadata as Keycloak serves it on its internal address.
METADATA = {
    "issuer": "http://kc.internal/realms/crc",
    "authorization_endpoint": "http://kc.internal/realms/crc/protocol/openid-connect/auth",
    "token_endpoint": "http://kc.internal/realms/crc/protocol/openid-connect/token",
    "jwks_uri": "http://kc.internal/realms/crc/protocol/openid-connect/certs",
}


@pytest.fixture
def kc_metadata():
    # Authlib caches the metadata, so later tests may not fetch it.
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get("http://kc.internal/realms/crc/.well-known/openid-configuration", json=METADATA)
        yield rsps


def authorize_params(resp) -> dict[str, list[str]]:
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("http://kc.test/realms/crc/protocol/openid-connect/auth?")
    return parse_qs(urlsplit(location).query)


def test_login_redirects_to_keycloak_with_pkce(client, kc_metadata):
    params = authorize_params(client.get("/login?next=/members/"))
    assert params["code_challenge_method"] == ["S256"]
    assert params["client_id"] == ["memberbase"]
    assert "kc_action" not in params and "max_age" not in params
    with client.session_transaction() as sess:
        assert sess["next"] == "/members/"


def test_public_keycloak_url_follows_request_host(app, client, kc_metadata):
    app.config["KEYCLOAK_PUBLIC_URL"] = "{scheme}://{hostname}:8180"
    resp = client.get("/login", base_url="https://zerver.lan:5100")
    assert resp.headers["Location"].startswith("https://zerver.lan:8180/realms/crc/protocol/openid-connect/auth?")


def test_login_passes_known_keycloak_action(client, kc_metadata):
    params = authorize_params(client.get("/login?kc_action=CONFIGURE_TOTP"))
    assert params["kc_action"] == ["CONFIGURE_TOTP"]


def test_login_ignores_unknown_keycloak_action(client, kc_metadata):
    assert "kc_action" not in authorize_params(client.get("/login?kc_action=delete_account"))


def test_reauth_forces_fresh_login(client, kc_metadata):
    params = authorize_params(client.get("/login?reauth=1"))
    assert params["max_age"] == ["0"] and params["prompt"] == ["login"]


@pytest.mark.parametrize(
    "target, expected",
    [
        (None, "/"),
        ("/members/?q=a", "/members/?q=a"),
        ("http://mb.test/profile", "/profile"),
        ("https://evil.example/x", "/"),
        ("//evil.example/x", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_next_is_never_an_open_redirect(app, target, expected):
    with app.test_request_context("/", base_url="http://mb.test"):
        assert auth._safe_next(target) == expected


def fake_token(monkeypatch, claims: dict) -> None:
    token = {"userinfo": claims, "id_token": "the-id-token"}

    def authorize_access_token(claims_options):
        assert claims_options["iss"]["values"] == ["http://kc.test/realms/crc"]
        return token

    monkeypatch.setattr(auth.oauth.keycloak, "authorize_access_token", authorize_access_token)


def test_callback_logs_active_person_in(client, admin, monkeypatch):
    fake_token(monkeypatch, {"crc_member_id": admin.id, "auth_time": 1234})
    with client.session_transaction() as sess:
        sess["next"] = "/profile"
    resp = client.get("/auth/callback?code=x&state=y")
    assert resp.headers["Location"] == "/profile"
    with client.session_transaction() as sess:
        assert sess["member_id"] == admin.id
        assert sess["auth_time"] == 1234
        assert sess["id_token"] == "the-id-token"
        assert "next" not in sess


def test_callback_activates_invited_person(client, world, monkeypatch):
    person = world.person(world.unit(), status="invited")
    fake_token(monkeypatch, {"crc_member_id": person.id})
    assert client.get("/auth/callback").status_code == 302
    assert people.find_person(person.id, None).status == "active"
    with client.session_transaction() as sess:
        assert abs(sess["auth_time"] - time.time()) < 5


@pytest.mark.parametrize("status", ["inactive", "former"])
def test_callback_refuses_non_active_person(client, world, monkeypatch, status):
    person = world.person(world.unit(), status=status)
    fake_token(monkeypatch, {"crc_member_id": person.id})
    resp = client.get("/auth/callback")
    assert resp.status_code == 403
    assert "Přihlášení odmítnuto" in resp.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert "member_id" not in sess


def test_callback_refuses_unknown_person(client, monkeypatch):
    fake_token(monkeypatch, {})
    assert client.get("/auth/callback").status_code == 403


def test_callback_after_cancelled_keycloak_action(client):
    with client.session_transaction() as sess:
        sess["next"] = "/profile"
    resp = client.get("/auth/callback?error=access_denied")
    assert resp.headers["Location"] == "/profile"


def test_logout_ends_keycloak_session(client, admin):
    login(client, admin)
    resp = client.get("/logout")
    query = parse_qs(urlsplit(resp.headers["Location"]).query)
    assert resp.headers["Location"].startswith("http://kc.test/realms/crc/protocol/openid-connect/logout?")
    assert query["id_token_hint"] == ["id-token"]
    assert query["post_logout_redirect_uri"] == ["http://mb.test/"]
    with client.session_transaction() as sess:
        assert "member_id" not in sess


def test_logout_without_session(client):
    query = parse_qs(urlsplit(client.get("/logout").headers["Location"]).query)
    assert "id_token_hint" not in query


def test_deactivated_person_is_logged_out_on_next_request(client, world, admin):
    person = world.person(world.unit())
    login(client, person)
    assert client.get("/profile").status_code == 200
    people.set_status(person, "inactive", admin.dn)
    resp = client.get("/profile")
    assert resp.headers["Location"] == "/"
    with client.session_transaction() as sess:
        assert "member_id" not in sess


def test_anonymous_is_sent_to_login(client):
    resp = client.get("/members/")
    assert resp.headers["Location"].startswith("/login?next=")


def test_member_cannot_open_admin_pages(client, world):
    login(client, world.person(world.unit()))
    for url in ["/units", "/qualifications", "/grants", "/history", "/permissions", "/members/new", "/members/invites"]:
        assert client.get(url).status_code == 403, url


def test_static_files_skip_the_person_lookup(client, admin, monkeypatch):
    login(client, admin)
    monkeypatch.setattr(people, "find_person", lambda *a: pytest.fail("looked up"))
    assert client.get("/static/css/main.css").status_code == 200


def test_step_up_redirects_when_login_is_old(client, world, admin):
    person = world.person(world.unit())
    login(client, admin, auth_time=time.time() - 3600)
    resp = client.post(
        f"/members/{person.id}/roles", data={"roles": ["medcover:member"]}, headers={"Referer": "/members/x"}
    )
    assert resp.headers["Location"].startswith("/login?next=/members/x&reauth=1")
    assert people.roles_of(person.dn) == set()


def test_security_headers(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "form-action 'self' http://kc.test" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_not_found_page(client, admin):
    login(client, admin)
    assert client.get("/nowhere").status_code == 404
