import time
import uuid

import pytest

from memberbase import create_app
from memberbase import directory as d
from memberbase import people


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


class World:
    """Builds throwaway data as the bootstrap admin; unique names per test."""

    def __init__(self, admin: people.Person) -> None:
        self.admin = admin

    def unit(self, name: str | None = None) -> people.Unit:
        return people.create_unit(name or f"Skupina {uuid.uuid4().hex[:8]}", self.admin.dn)

    def person(self, unit: people.Unit, name: str = "Jan Novák", status: str = "active", roles=(), **kw):
        email = kw.get("email") or f"{uuid.uuid4().hex[:10]}@example.org"
        person = people.create_person(name, email, kw.get("phone", "123456789"), unit, self.admin.dn)
        if status != "new":
            people.set_status(person, status, self.admin.dn)
        for key in roles:
            role = next(r for r in people.list_roles(self.admin.dn) if r.key == key)
            people.toggle_role(person, role, True, self.admin.dn)
        found = people.find_person(person.id, self.admin.dn)
        assert found is not None
        return found

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
