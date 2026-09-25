import time

import responses

from memberbase import people
from tests.conftest import KC, kc_user, login, text, unique

# ── List ─────────────────────────────────────────────────────────────────────


def test_member_list_shows_own_branch_only(client, world):
    home, other = world.unit(), world.unit()
    alice = world.person(home, "Alice Seznamová")
    world.person(home, "Anna Seznamová")
    world.person(other, "Bob Seznamový")
    login(client, alice)
    page = text(client.get("/members/"))
    assert "Anna Seznamová" in page and "Bob Seznamový" not in page
    assert "Hromadn" not in page and 'name="role"' not in page


def test_member_list_search_filters_and_roles(client, world, admin):
    unit = world.unit()
    world.person(unit, "Cyril Hledaný", roles=["medcover:coordinator"])
    world.person(unit, "Cecílie Jiná")
    login(client, admin)
    page = text(client.get(f"/members/?q=Hledan&unit={unit.id}"))
    assert "Cyril Hledaný" in page and "Cecílie Jiná" not in page
    page = text(client.get(f"/members/?unit={unit.id}&role=medcover:coordinator"))
    assert "Cyril Hledaný" in page and "Cecílie Jiná" not in page
    assert "MedCover: Coordinator" in page and 'id="batchToolbar"' in page


def test_archived_view(client, world, admin):
    unit = world.unit()
    world.person(unit, "Dan Archivovaný", status="former")
    login(client, admin)
    assert "Dan Archivovaný" not in text(client.get(f"/members/?unit={unit.id}"))
    page = text(client.get(f"/members/?unit={unit.id}&archived=1"))
    assert "Dan Archivovaný" in page and 'id="batchToolbar"' not in page


def test_search_input_is_escaped(client, admin):
    login(client, admin)
    assert client.get("/members/?q=*)(cn=*").status_code == 200


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_form(client, admin):
    login(client, admin)
    page = text(client.get("/members/new"))
    assert "Externí uživatelé" in page and "checked" in page


def test_create_member_and_invite(client, world, admin, kc):
    unit = world.unit()
    email = unique("nova")
    kc_user(kc, email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, admin)
    resp = client.post(
        "/members/new",
        data={"name": " Nová  Osoba ", "email": email.upper(), "phone": "777 123 456", "unit": unit.id, "invite": "1"},
    )
    person = people.search_people(admin.dn, "Nová Osoba", unit)[0]
    assert resp.headers["Location"] == f"/members/{person.id}"
    assert (person.email, person.phone, person.status, person.kind) == (email, "777123456", "invited", "member")
    group = people.d.get(unit.members_dn, ["member"])
    assert person.dn in group.all("member")
    assert kc.calls[-1].request.body == b'["UPDATE_PASSWORD"]'


def test_create_external_user_without_invite(client, world, admin):
    login(client, admin)
    email = unique("host")
    client.post("/members/new", data={"name": "Externí Host", "email": email, "unit": "external"})
    person = people.search_people(admin.dn, email)[0]
    assert person.kind == "external" and person.unit.is_external
    assert (person.status, person.status_label) == ("new", "Nepozvaný")


def test_create_reports_invalid_input(client, admin):
    login(client, admin)
    page = text(client.post("/members/new", data={"name": "", "email": "x", "phone": "12", "unit": "nope"}))
    for message in ["Vyplňte jméno.", "Zadejte platný e-mail.", "Telefon zadejte", "Vyberte místní skupinu."]:
        assert message in page


def test_create_rejects_duplicate_email(client, world, admin):
    unit = world.unit()
    email = unique("dvojnik")
    world.person(unit, email=email)
    login(client, admin)
    page = text(client.post("/members/new", data={"name": "Dvojník", "email": email, "unit": unit.id}))
    assert "Tento e-mail už používá jiná osoba." in page


def test_create_invite_failure_is_reported(client, world, admin, kc):
    kc.get(f"{KC}/admin/realms/crc/users", json=[])
    login(client, admin)
    email = unique("bezkc")
    resp = client.post(
        "/members/new",
        data={"name": "Bez Keycloaku", "email": email, "unit": world.unit().id, "invite": "1"},
        follow_redirects=True,
    )
    assert "Pozvánku se nepodařilo odeslat: user not found." in text(resp)
    assert people.search_people(admin.dn, email)[0].status == "new"


# ── Detail and edit ──────────────────────────────────────────────────────────


def test_detail_for_admin(client, world, admin):
    unit = world.unit()
    person = world.person(unit, "Eva Detailní", roles=["medcover:viewer"])
    people.save_qualification(None, "Řidič", "", [], False, admin.dn)
    login(client, admin)
    page = text(client.get(f"/members/{person.id}"))
    assert "Eva Detailní" in page and 'value="medcover:viewer" id="rmedcover' in page
    assert "Přesunout do jiné místní skupiny" in page and "Řidič" in page


def test_detail_read_only_for_member(client, world):
    unit = world.unit()
    alice, anna = world.person(unit, "Alice Čtenářka"), world.person(unit, "Anna Čtená")
    login(client, alice)
    page = text(client.get(f"/members/{anna.id}"))
    assert anna.email in page and 'name="name"' not in page and "Účet" not in page


def test_detail_of_invisible_person_is_404(client, world):
    alice = world.person(world.unit())
    bob = world.person(world.unit())
    login(client, alice)
    assert client.get(f"/members/{bob.id}").status_code == 404


def test_edit_person(client, world, admin, sent):
    person = world.person(world.unit(), "Staré Jméno")
    login(client, admin)
    client.post(
        f"/members/{person.id}/edit",
        data={"name": "Jana Nová", "email": person.email, "phone": "", "csn": person.csn},
    )
    after = people.find_person(person.id, admin.dn)
    assert (after.name, after.phone) == ("Jana Nová", "")
    assert sent == []


def test_edit_email_needs_recent_login_and_notifies_old_address(client, world, admin, sent):
    person = world.person(world.unit())
    login(client, admin, auth_time=time.time() - 3600)
    new_email = unique("nova.adresa")
    data = {"name": person.name, "email": new_email, "phone": "", "csn": person.csn}
    resp = client.post(f"/members/{person.id}/edit", data=data)
    assert "reauth=1" in resp.headers["Location"]
    login(client, admin)
    client.post(f"/members/{person.id}/edit", data=data)
    assert people.find_person(person.id, admin.dn).email == new_email
    assert sent == [(person.email, "Změna e-mailu v Evidenci členů")]


def test_edit_validation_stale_and_conflict(client, world, admin):
    unit = world.unit()
    person = world.person(unit)
    other = world.person(unit)
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/edit", data={"name": "", "email": "x"}, follow_redirects=True))
    assert "Vyplňte jméno." in page
    stale = {"name": "Kdokoli", "email": person.email, "phone": "", "csn": "20000101000000.000000Z#000000#000#000000"}
    assert "Záznam mezitím změnil" in text(client.post(f"/members/{person.id}/edit", data=stale, follow_redirects=True))
    taken = {"name": person.name, "email": other.email, "phone": "", "csn": person.csn}
    assert "Tento e-mail už používá" in text(
        client.post(f"/members/{person.id}/edit", data=taken, follow_redirects=True)
    )


# ── Status ───────────────────────────────────────────────────────────────────


def test_status_lifecycle(client, world, admin, kc):
    person = world.person(world.unit(), roles=["medcover:member"])
    kc_user(kc, person.email)
    kc.post(f"{KC}/admin/realms/crc/users/kc-1/logout")
    login(client, admin)

    def act(action):
        current = people.find_person(person.id, admin.dn)
        return client.post(f"/members/{person.id}/status/{action}", data={"csn": current.csn}, follow_redirects=True)

    assert "deaktivována" in text(act("deactivate"))
    assert "Osoba je aktivní." in text(act("activate"))
    assert "archivována" in text(act("archive"))
    after = people.find_person(person.id, admin.dn)
    assert after.status == "former" and people.roles_of(after.dn) == set()
    assert "Tuto změnu stavu nelze provést." in text(act("archive"))
    assert "obnovena jako neaktivní" in text(act("restore"))
    assert len([c for c in kc.calls if c.request.url.endswith("/logout")]) == 2


def test_status_change_reports_keycloak_failure(client, world, admin, kc):
    person = world.person(world.unit())
    kc.get(f"{KC}/admin/realms/crc/users", status=500)
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/status/deactivate", data={"csn": person.csn}, follow_redirects=True))
    assert "přihlašovací služba neodpověděla" in page


def test_status_stale_own_and_unknown(client, world, admin):
    person = world.person(world.unit())
    login(client, admin)
    stale = client.post(f"/members/{person.id}/status/deactivate", data={"csn": "old"}, follow_redirects=True)
    assert "Záznam mezitím změnil" in text(stale)
    own = client.post(f"/members/{admin.id}/status/deactivate", data={"csn": admin.csn}, follow_redirects=True)
    assert "Svůj vlastní stav" in text(own)
    assert client.post(f"/members/{person.id}/status/delete").status_code == 404


# ── Move ─────────────────────────────────────────────────────────────────────


def test_move_between_branches_keeps_identity_roles_and_holdings(client, world, admin):
    old, new = world.unit(), world.unit()
    person = world.person(old, roles=["medcover:member"])
    people.save_qualification(None, "Stěhovací kvalifikace", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == "Stěhovací kvalifikace")
    people.set_holdings(person, {qual.id}, admin.dn)
    login(client, admin)
    client.post(f"/members/{person.id}/move", data={"unit": new.id, "csn": person.csn})
    moved = people.find_person(person.id, admin.dn)
    assert moved.unit_dn == new.dn and moved.entry_uuid == person.entry_uuid
    assert people.roles_of(moved.dn) == {"medcover:member"}
    assert set(people.holdings_of(moved, admin.dn)) == {qual.id}
    assert moved.dn in people.d.get(new.members_dn, ["member"]).all("member")
    assert moved.dn not in people.d.get(old.members_dn, ["member"]).all("member")


def test_move_to_and_from_external(client, world, admin):
    unit = world.unit()
    person = world.person(unit)
    login(client, admin)
    client.post(f"/members/{person.id}/move", data={"unit": "external", "csn": person.csn})
    moved = people.find_person(person.id, admin.dn)
    assert moved.kind == "external" and moved.unit.is_external
    client.post(f"/members/{person.id}/move", data={"unit": unit.id, "csn": moved.csn})
    back = people.find_person(person.id, admin.dn)
    assert back.kind == "member" and back.unit_dn == unit.dn


def test_move_rejects_same_or_unknown_unit_and_stale_form(client, world, admin):
    unit = world.unit()
    person = world.person(unit)
    login(client, admin)
    same = client.post(f"/members/{person.id}/move", data={"unit": unit.id}, follow_redirects=True)
    assert "Vyberte jinou" in text(same)
    stale = client.post(
        f"/members/{person.id}/move", data={"unit": world.unit().id, "csn": "old"}, follow_redirects=True
    )
    assert "Záznam mezitím změnil" in text(stale)
    assert people.find_person(person.id, admin.dn).unit_dn == unit.dn


# ── Roles, qualifications ────────────────────────────────────────────────────


def test_set_roles(client, world, admin):
    person = world.person(world.unit(), roles=["medcover:viewer"])
    login(client, admin)
    client.post(
        f"/members/{person.id}/roles", data={"roles": ["medcover:member", "memberbase:district-coordinator", "x:y"]}
    )
    assert people.roles_of(person.dn) == {"medcover:member", "memberbase:district-coordinator"}


def test_archived_person_gets_no_roles(client, world, admin):
    person = world.person(world.unit(), status="former")
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/roles", data={"roles": ["medcover:member"]}, follow_redirects=True))
    assert "Archivované osobě nelze" in page and people.roles_of(person.dn) == set()


def test_set_qualifications(client, world, admin):
    person = world.person(world.unit())
    people.save_qualification(None, "Kvalifikace A", "", [], False, admin.dn)
    people.save_qualification(None, "Kvalifikace B", "", [], False, admin.dn)
    quals = {q.name: q.id for q in people.list_qualifications(admin.dn)}
    login(client, admin)
    client.post(f"/members/{person.id}/qualifications", data={"quals": [quals["Kvalifikace A"], "nope"]})
    assert set(people.holdings_of(person, admin.dn)) == {quals["Kvalifikace A"]}
    client.post(f"/members/{person.id}/qualifications", data={"quals": [quals["Kvalifikace B"]]})
    assert set(people.holdings_of(person, admin.dn)) == {quals["Kvalifikace B"]}


def test_batch_roles(client, world, admin):
    unit = world.unit()
    a = world.person(unit, roles=["medcover:member"])
    b = world.person(unit)
    gone = world.person(unit, status="former")
    login(client, admin)
    ids = [a.id, b.id, gone.id, "missing"]
    page = text(
        client.post(
            "/members/batch",
            data={"member_ids": ids, "role": "medcover:member", "action": "add"},
            follow_redirects=True,
        )
    )
    assert "u 1 osob, 3 beze změny" in page
    assert people.roles_of(b.dn) == {"medcover:member"} and people.roles_of(gone.dn) == set()
    client.post("/members/batch", data={"member_ids": [a.id, b.id], "role": "medcover:member", "action": "remove"})
    assert people.roles_of(a.dn) == set() == people.roles_of(b.dn)
    bad = client.post(
        "/members/batch", data={"member_ids": [a.id], "role": "x", "action": "add"}, follow_redirects=True
    )
    assert "Vyberte osoby, roli a akci." in text(bad)


def test_batch_move(client, world, admin):
    source, target = world.unit(), world.unit()
    a, b = world.person(source, "Anna Přesunutá"), world.person(source, "Bořek Přesunutý")
    already = world.person(target)
    login(client, admin)
    assert "Přesunout</button>" in text(client.get("/members/"))
    ids = [a.id, b.id, already.id, "missing"]
    page = text(
        client.post("/members/batch-move", data={"member_ids": ids, "target": target.id}, follow_redirects=True)
    )
    assert f"Přesunuto do „{target.name}“: 2 osob, 2 beze změny." in page
    for person in (a, b, already):
        moved = people.find_person(person.id, admin.dn)
        assert moved.unit_dn == target.dn
        assert moved.dn in people.d.get(target.members_dn, ["member"]).all("member")


def test_batch_move_reports_concurrent_change(client, world, admin, monkeypatch):
    person = world.person(world.unit())
    target = world.unit()

    def stale(*args):
        raise people.d.StaleEntry()

    monkeypatch.setattr(people, "move_person", stale)
    login(client, admin)
    page = text(
        client.post("/members/batch-move", data={"member_ids": [person.id], "target": target.id}, follow_redirects=True)
    )
    assert "0 osob, 0 beze změny." in page and "1 osob mezitím změnil někdo jiný" in page


def test_batch_move_needs_people_and_target(client, world, admin):
    person = world.person(world.unit())
    login(client, admin)
    for data in ({"target": world.unit().id}, {"member_ids": [person.id], "target": "nope"}):
        page = text(client.post("/members/batch-move", data=data, follow_redirects=True))
        assert "Vyberte osoby a cílovou místní skupinu." in page


# ── Invitations, MFA, history ────────────────────────────────────────────────


def test_invites_page_resend_and_cancel(client, world, admin, kc):
    person = world.person(world.unit(), "Pozvaná Osoba", status="invited")
    kc_user(kc, person.email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, admin)
    assert "Pozvaná Osoba" in text(client.get("/members/invites"))
    resp = client.post(f"/members/{person.id}/invite", data={"back": "invites"})
    assert resp.headers["Location"] == "/members/invites"
    resp = client.post(f"/members/{person.id}/invite")
    assert resp.headers["Location"] == f"/members/{person.id}"
    client.post(f"/members/{person.id}/cancel-invite")
    assert people.find_person(person.id, admin.dn).status == "former"
    client.post(f"/members/{person.id}/cancel-invite")  # already cancelled: no change
    assert people.find_person(person.id, admin.dn).status == "former"


def test_invite_only_before_first_login(client, world, admin):
    person = world.person(world.unit())
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/invite", follow_redirects=True))
    assert "Pozvánku lze poslat jen osobě, která se ještě nepřihlásila." in page


def test_invite_new_person(client, world, admin, kc):
    person = world.person(world.unit(), "Nová Osoba", status="new")
    kc_user(kc, person.email)
    kc.put(f"{KC}/admin/realms/crc/users/kc-1/execute-actions-email", json={})
    login(client, admin)
    assert "Poslat pozvánku</button>" in text(client.get(f"/members/{person.id}"))
    page = text(client.post(f"/members/{person.id}/invite", follow_redirects=True))
    assert f"Pozvánka odeslána na {person.email}." in page
    assert people.find_person(person.id, admin.dn).status == "invited"


def test_send_invites_to_selected(client, world, admin, kc):
    unit = world.unit()
    new, invited, failing = (world.person(unit, status=s) for s in ("new", "invited", "invited"))
    active = world.person(unit)
    kc_user(kc, new.email, "kc-1")
    kc_user(kc, invited.email, "kc-2")
    kc.get(
        f"{KC}/admin/realms/crc/users",
        json=[],
        match=[responses.matchers.query_param_matcher({"email": failing.email, "exact": "true"})],
    )
    for kc_id in ("kc-1", "kc-2"):
        kc.put(f"{KC}/admin/realms/crc/users/{kc_id}/execute-actions-email", json={})
    login(client, admin)
    page = text(client.get("/members/invites"))
    assert new.name in page and "Nepozvaný" in page and active.email not in page
    ids = [new.id, invited.id, failing.id, active.id, "missing"]
    page = text(client.post("/members/invites/send", data={"member_ids": ids}, follow_redirects=True))
    assert "Odesláno pozvánek: 2." in page
    assert f"Pozvánku se nepodařilo odeslat: {failing.email} (user not found)." in page
    statuses = [people.find_person(p.id, admin.dn).status for p in (new, invited, failing, active)]
    assert statuses == ["invited", "invited", "invited", "active"]


def test_send_invites_with_nothing_selected(client, admin):
    login(client, admin)
    page = text(client.post("/members/invites/send", follow_redirects=True))
    assert "Odesláno pozvánek: 0." in page


def test_mfa_reset(client, world, admin, kc, sent):
    person = world.person(world.unit())
    kc_user(kc, person.email)
    kc.get(
        f"{KC}/admin/realms/crc/users/kc-1/credentials",
        json=[{"id": "c1", "type": "otp"}, {"id": "c2", "type": "password"}, {"id": "c3", "type": "webauthn"}],
    )
    kc.delete(f"{KC}/admin/realms/crc/users/kc-1/credentials/c1")
    kc.delete(f"{KC}/admin/realms/crc/users/kc-1/credentials/c3")
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/mfa-reset", follow_redirects=True))
    assert "odebráno 2 ověřovacích prostředků" in page
    assert sent == [(person.email, "Reset dvoufázového ověření")]


def test_mfa_reset_failure(client, world, admin, kc, sent):
    person = world.person(world.unit())
    kc.get(f"{KC}/admin/realms/crc/users", body=responses.ConnectionError("down"))
    login(client, admin)
    page = text(client.post(f"/members/{person.id}/mfa-reset", follow_redirects=True))
    assert "Dvoufázové ověření se nepodařilo resetovat" in page and sent == []


def test_login_as_button_shown_only_in_debug(client, app, world, admin):
    person = world.person(world.unit())
    login(client, admin)
    assert "Přihlásit se jako tento uživatel" not in text(client.get(f"/members/{person.id}"))
    app.config["DEBUG"] = True
    assert "Přihlásit se jako tento uživatel" in text(client.get(f"/members/{person.id}"))


def test_login_as_requires_debug(client, app, world, admin):
    person = world.person(world.unit())
    login(client, admin)
    assert client.post(f"/members/{person.id}/login-as").status_code == 404


def test_login_as_requires_admin(client, app, world):
    app.config["DEBUG"] = True
    person = world.person(world.unit())
    login(client, world.person(world.unit()))
    assert client.post(f"/members/{person.id}/login-as").status_code == 404


def test_login_as_rejects_self_and_inactive_person(client, app, world, admin):
    app.config["DEBUG"] = True
    invited = world.person(world.unit(), status="invited")
    login(client, admin)
    assert client.post(f"/members/{admin.id}/login-as").status_code == 404
    assert client.post(f"/members/{invited.id}/login-as").status_code == 404


def test_login_as_switches_session(client, app, world, admin):
    app.config["DEBUG"] = True
    person = world.person(world.unit())
    login(client, admin)
    resp = client.post(f"/members/{person.id}/login-as", follow_redirects=True)
    assert person.name in text(resp)
    with client.session_transaction() as sess:
        assert sess["member_id"] == person.id
        assert sess["id_token"] == ""


def test_login_as_requires_recent_login(client, app, world, admin):
    app.config["DEBUG"] = True
    person = world.person(world.unit())
    login(client, admin, auth_time=time.time() - 3600)
    resp = client.post(f"/members/{person.id}/login-as")
    assert resp.headers["Location"].startswith("/login?next=") and "reauth=1" in resp.headers["Location"]


def test_person_history(client, world, admin):
    person = world.person(world.unit(), "Historie Osoby", roles=["medcover:member"])
    people.update_person(person, {"phone": "600600600"}, admin.dn, person.csn)
    login(client, admin)
    page = text(client.get(f"/members/{person.id}/history"))
    assert "Historie změn: Historie Osoby" in page
    assert "Telefon nastaveno: 600600600" in page
    assert "Telefon předtím: 123456789" in page
    assert "Člen přidáno: Historie Osoby" in page
