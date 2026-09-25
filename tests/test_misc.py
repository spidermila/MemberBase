import smtplib
from datetime import UTC, datetime

import ldap
import pytest
import responses

from memberbase import config, directory, forms, keycloak, mail, people
from tests.conftest import login


def text(resp) -> str:
    return resp.get_data(as_text=True)


# ── Profile, changelog ───────────────────────────────────────────────────────


def test_profile_shows_own_data_roles_and_qualifications(client, world, admin):
    people.save_qualification(None, "Profilová kvalifikace", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == "Profilová kvalifikace")
    person = world.person(world.unit(), "Petr Profil", roles=["medcover:member"])
    people.set_holdings(person, {qual.id}, admin.dn)
    login(client, person)
    page = text(client.get("/profile"))
    assert "Petr Profil" in page and "MedCover: Member" in page and "Profilová kvalifikace" in page
    assert "kc_action=CONFIGURE_TOTP" in page


def test_profile_phone_update(client, world):
    person = world.person(world.unit())
    login(client, person)
    resp = client.post("/profile", data={"phone": "+420 777 000 111", "csn": person.csn}, follow_redirects=True)
    assert "Telefon uložen." in text(resp)
    assert people.find_person(person.id, person.dn).phone == "+420777000111"
    assert "Telefon zadejte" in text(client.post("/profile", data={"phone": "abc"}, follow_redirects=True))
    stale = client.post("/profile", data={"phone": "777000222", "csn": person.csn}, follow_redirects=True)
    assert "Údaje se mezitím změnily." in text(stale)


def test_changelog_is_public(client):
    assert "Chystané změny" in text(client.get("/changelog"))


def test_landing_redirects_logged_in(client, admin):
    login(client, admin)
    assert client.get("/").headers["Location"] == "/members/"


def test_admin_menu_hidden_without_debug(client, admin):
    login(client, admin)
    assert ">Admin<" not in text(client.get("/profile"))


def test_admin_menu_hidden_for_non_admin_even_in_debug(client, app, world):
    app.config["DEBUG"] = True
    login(client, world.person(world.unit()))
    assert ">Admin<" not in text(client.get("/profile"))


def test_admin_menu_shown_for_admin_in_debug(client, app, admin):
    app.config["DEBUG"] = True
    login(client, admin)
    page = text(client.get("/profile"))
    assert ">Admin<" in page
    assert "http://kc.test/admin/master/console/#/crc/users" in page
    assert "http://mb.test:8025" in page


def test_directory_refusal_renders_403(client, world, monkeypatch):
    login(client, world.person(world.unit()))

    def refuse(*_a, **_k):
        raise directory.Denied()

    monkeypatch.setattr(people, "list_qualifications", refuse)
    assert client.get("/profile").status_code == 403


# ── Directory wrapper ────────────────────────────────────────────────────────


def test_entry_helpers():
    entry = directory.Entry("cn=a,ou=b", {"x": ["1", "2"]})
    assert entry.first("x") == "1" and entry.first("y", "d") == "d"
    assert entry.all("y") == [] and entry.parent_dn == "ou=b"


def test_get_missing_and_value_helpers(app, world, admin):
    assert directory.get(f"cn=missing,{directory.base_dn()}") is None
    unit = world.unit()
    person = world.person(unit)
    directory.add_values(unit.members_dn, "member", [person.dn], admin.dn)  # already there: no error
    directory.delete_values(unit.readers_dn("basic"), "member", [person.dn], admin.dn)  # absent: no error


def test_close_without_connection(app):
    directory.close()


def test_filter_escaping_blocks_injection(app, admin):
    assert people.search_people(admin.dn, "*)(cn=*") == []
    assert people.search_people(admin.dn, "*") == []
    assert people.find_person("*", admin.dn) is None
    assert people.find_person(")(objectClass=*", admin.dn) is None
    assert people.search_people(admin.dn, "\\") is not None
    control = [p.id for p in people.search_people(admin.dn, "Nov\x00ák")]
    assert control == [p.id for p in people.search_people(admin.dn, "Novák")]


def test_phone_search_and_partial_update(app, world, admin):
    person = world.person(world.unit(), phone="601234567")
    assert [p.id for p in people.search_people(admin.dn, "601 234")] == [person.id]
    people.update_person(person, {"name": "Jen Jméno"}, admin.dn, person.csn)
    after = people.find_person(person.id, admin.dn)
    assert (after.name, after.phone) == ("Jen Jméno", "601234567")


def test_stale_move_of_external_user(app, world, admin):
    person = world.person(world.external())
    with pytest.raises(directory.StaleEntry):
        people.move_person(person, world.unit(), admin.dn, "old")
    assert people.find_person(person.id, admin.dn).kind == "external"


def test_other_ldap_errors_propagate(app, admin):
    with pytest.raises(ldap.NO_SUCH_OBJECT):
        directory.modify(f"cn=missing,{directory.base_dn()}", {"cn": ["x"]}, admin.dn)


# ── Forms ────────────────────────────────────────────────────────────────────


def test_forms():
    assert forms.clean_name("x" * 121)[1]
    assert forms.clean_name("  Jan   Novák ") == ("Jan Novák", None)
    assert forms.clean_email(" A@B.cz ") == ("a@b.cz", None)
    assert forms.clean_phone("") == ("", None)
    assert forms.clean_phone("00420777123456") == ("00420777123456", None)
    assert forms.clean_date("") == (None, None)
    assert forms.clean_date("2030-01-31")[0] == datetime(2030, 1, 31, 23, 59, 59, tzinfo=UTC)


def test_split_name_and_times():
    assert people.split_name("Cher") == ("", "Cher")
    assert people.split_name("Jan Petr Novák") == ("Jan Petr", "Novák")
    assert people.parse_ldap_time("") is None


# ── Keycloak client ──────────────────────────────────────────────────────────


@responses.activate
def test_keycloak_token_failure(app):
    responses.post("http://kc.internal/realms/crc/protocol/openid-connect/token", status=401)
    with pytest.raises(keycloak.KeycloakError, match="token: HTTP 401"):
        keycloak.logout("a@example.org")


@responses.activate
def test_keycloak_http_error(app):
    responses.post("http://kc.internal/realms/crc/protocol/openid-connect/token", json={"access_token": "t"})
    responses.get("http://kc.internal/admin/realms/crc/users", status=403)
    with pytest.raises(keycloak.KeycloakError, match="GET /users: HTTP 403"):
        keycloak.user_id("a@example.org")


@responses.activate
def test_keycloak_calls_present_the_public_host(app):
    responses.post("http://kc.internal/realms/crc/protocol/openid-connect/token", json={"access_token": "t"})
    responses.post("http://kc.internal/admin/realms/crc/users/x/logout")
    responses.get("http://kc.internal/admin/realms/crc/users", json=[{"id": "x"}])
    app.config["KEYCLOAK_PUBLIC_URL"] = "{scheme}://{hostname}:8180"
    with app.test_request_context("/", base_url="http://zerver:5100"):
        keycloak.logout("a@example.org")
    for call in responses.calls:
        assert call.request.headers["X-Forwarded-Host"] == "zerver:8180"
        assert call.request.headers["X-Forwarded-Port"] == "8180"
    app.config["KEYCLOAK_PUBLIC_URL"] = "https://sso.example.org"
    with app.test_request_context("/"):
        assert keycloak._forwarded()["X-Forwarded-Port"] == "443"
    assert keycloak._forwarded() == {}  # no request (scheduled jobs)


# ── Mail ─────────────────────────────────────────────────────────────────────


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail = False

    def __init__(self, host, port, timeout):
        self.calls = [("connect", host, port)]
        FakeSMTP.instances.append(self)

    def __enter__(self):
        if FakeSMTP.fail:
            raise smtplib.SMTPException("boom")
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"]))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_mail_not_configured(app, smtp):
    assert mail.send("a@example.org", "S", "B") is False
    assert smtp.instances == []


def test_mail_sent_with_tls_and_login(app, smtp):
    app.config.update(SMTP_HOST="smtp.test", SMTP_USER="u", SMTP_PASSWORD="p")
    assert mail.send("a@example.org", "Předmět", "Text") is True
    assert smtp.instances[0].calls == [
        ("connect", "smtp.test", 587),
        ("starttls",),
        ("login", "u"),
        ("send", "a@example.org", "Předmět"),
    ]


def test_mail_plain_without_login(app, smtp):
    app.config.update(SMTP_HOST="smtp.test", SMTP_STARTTLS=False)
    assert mail.send("a@example.org", "S", "B") is True
    assert smtp.instances[0].calls[1][0] == "send"


def test_mail_failure_is_not_raised(app, smtp):
    app.config.update(SMTP_HOST="smtp.test")
    smtp.fail = True
    assert mail.send("a@example.org", "S", "B") is False


# ── Config ───────────────────────────────────────────────────────────────────


def test_missing_required_setting(monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config.from_env()
