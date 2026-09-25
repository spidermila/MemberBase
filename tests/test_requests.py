"""Žádosti: moves between Místní skupiny and access to named people."""

from datetime import UTC, datetime, timedelta

import pytest

from memberbase import approvals, people
from memberbase.directory import Denied
from tests.conftest import login, text
from tests.test_access_rules import level

TOMORROW = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture
def units(world):
    return world.unit(), world.unit()


@pytest.fixture
def cast(world, units):
    """src and dst units with a Chair each, a co-Chair in src, and Jan in src."""
    src, dst = units
    return {
        "src_chair": world.chair(src, "Sára Zdrojová"),
        "co_chair": world.chair(src, "Cyril Spolupředseda"),
        "dst_chair": world.chair(dst, "Dana Cílová"),
        "jan": world.person(src, "Jan Stěhovavý"),
    }


def only(me_person) -> approvals.Request:
    reqs = approvals.list_requests(me_person.dn)
    assert len(reqs) == 1
    return reqs[0]


# ── Moves ────────────────────────────────────────────────────────────────────


def file_move(client, cast, units, note="Stěhuje se"):
    login(client, cast["src_chair"])
    return client.post(f"/requests/move/{cast['jan'].id}", data={"unit": units[1].id, "note": note})


def test_chair_sees_move_request_form_only_for_own_people(client, world, cast, units):
    login(client, cast["src_chair"])
    assert "Požádat o přesun" in text(client.get(f"/members/{cast['jan'].id}"))
    login(client, cast["jan"])
    assert "Požádat o přesun" not in text(client.get(f"/members/{cast['jan'].id}"))
    login(client, cast["dst_chair"])
    assert client.post(f"/requests/move/{cast['jan'].id}", data={"unit": units[0].id}).status_code == 403


def test_move_request_is_filed_and_announced(client, cast, units, sent):
    resp = file_move(client, cast, units)
    assert resp.headers["Location"] == f"/members/{cast['jan'].id}"
    req = only(cast["src_chair"])
    assert (req.type, req.status, req.unit.id, req.move_subject_name) == (
        "move",
        "pending",
        units[1].id,
        "Jan Stěhovavý",
    )
    assert req.move_from.id == units[0].id and req.note == "Stěhuje se"
    assert sent == [(cast["dst_chair"].email, "Nová žádost: Přesun do místní skupiny")]
    # Readers: requester, destination Chair, the other source Chair; nobody else.
    for reader in ("dst_chair", "co_chair"):
        assert len(approvals.list_requests(cast[reader].dn)) == 1
    assert approvals.list_requests(cast["jan"].dn) == []
    page = text(client.get("/requests/"))
    assert "Vaše žádosti" in page and "Jan Stěhovavý" in page


def test_move_request_needs_another_unit(client, cast, units):
    login(client, cast["src_chair"])
    resp = client.post(f"/requests/move/{cast['jan'].id}", data={"unit": units[0].id}, follow_redirects=True)
    assert "Vyberte jinou místní skupinu." in text(resp)
    assert approvals.list_requests(cast["src_chair"].dn) == []


def test_destination_chair_approves_and_person_moves(client, cast, units, sent):
    file_move(client, cast, units)
    login(client, cast["dst_chair"])
    page = text(client.get("/requests/"))
    assert "Čekají na vaše rozhodnutí" in page and "Jan Stěhovavý" in page
    req = only(cast["dst_chair"])
    assert "Schválit" in text(client.get(f"/requests/{req.id}"))
    sent.clear()
    resp = client.post(f"/requests/{req.id}/decide", data={"action": "approve", "csn": req.csn}, follow_redirects=True)
    assert "Žádost je vyřízena." in text(resp)
    moved = people.find_person(cast["jan"].id, None)
    assert moved.unit_dn.lower() == units[1].dn.lower()
    assert only(cast["src_chair"]).status == "done"
    assert sorted(to for to, _ in sent) == sorted([cast["src_chair"].email, cast["co_chair"].email])
    # Decided: no second decision.
    resp = client.post(f"/requests/{req.id}/decide", data={"action": "reject", "csn": req.csn}, follow_redirects=True)
    assert "O žádosti už je rozhodnuto." in text(resp)


def test_rejected_move_changes_nothing(client, cast, units):
    file_move(client, cast, units)
    login(client, cast["dst_chair"])
    req = only(cast["dst_chair"])
    resp = client.post(f"/requests/{req.id}/decide", data={"action": "reject", "csn": req.csn}, follow_redirects=True)
    assert "Žádost je zamítnuta." in text(resp)
    assert people.find_person(cast["jan"].id, None).unit_dn.lower() == units[0].dn.lower()
    assert only(cast["src_chair"]).status == "rejected"


def test_stale_decision_is_refused(client, cast, units):
    file_move(client, cast, units)
    login(client, cast["dst_chair"])
    req = only(cast["dst_chair"])
    resp = client.post(f"/requests/{req.id}/decide", data={"action": "approve", "csn": "stale"}, follow_redirects=True)
    assert "Žádost mezitím změnil někdo jiný" in text(resp)
    assert only(cast["dst_chair"]).pending


def test_only_deciders_decide(client, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    for who in ("src_chair", "co_chair"):
        login(client, cast[who])
        assert "Schválit" not in text(client.get(f"/requests/{req.id}"))
        assert client.post(f"/requests/{req.id}/decide", data={"action": "approve"}).status_code == 403
    login(client, cast["jan"])
    assert client.get(f"/requests/{req.id}").status_code == 404


def approve_as(client, who, req_id, **extra):
    login(client, who)
    req = approvals.get_request(req_id, who.dn)
    data = {"action": "approve", "csn": req.csn} | extra
    return text(client.post(f"/requests/{req_id}/decide", data=data, follow_redirects=True))


def test_move_fails_if_person_moved_meanwhile(client, world, admin, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    people.move_person(cast["jan"], world.unit(), admin.dn, cast["jan"].csn)
    page = approve_as(client, cast["dst_chair"], req.id)
    assert "nepodařilo se ji provést: Osoba mezitím změnila místní skupinu" in page
    assert only(cast["src_chair"]).status == "failed"


def test_move_fails_if_requester_no_longer_chairs(client, admin, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    people.set_chair(cast["src_chair"], False, admin.dn)
    assert "Žadatel není předsedou" in approve_as(client, cast["dst_chair"], req.id)


def test_privileged_person_moves_only_with_an_admin(client, world, admin, cast, units):
    boss = world.person(units[0], "Olga Oprávněná", roles=["medcover:admin"])
    login(client, cast["src_chair"])
    page = text(client.get(f"/members/{boss.id}"))
    assert "spravuje jen Admin" in page and "Požádat o přesun" not in page and 'name="csn"' not in page
    resp = client.post(f"/requests/move/{boss.id}", data={"unit": units[1].id}, follow_redirects=True)
    assert "přesouvá jen Admin" in text(resp)
    # Filed anyway (e.g. straight to the directory): carrying it out checks again.
    for _ in range(2):
        approvals.file_move(boss, units[1], "", cast["src_chair"], cast["src_chair"].dn)
    first, second = approvals.list_requests(cast["dst_chair"].dn)
    assert "smí přesunout jen Admin" in approve_as(client, cast["dst_chair"], first.id)
    assert "Žádost je vyřízena." in approve_as(client, admin, second.id)
    assert people.find_person(boss.id, None).unit_dn.lower() == units[1].dn.lower()


def test_directory_refusal_marks_request_failed(client, cast, units, monkeypatch):
    file_move(client, cast, units)
    req = only(cast["src_chair"])

    def refuse(*_a, **_k):
        raise Denied()

    monkeypatch.setattr(people, "move_person", refuse)
    assert "Adresář změnu odmítl." in approve_as(client, cast["dst_chair"], req.id)


def test_unit_without_chair_announces_to_admins(client, world, admin, cast, units, sent):
    lonely = world.unit()
    login(client, cast["src_chair"])
    client.post(f"/requests/move/{cast['jan'].id}", data={"unit": lonely.id})
    assert (admin.email, "Nová žádost: Přesun do místní skupiny") in sent


def test_former_person_cannot_be_requested(client, world, cast, units):
    gone = world.person(units[0], "Bývalý Člen", status="former")
    login(client, cast["src_chair"])
    assert client.post(f"/requests/move/{gone.id}", data={"unit": units[1].id}).status_code == 403


# ── Access ───────────────────────────────────────────────────────────────────


@pytest.fixture
def helper(world):
    """A Chair of a third unit asking for access."""
    return world.chair(world.unit(), "Hana Humanitární")


def test_access_form_needs_permission(client, cast):
    login(client, cast["jan"])
    assert client.get("/requests/access").status_code == 403


def test_access_form_validation(client, helper, units):
    login(client, helper)
    page = text(client.get("/requests/access"))
    assert units[0].name in page and page.count('name="name"') == 5
    cases = [
        ({"name": ["Jan"], "unit": [""], "level": "contact", "note": "x"}, "U jména „Jan“ vyberte místní skupinu."),
        ({"name": ["x" * 121], "unit": [units[0].id], "level": "contact", "note": "x"}, "nejvýše 120 znaků"),
        ({"name": [""], "unit": [""], "level": "contact", "note": "x"}, "Napište aspoň jedno jméno."),
        ({"name": ["Jan"], "unit": [units[0].id], "level": "all", "note": "x"}, "Vyberte rozsah údajů."),
        ({"name": ["Jan"], "unit": [units[0].id], "level": "basic", "note": " "}, "Napište, k čemu"),
        (
            {"name": ["Jan"], "unit": [units[0].id], "level": "basic", "note": "x", "expires": "2020-01-01"},
            "v budoucnu",
        ),
        ({"name": ["Jan"], "unit": [units[0].id], "level": "basic", "note": "x", "expires": "zítra"}, "platné datum"),
    ]
    for data, message in cases:
        assert message in text(client.post("/requests/access", data=data)), message
    assert approvals.list_requests(helper.dn) == []


def test_access_request_splits_per_unit(client, helper, cast, units, sent):
    login(client, helper)
    data = {
        "name": ["Jan Stehovavy", "jan stehovavy", "Neznámý Člověk", "Dana Cílová", ""],
        "unit": [units[0].id, units[0].id, units[0].id, units[1].id, ""],
        "level": "contact",
        "expires": TOMORROW,
        "note": "Sbírka",
    }
    resp = client.post("/requests/access", data=data)
    assert resp.headers["Location"] == "/requests/"
    reqs = {r.unit.id: r for r in approvals.list_requests(helper.dn)}
    assert reqs[units[0].id].access_names == ["Jan Stehovavy", "Neznámý Člověk"]
    assert reqs[units[1].id].access_names == ["Dana Cílová"]
    assert reqs[units[0].id].expires_at is not None
    assert {to for to, _ in sent} == {cast["src_chair"].email, cast["co_chair"].email, cast["dst_chair"].email}


def file_access(client, helper, unit, names, **extra):
    login(client, helper)
    data = {"name": names, "unit": [unit.id] * len(names), "level": "contact", "note": "Sbírka"} | extra
    client.post("/requests/access", data=data)
    return next(r for r in approvals.list_requests(helper.dn) if r.pending)


def test_chair_matches_names_and_grants_only_chosen_people(client, world, helper, cast, units, sent):
    jana = world.person(units[0], "Jana Nováková")
    world.person(units[0], "Jana Nováková")  # a namesake: no automatic match
    req = file_access(client, helper, units[0], ["jan stehovavy", "Jana Nováková", "Pepa Neznámý"])
    login(client, cast["src_chair"])
    page = text(client.get(f"/requests/{req.id}"))
    jan = cast["jan"]
    assert f'<option value="{jan.id}" selected>' in page  # accent-insensitive exact match
    assert page.count("přesně nenašli") == 2 and "Podobná jména" in page
    # Nothing chosen: approving is refused.
    resp = client.post(f"/requests/{req.id}/decide", data={"action": "approve", "csn": req.csn}, follow_redirects=True)
    assert "Nevybrali jste nikoho" in text(resp)
    sent.clear()
    data = {"action": "approve", "csn": req.csn, "subject_0": jan.id, "subject_1": jana.id, "subject_2": ""}
    resp = client.post(f"/requests/{req.id}/decide", data=data, follow_redirects=True)
    assert "Žádost je vyřízena." in text(resp)
    assert sent == [(helper.email, "Žádost: Vyřízena")]
    assert level(jan, helper.dn) == "contact" and level(jana, helper.dn) == "contact"
    assert level(cast["co_chair"], helper.dn) == "none"
    login(client, helper)
    page = text(client.get(f"/requests/{req.id}"))
    assert "Zpřístupněno:" in page and "Jan Stěhovavý" in page


def test_access_grant_expires(client, world, admin, helper, cast, units):
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"], expires=TOMORROW)
    data = {"action": "approve", "csn": req.csn, "subject_0": cast["jan"].id}
    login(client, cast["src_chair"])
    client.post(f"/requests/{req.id}/decide", data=data)
    grant = people.list_grants(admin.dn, f"(crcGrantee={helper.id})")[0]
    assert grant.expires_at == req.expires_at
    login(client, admin)
    assert f"<td>Jan Stěhovavý ({units[0].name})</td>" in text(client.get("/grants"))


def test_access_fails_for_inactive_requester_or_past_expiry(client, world, admin, helper, cast, units):
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"])
    people.set_status(helper, "inactive", admin.dn)
    login(client, cast["src_chair"])
    data = {"action": "approve", "csn": req.csn, "subject_0": cast["jan"].id}
    assert "Žadatel už není aktivní." in text(
        client.post(f"/requests/{req.id}/decide", data=data, follow_redirects=True)
    )
    people.set_status(helper, "active", admin.dn)
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"], expires=TOMORROW)
    people.d.modify(req.dn, {"crcExpiresAt": ["20200101000000Z"]}, admin.dn)
    page = approve_as(client, cast["src_chair"], req.id, subject_0=cast["jan"].id)
    assert "Požadovaná platnost už uplynula." in page


def test_requests_page_sections(client, admin, helper, cast, units):
    file_access(client, helper, units[0], ["Jan Stěhovavý"])
    login(client, admin)
    page = text(client.get("/requests/"))
    assert "Čekají na vaše rozhodnutí" in page and "Jan Stěhovavý" in page
    login(client, cast["co_chair"])
    assert "Nic nečeká." not in text(client.get("/requests/"))


def test_history_labels_requests_and_person_grants(client, admin, helper, cast, units):
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"])
    login(client, cast["src_chair"])
    client.post(f"/requests/{req.id}/decide", data={"action": "approve", "csn": req.csn, "subject_0": cast["jan"].id})
    login(client, admin)
    page = text(client.get("/history"))
    assert "žádost" in page and "Stav žádosti přidáno: Vyřízena" in page
    assert "Jméno a kontakt – Jan Stěhovavý" in page


def test_second_pending_move_request_is_refused(client, cast, units):
    file_move(client, cast, units)
    resp = client.post(f"/requests/move/{cast['jan'].id}", data={"unit": units[1].id}, follow_redirects=True)
    assert "už čeká na rozhodnutí" in text(resp)
    assert len(approvals.list_requests(cast["src_chair"].dn)) == 1


def test_move_filed_by_non_chair_fails_even_if_admin_approves(client, admin, cast, units):
    """Filed straight to the directory by a plain member: the requester is who
    filed it, and they must chair the Místní skupina the person leaves."""
    member = cast["jan"]
    other = cast["co_chair"]
    req_id = approvals._file(
        units[1],
        member,
        {
            "objectClass": ["crcRequest", "crcMoveRequest"],
            "crcRequestType": ["move"],
            "crcMoveSubject": [other.id],
            "crcMoveSubjectName": [other.name],
            "crcMoveFromUnit": [units[0].id],
        },
        member.dn,
    )
    assert approvals.get_request(req_id, admin.dn).requested_by_name == member.name
    assert "Žadatel není předsedou" in approve_as(client, admin, req_id)


def test_admin_cannot_approve_own_access_request(client, admin, cast, units):
    req = file_access(client, admin, units[0], ["Jan Stěhovavý"])
    assert "nemůže rozhodnout sám žadatel" in approve_as(client, admin, req.id, subject_0=cast["jan"].id)


def test_decision_needs_a_known_action(client, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    login(client, cast["dst_chair"])
    assert client.post(f"/requests/{req.id}/decide", data={"action": "maybe", "csn": req.csn}).status_code == 400


def test_inactive_chair_means_admins_are_told(client, admin, cast, units, sent):
    people.set_status(cast["dst_chair"], "inactive", admin.dn)
    file_move(client, cast, units)
    assert (admin.email, "Nová žádost: Přesun do místní skupiny") in sent
    assert cast["dst_chair"].email not in {to for to, _ in sent}


def test_move_fails_if_name_does_not_match(client, admin, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    people.update_person(cast["jan"], {"name": "Jan Jiný"}, admin.dn, cast["jan"].csn)
    assert "Jméno osoby se od podání žádosti změnilo" in approve_as(client, cast["dst_chair"], req.id)


def test_move_fails_if_requester_inactive(client, admin, cast, units):
    file_move(client, cast, units)
    req = only(cast["src_chair"])
    people.set_status(cast["src_chair"], "inactive", admin.dn)
    assert "Žadatel už není aktivní." in approve_as(client, cast["dst_chair"], req.id)


def test_access_fails_if_requester_no_longer_chairs_or_level_is_forged(client, admin, helper, cast, units):
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"])
    people.d.modify(req.dn, {"crcAccessLevel": ["everything"]}, admin.dn)
    page = approve_as(client, cast["src_chair"], req.id, subject_0=cast["jan"].id)
    assert "neplatný rozsah" in page
    req = file_access(client, helper, units[0], ["Jan Stěhovavý"])
    people.set_chair(helper, False, admin.dn)
    page = approve_as(client, cast["src_chair"], req.id, subject_0=cast["jan"].id)
    assert "ani Admin" in page


def test_too_many_names_are_refused(client, helper, units):
    login(client, helper)
    data = {"name": ["Jan"] * 51, "unit": [units[0].id] * 51, "level": "basic", "note": "x"}
    assert "nejvýše o 50 jmen" in text(client.post("/requests/access", data=data))
