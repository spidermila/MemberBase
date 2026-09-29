"""External users: managed by District Coordinators, invited to MedCover,
never logged in to MemberBase, holding only the MedCover role "external"."""

from urllib.parse import parse_qs, urlsplit

import pytest

from memberbase import directory, people
from tests.conftest import KC, kc_user, login, text, unique
from tests.test_auth import fake_token


@pytest.fixture
def dc(world):
    return world.person(world.unit(), "Dana Koordinátorka", roles=["memberbase:district-coordinator"])


def _roles(person: people.Person) -> set[str]:
    return people.memberships(person.dn)[0]


def test_external_user_cannot_log_in(client, world, monkeypatch):
    ext = world.person(world.external(), "Eva Externí", status="invited")
    fake_token(monkeypatch, {"crc_member_id": ext.id})
    resp = client.get("/auth/callback")
    assert resp.status_code == 403 and "Přihlaste se do aplikace MedCover" in text(resp)
    assert people.find_person(ext.id, None).status == "invited"  # MedCover activates them
    with client.session_transaction() as sess:
        assert "member_id" not in sess


def test_external_user_session_is_ended(client, world):
    ext = world.person(world.external(), "Eva Externí")
    login(client, ext)
    assert "Přihlaste se do aplikace MedCover" in text(client.get("/members/", follow_redirects=True))
    with client.session_transaction() as sess:
        assert "member_id" not in sess


def test_district_coordinator_creates_and_invites_external_users_only(client, world, dc, kc):
    email = unique("host")
    kc_user(kc, email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, dc)
    assert "+ Nová osoba" in text(client.get("/members/"))
    options = text(client.get("/members/new")).split("<select")[1].split("</select>")[0]
    assert "Externí uživatelé" in options and "Skupina" not in options
    data = {"surname": "Host", "given_name": "Nový", "email": email, "unit": "external", "invite": "1"}
    client.post("/members/new", data=data)
    ext = people.search_people(dc.dn, email)[0]
    assert (ext.kind, ext.status, _roles(ext)) == ("external", "invited", {people.EXTERNAL_ROLE})
    params = parse_qs(urlsplit(kc.calls[-1].request.url).query)
    assert params["client_id"] == ["medcover"] and "redirect_uri" not in params
    data = {"surname": "Člen", "given_name": "Nový", "email": unique("clen"), "unit": world.unit().id}
    assert "Vyberte místní skupinu." in text(client.post("/members/new", data=data, follow_redirects=True))


def test_members_are_invited_back_to_memberbase(client, world, admin, kc):
    person = world.person(world.unit(), status="new")
    kc_user(kc, person.email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, admin)
    client.post(f"/members/{person.id}/invite")
    params = parse_qs(urlsplit(kc.calls[-1].request.url).query)
    assert params["client_id"] == ["memberbase"] and params["redirect_uri"] == ["http://mb.test/"]


def test_district_coordinator_manages_external_users(client, world, admin, dc):
    ext = world.person(world.external(), "Eva Externí")
    member = world.person(world.unit(), "Jana Členka")
    people.save_qualification(None, f"Kval {dc.id}", "", [], True, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == f"Kval {dc.id}")
    login(client, dc)
    page = text(client.get(f"/members/{ext.id}"))
    assert "Uložit kvalifikace" in page and "Deaktivovat" in page and "Uložit role" not in page
    client.post(
        f"/members/{ext.id}/edit", data={"surname": "Nová", "given_name": "Eva", "email": ext.email, "csn": ext.csn}
    )
    client.post(f"/members/{ext.id}/qualifications", data={"quals": [qual.id]})
    ext = people.find_person(ext.id, dc.dn)
    client.post(f"/members/{ext.id}/status/deactivate", data={"csn": ext.csn})
    ext = people.find_person(ext.id, dc.dn)
    assert (ext.name, ext.status) == ("Nová Eva", "inactive")
    assert set(people.holdings_of(ext, dc.dn)) == {qual.id}
    assert "Deaktivovat" not in text(client.get(f"/members/{member.id}"))
    assert client.post(f"/members/{member.id}/edit", data={"surname": "X"}).status_code == 403


def test_district_coordinator_cannot_change_privileged_external_users(client, world, dc):
    boss = world.person(world.external(), "Olga Oprávněná", roles=["medcover:coordinator"])
    login(client, dc)
    assert "Deaktivovat" not in text(client.get(f"/members/{boss.id}"))
    data = {"surname": "Změna", "given_name": "Olga", "email": boss.email, "csn": boss.csn}
    assert "smí měnit jen Admin" in text(client.post(f"/members/{boss.id}/edit", data=data, follow_redirects=True))


def test_only_the_external_role_is_offered_to_external_users(client, world, admin):
    ext = world.person(world.external(), "Eva Externí")
    member = world.person(world.unit(), "Jana Členka")
    login(client, admin)
    page = text(client.get(f"/members/{ext.id}"))
    assert 'value="medcover:external"' in page and 'value="medcover:member"' not in page
    page = text(client.get(f"/members/{member.id}"))
    assert 'value="medcover:member"' in page and 'value="medcover:external"' not in page
    client.post(f"/members/{ext.id}/roles", data={"roles": ["medcover:member", "medcover:external"]})
    client.post(f"/members/{member.id}/roles", data={"roles": ["medcover:member", "medcover:external"]})
    assert _roles(ext) == {people.EXTERNAL_ROLE} and _roles(member) == {"medcover:member"}
    client.post("/members/batch", data={"member_ids": [ext.id, member.id], "role": "medcover:viewer", "action": "add"})
    assert _roles(ext) == {people.EXTERNAL_ROLE} and _roles(member) == {"medcover:member", "medcover:viewer"}


def test_moving_to_and_from_external_users_swaps_the_roles(client, world, admin):
    unit = world.unit()
    person = world.person(unit, roles=["medcover:member", "memberbase:district-coordinator"])
    login(client, admin)
    client.post(f"/members/{person.id}/move", data={"unit": "external", "csn": person.csn})
    moved = people.find_person(person.id, admin.dn)
    assert _roles(moved) == {people.EXTERNAL_ROLE}
    client.post(f"/members/{person.id}/move", data={"unit": unit.id, "csn": moved.csn})
    assert _roles(people.find_person(person.id, admin.dn)) == set()


def test_stale_move_keeps_the_roles(world, admin):
    person = world.person(world.unit(), roles=["medcover:member"])
    with pytest.raises(directory.StaleEntry):
        people.move_person(person, world.external(), admin.dn, "old")
    assert _roles(people.find_person(person.id, admin.dn)) == {"medcover:member"}


def test_restoring_an_archived_external_user_gives_the_role_back(client, world, admin, dc):
    ext = world.person(world.external(), "Eva Externí")
    login(client, admin)
    client.post(f"/members/{ext.id}/status/archive", data={"csn": ext.csn})
    assert _roles(ext) == set()
    login(client, dc)
    ext = people.find_person(ext.id, dc.dn)
    client.post(f"/members/{ext.id}/status/restore", data={"csn": ext.csn})
    assert _roles(ext) == {people.EXTERNAL_ROLE}


def test_failed_move_keeps_the_roles(world, admin, monkeypatch):
    person = world.person(world.unit(), roles=["medcover:member"])

    def fail(*args, **kwargs):
        raise directory.Denied()

    monkeypatch.setattr(directory, "move", fail)
    with pytest.raises(directory.Denied):
        people.move_person(person, world.external(), admin.dn, person.csn)
    assert _roles(person) == {"medcover:member"}


def test_nobody_moves_themselves_to_external_users(client, world, admin):
    me = world.person(world.unit(), "Adam Správce", roles=["memberbase:admin"])
    other = world.person(world.unit(), "Jana Členka")
    login(client, me)
    page = text(client.post(f"/members/{me.id}/move", data={"unit": "external", "csn": me.csn}, follow_redirects=True))
    assert "Sami sebe mezi externí uživatele" in page
    client.post("/members/batch-move", data={"member_ids": [me.id, other.id], "target": "external"})
    assert people.find_person(me.id, admin.dn).kind == "member"
    assert people.find_person(other.id, admin.dn).kind == "external"
    assert "memberbase:admin" in _roles(me)
