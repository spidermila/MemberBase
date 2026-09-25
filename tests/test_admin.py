import uuid

import pytest
import responses

from memberbase import history, people
from tests.conftest import login

KC = "http://kc.internal"


def text(resp) -> str:
    return resp.get_data(as_text=True)


# ── Místní skupiny ───────────────────────────────────────────────────────────


def test_create_and_rename_unit(client, admin):
    name = f"Místní skupina Třinec {uuid.uuid4().hex[:4]}"
    login(client, admin)
    client.post("/units", data={"name": name})
    unit = next(u for u in people.list_units(admin.dn) if u.name == name)
    assert unit.slug.startswith("mistni-skupina-trinec-")
    groups = {e.first("cn") for e in people.d.search(unit.dn, "(objectClass=crcGroup)", ["cn"], as_dn=admin.dn)}
    assert groups == {"members", "chair", "readers-basic", "readers-contact", "readers-extended"}
    assert people.d.get(unit.requests_dn, ["ou"], as_dn=admin.dn) is not None
    client.post(f"/units/{unit.id}", data={"name": "Přejmenovaná"})
    assert people.get_unit(unit.id, admin.dn).name == "Přejmenovaná"
    assert "Přejmenovaná" in text(client.get("/units"))


def test_same_name_gets_unique_slug(admin):
    name = f"Stejná {uuid.uuid4().hex[:4]}"
    first, second = people.create_unit(name, admin.dn), people.create_unit(name, admin.dn)
    assert second.slug == f"{first.slug}-2"
    assert people.slugify("!!!") == "skupina"


def test_unit_validation(client, world, admin):
    login(client, admin)
    assert "Vyplňte jméno." in text(client.post("/units", data={"name": " "}, follow_redirects=True))
    unit = world.unit()
    assert "Vyplňte jméno." in text(client.post(f"/units/{unit.id}", data={"name": ""}, follow_redirects=True))
    assert client.post("/units/external", data={"name": "x"}).status_code == 404
    assert client.post("/units/missing", data={"name": "x"}).status_code == 404


# ── Kvalifikace ──────────────────────────────────────────────────────────────


def qual_named(admin, name):
    return next(q for q in people.list_qualifications(admin.dn) if q.name == name)


def test_qualification_crud(client, world, admin):
    parent_name, child_name = f"Záchranář {uuid.uuid4().hex[:4]}", f"Zdravotník {uuid.uuid4().hex[:4]}"
    login(client, admin)
    assert "Nová kvalifikace" in text(client.get("/qualifications/new"))
    client.post("/qualifications/new", data={"name": parent_name, "can_be_rp": "1", "description": "Popis"})
    parent = qual_named(admin, parent_name)
    assert parent.can_be_rp and parent.description == "Popis"
    client.post("/qualifications/new", data={"name": child_name, "parents": [parent.id, "nope"]})
    child = qual_named(admin, child_name)
    assert child.parents == [parent.id] and not child.can_be_rp

    holder = world.person(world.unit(), "Držitel Kvalifikace")
    people.set_holdings(holder, {child.id}, admin.dn)
    page = text(client.get("/qualifications"))
    assert parent_name in page and child_name in page
    assert "Držitel Kvalifikace" in text(client.get(f"/qualifications/{child.id}"))

    renamed = child_name + " II"
    client.post(f"/qualifications/{child.id}", data={"name": renamed})
    assert qual_named(admin, renamed).parents == []

    held = text(client.post(f"/qualifications/{child.id}/delete", follow_redirects=True))
    assert "nelze ji smazat" in held
    people.save_qualification(qual_named(admin, renamed), renamed, "", [parent.id], False, admin.dn)
    referenced = text(client.post(f"/qualifications/{parent.id}/delete", follow_redirects=True))
    assert "nelze ji smazat" in referenced

    people.set_holdings(holder, set(), admin.dn)
    for qual_id in (child.id, parent.id):
        assert "je smazána" in text(client.post(f"/qualifications/{qual_id}/delete", follow_redirects=True))
    assert client.get(f"/qualifications/{parent.id}").status_code == 404


def test_qualification_validation_and_stale(client, admin, monkeypatch):
    name = f"Kvalifikace {uuid.uuid4().hex[:4]}"
    login(client, admin)
    assert "Vyplňte jméno." in text(client.post("/qualifications/new", data={"name": ""}))
    people.save_qualification(None, name, "", [], False, admin.dn)
    qual = qual_named(admin, name)
    stale = people.Qualification(**{**qual.__dict__, "csn": "old"})
    monkeypatch.setattr("memberbase.views.admin._qual_or_404", lambda qual_id: stale)
    assert "mezitím změnil" in text(client.post(f"/qualifications/{qual.id}", data={"name": "Jiný"}))


# ── Sdílení údajů ────────────────────────────────────────────────────────────


def test_grant_lifecycle(client, world, admin):
    home, other = world.unit(), world.unit()
    alice = world.person(home, "Alice Příjemkyně")
    login(client, admin)
    page = text(client.get("/grants"))
    assert "Zatím žádná sdílení" in page or "Nové sdílení" in page
    client.post(
        "/grants",
        data={
            "grantee_kind": "person",
            "grantee_person": alice.id,
            "target": other.id,
            "level": "contact",
            "expires": "2099-12-31",
            "description": "Cvičení",
        },
    )
    client.post("/grants", data={"grantee_kind": "unit", "grantee_unit": home.id, "target": other.id, "level": "basic"})
    page = text(client.get("/grants"))
    assert "Alice Příjemkyně" in page and "31. 12. 2099" in page and "trvale" in page and "Cvičení" in page
    grants = people.list_grants(admin.dn, f"(crcGrantee={alice.id})")
    client.post(f"/grants/{grants[0].id}/revoke")
    assert people.list_grants(admin.dn, f"(crcGrantee={alice.id})") == []
    assert client.post("/grants/missing/revoke").status_code == 404


def test_grant_validation(client, world, admin):
    unit = world.unit()
    login(client, admin)

    def post(**data):
        return text(client.post("/grants", data=data, follow_redirects=True))

    assert "Vyplňte komu" in post(grantee_kind="person", target=unit.id, level="contact")
    assert "Vyplňte komu" in post(grantee_kind="person", grantee_person="x", target="nope", level="contact")
    assert "Vyplňte komu" in post(grantee_kind="person", grantee_person="x", target=unit.id, level="all")
    assert "vidí své členy už sama" in post(grantee_kind="unit", grantee_unit=unit.id, target=unit.id, level="basic")
    assert "Zadejte platné datum." in post(
        grantee_kind="person", grantee_person="x", target=unit.id, level="basic", expires="zítra"
    )
    assert "Příjemce nebyl nalezen." in post(
        grantee_kind="person", grantee_person="missing", target=unit.id, level="basic"
    )
    assert "Příjemce nebyl nalezen." in post(
        grantee_kind="unit", grantee_unit="external", target=unit.id, level="basic"
    )


def test_revoke_keeps_membership_while_another_grant_needs_it(world, admin):
    home, other = world.unit(), world.unit()
    alice, bob = world.person(home), world.person(other)
    for description in ("první", "druhé"):
        people.create_grant(alice.id, other, "contact", None, description, admin.id, admin.dn)
    first, second = people.list_grants(admin.dn, f"(crcGrantee={alice.id})")
    people.revoke_grant(first, admin.dn)
    assert people.find_person(bob.id, alice.dn) is not None
    people.revoke_grant(second, admin.dn)
    assert people.find_person(bob.id, alice.dn) is None


def test_revoke_grant_of_archived_person_still_deletes_record(world, admin):
    alice, other = world.person(world.unit()), world.unit()
    people.create_grant(alice.id, other, "basic", None, "", admin.id, admin.dn)
    [grant] = people.list_grants(admin.dn, f"(crcGrantee={alice.id})")
    grant.grantee = "gone"  # grantee no longer resolvable
    people.revoke_grant(grant, admin.dn)
    assert people.list_grants(admin.dn, f"(crcGrantee={alice.id})") == []


# ── Jobs ─────────────────────────────────────────────────────────────────────


def test_repair_members(world, admin):
    unit = world.unit()
    person = world.person(unit)
    people.d.modify(unit.members_dn, {"member": []}, admin.dn)
    assert people.repair_members() >= 1
    assert person.dn in people.d.get(unit.members_dn, ["member"]).all("member")
    assert people.repair_members() == 0


def test_cli_commands(app, world, admin):
    runner = app.test_cli_runner()
    assert "expired grants:" in runner.invoke(args=["expire-grants"]).output
    assert "entries repaired:" in runner.invoke(args=["repair-members"]).output


# ── History and permissions ──────────────────────────────────────────────────


def test_global_history(client, world, admin):
    unit = world.unit()
    person = world.person(unit, "Historická Osoba")
    people.save_qualification(None, f"Hist {uuid.uuid4().hex[:4]}", "", [], True, admin.dn)
    people.create_grant(person.id, world.unit(), "basic", None, "", admin.id, admin.dn)
    people.move_person(person, world.unit(), admin.dn, person.csn)
    login(client, admin)
    page = text(client.get("/history"))
    for fragment in [
        "Vytvoření",
        "Přesun",
        "Historická Osoba",
        "Může být zodpovědná osoba: ano",
        "sdílení údajů",
        "Jméno – ",
        "všichni z",
    ]:
        assert fragment in page, fragment


def test_history_labels(app, admin):
    labels = history.Labels(admin.dn)
    base = people.d.base_dn()
    assert (
        labels.dn(f"crcHoldingId=x,uid=nobody,ou=nowhere,ou=units,{base}") == "kvalifikace – "
        f"uid=nobody,ou=nowhere,ou=units,{base}"
    )
    assert labels.dn("crcQualificationId=unknown,ou=x") == "kvalifikace"
    assert labels.dn("gidNumber=1+uidNumber=1,cn=peercred,cn=external,cn=auth") == "Správa serveru"
    assert labels.dn(f"cn=keycloak,ou=services,{base}") == "Přihlašování (Keycloak)"
    assert labels.value("userPassword", "{ARGON2}x") == "••••"
    assert labels.value("crcMemberStatus", "former") == "Archivovaný"
    assert labels.value("crcCanBeRp", "FALSE") == "ne"
    assert labels.value("crcGrantee", "who") == "who"
    assert history._parse_mod("mail:") == ("mail", "", "")


@responses.activate
def test_permissions_page(client, admin, app):
    login(client, admin)
    page = text(client.get("/permissions"))
    assert "OS koordinátor" in page and "MedCover zatím mapování rolí nevystavuje." in page

    app.config["MEDCOVER_ROLES_URL"] = "http://medcover.test/api/roles"
    responses.post(f"{KC}/realms/crc/protocol/openid-connect/token", json={"access_token": "svc"})
    responses.get("http://medcover.test/api/roles", json={"roles": [{"name": "Viewer", "permissions": ["event.view"]}]})
    page = text(client.get("/permissions"))
    assert "Viewer" in page and "event.view" in page

    responses.get("http://medcover.test/api/roles", status=503)
    responses.replace(responses.GET, "http://medcover.test/api/roles", status=503)
    assert "MedCover teď neodpovídá" in text(client.get("/permissions"))


@pytest.mark.parametrize("url", ["/units", "/qualifications", "/grants", "/history", "/permissions"])
def test_district_coordinator_cannot_manage(client, world, url):
    login(client, world.person(world.unit(), roles=["memberbase:district-coordinator"]))
    assert client.get(url).status_code == 403
