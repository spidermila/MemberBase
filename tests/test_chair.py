"""MS Chair: administers the people of their own Místní skupina only."""

import pytest

from memberbase import people
from tests.conftest import KC, kc_user, login, text, unique


@pytest.fixture
def home(world):
    return world.unit()


@pytest.fixture
def chair(world, home):
    return world.chair(home)


def test_admin_appoints_and_removes_chair(client, world, admin, home):
    person = world.person(home, "Karel Kandidát")
    login(client, admin)
    assert "Předseda MS (" in text(client.get(f"/members/{person.id}"))
    client.post(f"/members/{person.id}/roles", data={"chair": "1"})
    assert people.memberships(person.dn)[1] == {home.dn.lower()}
    assert "Předseda MS</span>" in text(client.get(f"/members/{person.id}"))
    resp = client.post(f"/members/{person.id}/roles", data={})
    assert people.memberships(person.dn)[1] == set()
    assert "odebráno 1" in text(client.get(resp.headers["Location"]))


def test_external_user_cannot_be_chair(client, world, admin):
    ext = world.person(world.external(), "Eva Externí")
    login(client, admin)
    assert "Předseda MS (" not in text(client.get(f"/members/{ext.id}"))
    client.post(f"/members/{ext.id}/roles", data={"chair": "1"})
    assert people.memberships(ext.dn)[1] == set()


def test_chair_sees_management_screens(client, chair, home):
    login(client, chair)
    page = text(client.get("/members/"))
    assert "+ Nová osoba" in page and "Pozvánky" in page and "Žádosti" in page
    page = text(client.get("/members/new"))
    assert home.name in page and "Externí" not in page.split("<select")[1].split("</select>")[0]


def test_plain_member_gets_no_management(client, world, home):
    alice = world.person(home, "Alice Obyčejná")
    other = world.person(home, "Anna Obyčejná")
    login(client, alice)
    assert "+ Nová osoba" not in text(client.get("/members/"))
    assert client.get("/members/new").status_code == 403
    assert client.get("/members/invites").status_code == 403
    assert client.post("/members/invites/send", data={"member_ids": [other.id]}).status_code == 403
    assert client.post(f"/members/{other.id}/edit", data={"surname": "X"}).status_code == 403
    assert client.post(f"/members/{other.id}/status/deactivate").status_code == 403
    assert client.post(f"/members/{other.id}/qualifications").status_code == 403


def test_chair_creates_member_in_own_unit_only(client, world, chair, home):
    other = world.unit()
    login(client, chair)
    data = {"surname": "Člen", "given_name": "Nový", "email": unique("novy"), "unit": other.id}
    assert "Vyberte místní skupinu." in text(client.post("/members/new", data=data, follow_redirects=True))
    client.post("/members/new", data=data | {"unit": home.id})
    person = people.search_people(chair.dn, "Člen Nový", home)[0]
    # A Chair may not write cn=members: the person joins it at first login.
    assert person.dn not in people.d.get(home.members_dn, ["member"]).all("member")
    people.set_status(person, "invited", chair.dn)
    person = people.find_person(person.id, None)
    people.activate_invited(person)
    assert person.dn in people.d.get(home.members_dn, ["member"]).all("member")


def test_activating_external_user_joins_no_members_group(world, admin):
    ext = world.person(world.external(), "Eva Externí", status="invited")
    people.activate_invited(people.find_person(ext.id, None))
    assert people.find_person(ext.id, None).status == "active"


def test_chair_edits_status_and_qualifications_in_own_unit(client, world, admin, chair, home):
    person = world.person(home, "Jana Členka")
    stranger = world.person(world.unit(), "Cizí Osoba")
    people.save_qualification(None, f"Kval {chair.id}", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == f"Kval {chair.id}")
    login(client, chair)
    page = text(client.get(f"/members/{person.id}"))
    assert 'name="csn"' in page and "Uložit kvalifikace" in page and "Deaktivovat" in page
    client.post(
        f"/members/{person.id}/edit",
        data={"surname": "Nová", "given_name": "Jana", "email": person.email, "csn": person.csn},
    )
    client.post(f"/members/{person.id}/qualifications", data={"quals": [qual.id]})
    person = people.find_person(person.id, chair.dn)
    client.post(f"/members/{person.id}/status/deactivate", data={"csn": person.csn})
    person = people.find_person(person.id, chair.dn)
    assert (person.name, person.status) == ("Nová Jana", "inactive")
    assert set(people.holdings_of(person, chair.dn)) == {qual.id}
    # Other units: not even visible.
    assert client.post(f"/members/{stranger.id}/edit", data={"surname": "X"}).status_code == 404


@pytest.mark.parametrize("role", ["memberbase:admin", "medcover:coordinator"])
def test_chair_cannot_change_privileged_people(client, world, chair, home, role):
    boss = world.person(home, "Olga Oprávněná", roles=[role])
    login(client, chair)
    page = text(
        client.post(
            f"/members/{boss.id}/edit",
            data={"surname": "Změna", "given_name": "Jan", "email": boss.email, "csn": boss.csn},
            follow_redirects=True,
        )
    )
    assert "smí měnit jen Admin" in page
    page = text(client.post(f"/members/{boss.id}/status/deactivate", data={"csn": boss.csn}, follow_redirects=True))
    assert "smí měnit jen Admin" in page
    assert people.find_person(boss.id, chair.dn).status == "active"


@pytest.mark.parametrize("chair_role", [None, "medcover:viewer"])
def test_chair_archives_and_the_job_removes_roles(client, world, admin, chair, home, kc, chair_role):
    """A Chair with a MedCover role sees the role's members, but still may not change them."""
    if chair_role:
        role = next(r for r in people.list_roles(admin.dn) if r.key == chair_role)
        people.toggle_role(chair, role, True, admin.dn)
    person = world.person(home, "Rudolf Rolový", roles=["medcover:member"])
    kc_user(kc, person.email)
    kc.post(f"{KC}/admin/realms/crc/users/kc-1/logout")
    login(client, chair)
    resp = client.post(f"/members/{person.id}/status/archive", data={"csn": person.csn})
    assert resp.status_code == 302 and kc.calls[-1].request.url.endswith("/logout")
    assert people.find_person(person.id, chair.dn).status == "former"
    assert people.memberships(person.dn)[0] == {"medcover:member"}
    assert people.repair_members() >= 1
    assert people.memberships(person.dn)[0] == set()


def test_chair_invites_only_own_people(client, world, admin, chair, home, kc):
    mine = world.person(home, "Moje Pozvaná", status="new")
    other = world.unit()
    theirs = world.person(other, "Cizí Pozvaná", status="new")
    # Seeing people of another Místní skupina is not managing them.
    people.create_grant(chair.id, other, "contact", None, "", admin.id, admin.dn)
    kc_user(kc, mine.email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, chair)
    page = text(client.get("/members/invites"))
    assert "Moje Pozvaná" in page and "Cizí Pozvaná" not in page
    resp = client.post("/members/invites/send", data={"member_ids": [mine.id, theirs.id]}, follow_redirects=True)
    assert "Odesláno pozvánek: 1" in text(resp)
    client.post(f"/members/{mine.id}/cancel-invite")
    assert people.find_person(mine.id, chair.dn).status == "former"


def test_admin_move_of_chair_ends_chairing(client, world, admin, chair):
    login(client, admin)
    target = world.unit()
    client.post(f"/members/{chair.id}/move", data={"unit": target.id, "csn": chair.csn})
    assert people.memberships(people.find_person(chair.id, admin.dn).dn)[1] == set()


def test_admin_archive_of_chair_ends_chairing(client, world, admin, chair, kc):
    kc_user(kc, chair.email)
    kc.post(f"{KC}/admin/realms/crc/users/kc-1/logout")
    login(client, admin)
    client.post(f"/members/{chair.id}/status/archive", data={"csn": chair.csn})
    assert people.memberships(chair.dn)[1] == set()


def test_permissions_page_lists_chair(client, admin):
    login(client, admin)
    page = text(client.get("/permissions"))
    assert "Předseda MS" in page and "(ve své místní skupině)" in page


def test_chair_is_not_offered_changes_to_privileged_people(client, world, admin, chair, home):
    boss = world.person(home, "Olga Oprávněná", status="new", roles=["memberbase:district-coordinator"])
    people.save_qualification(None, f"Kval {chair.id}", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == f"Kval {chair.id}")
    login(client, chair)
    page = text(client.get(f"/members/{boss.id}"))
    assert "spravuje jen Admin" in page and "Uložit kvalifikace" not in page and "Poslat pozvánku" not in page
    assert "Olga Oprávněná" not in text(client.get("/members/invites"))
    page = text(client.post(f"/members/{boss.id}/qualifications", data={"quals": [qual.id]}, follow_redirects=True))
    assert "smí měnit jen Admin" in page
    assert people.holdings_of(boss, admin.dn) == {}
    assert "smí měnit jen Admin" in text(client.post(f"/members/{boss.id}/invite", follow_redirects=True))
    resp = client.post("/members/invites/send", data={"member_ids": [boss.id]}, follow_redirects=True)
    assert "smí jen Admin" in text(resp)
    assert "smí měnit jen Admin" in text(client.post(f"/members/{boss.id}/cancel-invite", follow_redirects=True))
    assert people.find_person(boss.id, admin.dn).status == "new"
    # Their own card: no edit form either.
    assert "spravuje jen Admin" in text(client.get(f"/members/{chair.id}"))


def test_members_job_adds_missing_chair_and_requests(world, admin):
    unit = world.unit()
    person = world.person(unit, "Stará Skupina")
    people.d.delete(unit.chair_dn, admin.dn)
    people.d.delete(unit.requests_dn, admin.dn)
    people.move_person(person, world.unit(), admin.dn, person.csn)  # no cn=chair: no error
    assert people.repair_members() >= 2
    assert people.d.get(unit.chair_dn, ["cn"], admin.dn) is not None
    assert people.d.get(unit.requests_dn, ["ou"], admin.dn) is not None
