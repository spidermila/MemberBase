import time
import uuid

import pytest
import responses

from memberbase import create_app
from memberbase import directory as d
from memberbase import mail, people


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SERVER_NAME": "mb.test"})
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app) -> people.Person:
    """The bootstrap admin from the test directory."""
    found = d.search(people.units_base(), "(mail=admin@example.org)", ["crcMemberId"])
    person = people.find_person(found[0].first("crcMemberId"), None)
    assert person is not None
    return person


def _split(name: str) -> tuple[str, str]:
    """Surname and given name of a "Surname Given" test name."""
    surname, _, given = name.partition(" ")
    return surname, given


class World:
    """Builds throwaway data as the bootstrap admin; unique names per test."""

    def __init__(self, admin: people.Person) -> None:
        self.admin = admin

    def unit(self, name: str | None = None) -> people.Unit:
        return people.create_unit(name or f"Skupina {uuid.uuid4().hex[:8]}", self.admin.dn)

    def person(self, unit: people.Unit, name: str = "Novák Jan", status: str = "active", roles=(), **kw):
        email = kw.get("email") or f"{uuid.uuid4().hex[:10]}@example.org"
        person = people.create_person(*_split(name), email, kw.get("phone", "123456789"), unit, self.admin.dn)
        if status != "new":
            people.set_status(person, status, self.admin.dn)
        for key in roles:
            role = next(r for r in people.list_roles(self.admin.dn) if r.key == key)
            people.toggle_role(person, role, True, self.admin.dn)
        found = people.find_person(person.id, self.admin.dn)
        assert found is not None
        return found

    def chair(self, unit: people.Unit, name: str = "Předsedkyně Petra", **kw):
        person = self.person(unit, name, **kw)
        people.set_chair(person, True, self.admin.dn)
        return person

    def external(self) -> people.Unit:
        unit = people.get_unit("external", self.admin.dn)
        assert unit is not None
        return unit


@pytest.fixture
def world(admin) -> World:
    return World(admin)


def login(client, person: people.Person, auth_time: float | None = None) -> None:
    with client.session_transaction() as sess:
        sess["member_id"] = person.id
        sess["auth_time"] = time.time() if auth_time is None else auth_time
        sess["id_token"] = "id-token"


KC = "http://kc.internal"
TOKEN_URL = f"{KC}/realms/crc/protocol/openid-connect/token"


@pytest.fixture
def kc():
    with responses.RequestsMock() as rsps:
        rsps.post(TOKEN_URL, json={"access_token": "svc"})
        yield rsps


@pytest.fixture
def sent(monkeypatch):
    mails: list[tuple[str, str]] = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: mails.append((to, subject)) or True)
    return mails


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.org"


def text(resp) -> str:
    return resp.get_data(as_text=True)


def kc_user(kc, email: str, kc_id: str = "kc-1") -> None:
    kc.get(
        f"{KC}/admin/realms/crc/users",
        json=[{"id": kc_id}],
        match=[responses.matchers.query_param_matcher({"email": email, "exact": "true"})],
    )
