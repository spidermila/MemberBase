"""Directory access rules: who may read or change what. Every rule is checked
for both the allowed and the denied case, against the real OpenLDAP image."""

import os
import time
from datetime import UTC, datetime, timedelta

import ldap
import pytest

from memberbase import approvals
from memberbase import directory as d
from memberbase import people

BASIC = ["cn", "crcMemberId", "crcMemberStatus"]
CONTACT = ["mail", "telephoneNumber"]


def visible(target: people.Person, as_dn: str | None) -> dict[str, list[str]]:
    entry = d.get(target.dn, BASIC + CONTACT, as_dn=as_dn)
    return {} if entry is None else {k: v for k, v in entry.attrs.items() if k != "entryCSN"}


def level(target: people.Person, as_dn: str | None) -> str:
    """none | basic | contact, as the reader sees the target."""
    seen = visible(target, as_dn)
    if "mail" in seen and "telephoneNumber" in seen:
        return "contact"
    if "cn" in seen:
        return "basic"
    assert not seen
    return "none"


def service_conn(name: str, password: str) -> ldap.ldapobject.LDAPObject:
    conn = d.connect()
    conn.simple_bind_s(f"cn={name},ou=services,{d.base_dn()}", password)
    return conn


@pytest.fixture
def setup(world):
    home, other = world.unit(), world.unit()
    return {
        "home": home,
        "other": other,
        "alice": world.person(home, "Alice Domácí"),
        "anna": world.person(home, "Anna Domácí"),
        "bob": world.person(other, "Bob Cizí"),
        "ext": world.person(world.external(), "Eva Externí"),
    }


def test_own_branch_default_level_is_contact(setup):
    assert level(setup["anna"], setup["alice"].dn) == "contact"


def test_other_branch_and_external_hidden_by_default(setup):
    alice = setup["alice"].dn
    assert level(setup["bob"], alice) == "none"
    assert level(setup["ext"], alice) == "none"
    assert level(setup["alice"], setup["bob"].dn) == "none"


def test_external_user_sees_only_self(setup):
    ext = setup["ext"].dn
    assert level(setup["ext"], ext) == "contact"
    assert level(setup["alice"], ext) == "none"


@pytest.mark.parametrize("grant_level, expected", [("basic", "basic"), ("contact", "contact"), ("extended", "contact")])
def test_personal_grant_unlocks_level(setup, admin, grant_level, expected):
    alice = setup["alice"]
    people.create_grant(alice.id, setup["other"], grant_level, None, "", admin.id, admin.dn)
    assert level(setup["bob"], alice.dn) == expected
    assert level(setup["bob"], setup["anna"].dn) == "none"


def test_branch_grant_covers_all_members_of_grantee_branch(setup, admin):
    people.create_grant(setup["home"].id, setup["other"], "contact", None, "", admin.id, admin.dn)
    assert level(setup["bob"], setup["alice"].dn) == "contact"
    assert level(setup["bob"], setup["anna"].dn) == "contact"
    # One direction only.
    assert level(setup["alice"], setup["bob"].dn) == "none"
    # External users are not members of the branch.
    assert level(setup["bob"], setup["ext"].dn) == "none"


def test_grant_to_external_users(setup, admin):
    people.create_grant(setup["alice"].id, setup["ext"].unit, "basic", None, "", admin.id, admin.dn)
    assert level(setup["ext"], setup["alice"].dn) == "basic"


def test_revoked_grant_no_longer_applies(setup, admin):
    people.create_grant(setup["alice"].id, setup["other"], "contact", None, "", admin.id, admin.dn)
    grant = people.list_grants(admin.dn, f"(crcGrantee={setup['alice'].id})")[0]
    people.revoke_grant(grant, admin.dn)
    assert level(setup["bob"], setup["alice"].dn) == "none"


def test_inactive_person_sees_nothing(setup, admin):
    anna = setup["anna"]
    people.set_status(anna, "inactive", admin.dn)
    assert level(setup["alice"], anna.dn) == "none"
    assert level(anna, anna.dn) == "none"


def test_non_active_statuses_block_reads(setup, admin):
    alice = setup["alice"]
    for status in ["new", "invited", "inactive", "former"]:
        people.set_status(alice, status, admin.dn)
        assert level(setup["anna"], alice.dn) == "none", status
    people.set_status(alice, "active", admin.dn)
    assert level(setup["anna"], alice.dn) == "contact"


def test_admin_and_district_coordinator_see_everyone(setup, world, admin):
    dc = world.person(setup["home"], "Dana Koordinátorka", roles=["memberbase:district-coordinator"])
    for reader in (admin.dn, dc.dn):
        assert level(setup["bob"], reader) == "contact"
        assert level(setup["ext"], reader) == "contact"


def test_district_coordinator_cannot_write(setup, world):
    dc = world.person(setup["home"], "Dana Koordinátorka", roles=["memberbase:district-coordinator"])
    with pytest.raises(d.Denied):
        d.modify(setup["bob"].dn, {"cn": ["Změna"]}, dc.dn)


def test_person_may_change_own_phone_only(setup):
    alice = setup["alice"]
    d.modify(alice.dn, {"telephoneNumber": ["111222333"]}, alice.dn)
    for attr in ["cn", "mail", "crcMemberStatus"]:
        with pytest.raises(d.Denied):
            d.modify(alice.dn, {attr: ["x@example.org"]}, alice.dn)
    with pytest.raises(d.Denied):
        d.modify(setup["anna"].dn, {"telephoneNumber": ["111222333"]}, alice.dn)


def test_person_cannot_grant_themselves_a_role(setup, admin):
    role = next(r for r in people.list_roles(admin.dn) if r.key == "memberbase:admin")
    with pytest.raises(d.Denied):
        d.add_values(role.dn, "member", [setup["alice"].dn], setup["alice"].dn)


def test_role_membership_hidden_from_people(setup, admin):
    admin_role = f"cn=admin,ou=roles,ou=memberbase,ou=apps,{d.base_dn()}"
    assert "member" not in d.get(admin_role, ["member"], as_dn=setup["alice"].dn).attrs
    assert "member" in d.get(admin_role, ["member"], as_dn=admin.dn).attrs


def test_units_and_definitions_are_public(setup):
    alice = setup["alice"].dn
    assert people.list_units(alice)
    assert people.list_roles(alice)
    assert d.get(people.qualifications_base(), ["ou"], as_dn=alice) is not None


def test_grants_and_services_hidden_from_people(setup, admin):
    people.create_grant(setup["alice"].id, setup["other"], "basic", None, "", admin.id, admin.dn)
    assert people.list_grants(setup["alice"].dn) == []
    assert d.search(f"ou=services,{d.base_dn()}", as_dn=setup["alice"].dn) == []
    assert d.search(f"ou=services,{d.base_dn()}", as_dn=admin.dn) == []


def test_holdings_follow_person_visibility(setup, admin):
    people.save_qualification(None, "Zdravotník", "", [], True, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == "Zdravotník")
    people.set_holdings(setup["bob"], {qual.id}, admin.dn)
    assert people.holdings_of(setup["bob"], setup["bob"].dn) != {}
    assert people.holdings_of(setup["bob"], setup["alice"].dn) == {}


def test_accesslog_admins_only(setup, admin):
    filterstr = "(reqType=modify)"
    assert d.search("cn=accesslog", filterstr, ["reqDN"], as_dn=setup["alice"].dn) == []
    assert d.search("cn=accesslog", filterstr, ["reqDN"], as_dn=admin.dn)


def test_accesslog_records_the_real_person(setup, admin):
    people.update_person(setup["alice"], {"phone": "999888777"}, admin.dn, setup["alice"].csn)
    entries = d.search("cn=accesslog", f"(reqDN={setup['alice'].dn})", ["reqAuthzID"], as_dn=admin.dn)
    assert admin.dn.lower() in {e.first("reqAuthzID").lower() for e in entries}


# ── Service accounts ─────────────────────────────────────────────────────────


def test_memberbase_service_reads_login_data_but_not_phone(setup):
    seen = visible(setup["bob"], None)
    assert seen["cn"] and seen["mail"]
    assert "telephoneNumber" not in seen


def test_memberbase_service_cannot_edit_person_data(setup):
    people.set_status(setup["bob"], "inactive", None)
    with pytest.raises(d.Denied):
        d.modify(setup["bob"].dn, {"cn": ["Změna"]}, None)


def test_memberbase_service_cannot_act_as_a_service(admin):
    with pytest.raises(ldap.PROXIED_AUTHORIZATION_DENIED):
        d.search(d.base_dn(), as_dn=f"cn=keycloak,ou=services,{d.base_dn()}")


def test_sync_account_reads_only_medcover_people(setup, admin):
    role = next(r for r in people.list_roles(admin.dn) if r.key == "medcover:member")
    people.toggle_role(setup["bob"], role, True, admin.dn)
    conn = service_conn("medcover-sync", os.environ["TEST_SYNC_PASSWORD"])

    def read(person):
        try:
            return conn.search_s(person.dn, ldap.SCOPE_BASE, attrlist=["mail", "telephoneNumber", "givenName"])
        except ldap.NO_SUCH_OBJECT:
            return []

    (_, attrs), *_ = read(setup["bob"])
    assert set(attrs) == {"mail", "telephoneNumber"}
    assert read(setup["alice"]) == []
    medcover = conn.search_s(f"ou=medcover,ou=apps,{d.base_dn()}", ldap.SCOPE_SUBTREE)
    assert medcover
    # Definitions of other apps' roles are public, their holders are not.
    other_app = conn.search_s(f"ou=memberbase,ou=apps,{d.base_dn()}", ldap.SCOPE_SUBTREE, attrlist=["member"])
    assert all("member" not in attrs for _, attrs in other_app)
    conn.unbind_s()


def test_keycloak_account_changes_passwords_only(setup):
    conn = service_conn("keycloak", os.environ["TEST_KEYCLOAK_PASSWORD"])
    conn.passwd_s(setup["bob"].dn, None, "Nove-heslo-123")
    with pytest.raises(ldap.INSUFFICIENT_ACCESS):
        conn.modify_s(setup["bob"].dn, [(ldap.MOD_REPLACE, "cn", [b"Zmena"])])
    [(_, attrs)] = conn.search_s(setup["bob"].dn, ldap.SCOPE_BASE, attrlist=["mail", "telephoneNumber", "userPassword"])
    assert set(attrs) == {"mail"}
    conn.unbind_s()
    # The person can now bind with the new password; nobody reads the hash.
    user = d.connect()
    user.simple_bind_s(setup["bob"].dn, "Nove-heslo-123")
    user.unbind_s()
    assert "userPassword" not in d.get(setup["bob"].dn, ["userPassword"], as_dn=setup["bob"].dn).attrs


def test_expired_grants_are_revoked_by_the_job(setup, admin):
    past = datetime.now(UTC) - timedelta(minutes=1)
    people.create_grant(setup["alice"].id, setup["other"], "contact", past, "", admin.id, admin.dn)
    assert level(setup["bob"], setup["alice"].dn) == "contact"
    assert people.expire_grants() >= 1
    assert level(setup["bob"], setup["alice"].dn) == "none"


# ── MS Chair ─────────────────────────────────────────────────────────────────


@pytest.fixture
def chair(setup, world):
    return world.chair(setup["home"])


def test_chair_edits_people_of_own_unit_only(setup, chair):
    d.modify(setup["alice"].dn, {"cn": ["Alice Nová"], "mail": [f"n-{chair.id}@example.org"]}, chair.dn)
    people.set_status(setup["alice"], "inactive", chair.dn)
    for target in (setup["bob"], setup["ext"]):
        with pytest.raises(d.Denied):
            d.modify(target.dn, {"cn": ["Změna"]}, chair.dn)
    with pytest.raises(d.Denied):
        d.modify(setup["alice"].dn, {"crcMemberKind": ["external"]}, chair.dn)


@pytest.mark.parametrize("role", sorted(people.PRIVILEGED_ROLES))
def test_chair_cannot_edit_privileged_people(setup, world, chair, role):
    boss = world.person(setup["home"], "Olga Oprávněná", roles=[role])
    for attr, value in [("mail", "x@example.org"), ("crcMemberStatus", "inactive"), ("cn", "Změna")]:
        with pytest.raises(d.Denied):
            d.modify(boss.dn, {attr: [value]}, chair.dn)
    assert level(boss, chair.dn) == "contact"


def test_chair_cannot_edit_another_chair_or_themselves(setup, world, chair):
    other_chair = world.chair(setup["home"], "Pavel Místopředseda")
    for target in (other_chair, chair):
        with pytest.raises(d.Denied):
            d.modify(target.dn, {"mail": ["x@example.org"]}, chair.dn)
    d.modify(chair.dn, {"telephoneNumber": ["111222333"]}, chair.dn)


def test_chair_creates_people_in_own_unit_only(setup, chair):
    person = people.create_person("Člen", "Nový", f"n-{chair.id}@example.org", "", setup["home"], chair.dn)
    assert person.unit_dn.lower() == setup["home"].dn.lower()
    with pytest.raises(d.Denied):
        people.create_person("Člen", "Cizí", f"c-{chair.id}@example.org", "", setup["other"], chair.dn)


def _person_attrs(unit, chair, **extra):
    member_id = people.new_id()
    attrs = {
        "objectClass": ["inetOrgPerson", "crcMember"],
        "uid": [member_id],
        "crcMemberId": [member_id],
        "cn": ["Podvržený"],
        "sn": ["Podvržený"],
        "mail": [f"p-{member_id}@example.org"],
        "crcMemberStatus": ["active"],
        "crcMemberKind": ["member"],
    } | extra
    return f"uid={member_id},{unit.dn}", attrs


def test_chair_cannot_add_people_with_credentials_or_proxy_rights(setup, chair):
    for extra in (
        {"userPassword": ["heslo-123"]},
        {"authzTo": ["dn.regex:.*"]},
        {"description": ["cokoli"]},
    ):
        dn, attrs = _person_attrs(setup["home"], chair, **extra)
        with pytest.raises(d.Denied):
            d.add(dn, attrs, chair.dn)
    dn, attrs = _person_attrs(setup["home"], chair)
    d.add(dn, attrs, chair.dn)


def test_chair_cannot_change_holdings_of_privileged_people(setup, world, admin, chair):
    boss = world.person(setup["home"], "Olga Oprávněná", roles=["medcover:coordinator"])
    people.save_qualification(None, f"Kval2 {chair.id}", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == f"Kval2 {chair.id}")
    for target in (boss, chair):
        with pytest.raises(d.Denied):
            people.set_holdings(target, {qual.id}, chair.dn)


def test_chair_cannot_delete_or_move_people(setup, chair):
    with pytest.raises(d.Denied):
        d.delete(setup["alice"].dn, chair.dn)
    with pytest.raises(d.Denied):
        d.move(setup["alice"].dn, setup["other"].dn, chair.dn)


def test_chair_cannot_change_members_group(setup, chair):
    with pytest.raises(d.Denied):
        d.add_values(setup["home"].members_dn, "member", [setup["bob"].dn], chair.dn)


def test_chair_manages_holdings_of_own_unit_only(setup, admin, chair):
    people.save_qualification(None, f"Kval {chair.id}", "", [], False, admin.dn)
    qual = next(q for q in people.list_qualifications(admin.dn) if q.name == f"Kval {chair.id}")
    people.set_holdings(setup["alice"], {qual.id}, chair.dn)
    assert people.holdings_of(setup["alice"], chair.dn)
    people.set_holdings(setup["alice"], set(), chair.dn)
    assert not people.holdings_of(setup["alice"], chair.dn)
    with pytest.raises(d.Denied):
        people.set_holdings(setup["bob"], {qual.id}, chair.dn)


def test_only_admin_appoints_chairs(setup, chair):
    for person in (chair, setup["alice"]):
        with pytest.raises(d.Denied):
            people.set_chair(setup["anna"], True, person.dn)
    with pytest.raises(d.Denied):
        people.set_chair(chair, False, chair.dn)


def test_chair_cannot_assign_roles_or_grants(setup, admin, chair):
    role = next(r for r in people.list_roles(admin.dn) if r.key == "medcover:member")
    with pytest.raises(d.Denied):
        people.toggle_role(setup["alice"], role, True, chair.dn)
    with pytest.raises(d.Denied):
        people.create_grant(setup["alice"].id, setup["other"], "basic", None, "", chair.id, chair.dn)


def test_own_unit_sees_its_chairs_others_do_not(setup, chair):
    assert people.chair_members(setup["home"], setup["alice"].dn) == {chair.dn.lower()}
    assert people.chair_members(setup["home"], setup["bob"].dn) == set()
    assert people.memberships(chair.dn)[1] == {setup["home"].dn.lower()}


def test_chair_reads_nothing_more_of_other_units(setup, chair):
    assert level(setup["bob"], chair.dn) == "none"


# ── Requests ─────────────────────────────────────────────────────────────────


def _request(unit, requester, **extra):
    req_id = people.new_id()
    dn = f"crcRequestId={req_id},{unit.requests_dn}"
    attrs = {
        "objectClass": ["crcRequest", "crcAccessRequest"],
        "crcRequestId": [req_id],
        "crcRequestType": ["access"],
        "crcRequestedByDn": [requester.dn],
        "crcRequestStatus": ["pending"],
        "crcAccessName": ["Bob Cizí"],
        "crcAccessLevel": ["contact"],
    } | extra
    d.add(dn, attrs, requester.dn)
    return dn


def test_anyone_files_a_request_but_only_as_themselves(setup):
    alice = setup["alice"]
    dn = _request(setup["other"], alice)
    assert d.get(dn, ["crcRequestStatus"], alice.dn).first("crcRequestStatus") == "pending"
    with pytest.raises(d.Denied):
        _request(setup["other"], alice, crcRequestedByDn=[setup["anna"].dn])
    with pytest.raises(d.Denied):
        _request(setup["other"], setup["ext"])


def test_requests_cannot_carry_credentials_or_a_decision(setup, admin):
    alice = setup["alice"]
    for extra in (
        {"objectClass": ["crcRequest", "crcAccessRequest", "simpleSecurityObject"], "userPassword": ["heslo-123"]},
        {"crcRequestStatus": ["approved"]},
        {"crcDecidedBy": [admin.id]},
        {"crcAccessSubject": [setup["bob"].id]},
        {"authzTo": ["dn.regex:.*"]},
    ):
        with pytest.raises(d.Denied):
            _request(setup["other"], alice, **extra)


def test_request_readers(setup, world):
    other_chair = world.chair(setup["other"], "Olga Cizí")
    home_chair = world.chair(setup["home"], "Hana Domácí")
    dn = _request(setup["other"], setup["alice"], crcRequestNotify=[home_chair.dn])
    for reader in (setup["alice"], other_chair, home_chair):
        assert d.get(dn, ["crcRequestStatus"], reader.dn) is not None
    for reader in (setup["anna"], setup["bob"]):
        assert d.get(dn, ["crcRequestStatus"], reader.dn) is None


def test_only_the_units_chair_decides(setup, world):
    other_chair = world.chair(setup["other"], "Olga Cizí")
    home_chair = world.chair(setup["home"], "Hana Domácí")
    dn = _request(setup["other"], setup["alice"], crcRequestNotify=[home_chair.dn])
    for person in (setup["alice"], home_chair, setup["bob"]):
        with pytest.raises(d.Denied):
            d.modify(dn, {"crcRequestStatus": ["approved"]}, person.dn)
    with pytest.raises(d.Denied):
        d.modify(dn, {"crcAccessName": ["Někdo jiný"]}, other_chair.dn)
    d.swap(dn, "crcRequestStatus", "pending", "approved", other_chair.dn, {"crcAccessSubject": [setup["bob"].id]})
    with pytest.raises(d.Denied):
        d.delete(dn, other_chair.dn)


def test_notify_and_requester_follow_moves(setup, admin):
    dn = _request(setup["other"], setup["alice"], crcRequestNotify=[setup["anna"].dn])
    people.move_person(setup["alice"], setup["other"], admin.dn, setup["alice"].csn)
    people.move_person(setup["anna"], setup["other"], admin.dn, setup["anna"].csn)
    time.sleep(0.5)  # refint rewrites asynchronously
    entry = d.get(dn, ["crcRequestedByDn", "crcRequestNotify"], admin.dn)
    assert entry.first("crcRequestedByDn").lower().endswith(setup["other"].dn.lower())
    assert entry.first("crcRequestNotify").lower().endswith(setup["other"].dn.lower())


# ── Per-person grants ────────────────────────────────────────────────────────


def test_person_grant_shows_just_that_person(setup, world, admin):
    bob, alice = setup["bob"], setup["alice"]
    bella = world.person(setup["other"], "Bella Cizí")
    people.create_grant(alice.id, bob, "contact", None, "", admin.id, None)
    people.create_grant(alice.id, bob, "basic", None, "", admin.id, None)
    assert level(bob, alice.dn) == "contact"
    assert level(bella, alice.dn) == "none"
    assert level(bob, setup["anna"].dn) == "none"
    grant = people.list_grants(admin.dn, f"(&(crcGrantee={alice.id})(crcGrantTarget={bob.readers_dn('contact')}))")[0]
    assert grant.target_owner_dn.lower() == bob.dn.lower()
    people.create_grant(alice.id, bob, "contact", None, "", admin.id, None)  # group exists already
    people.revoke_grant(grant, None)
    assert level(bob, alice.dn) == "contact"  # the other grant still holds
    for grant in people.list_grants(
        admin.dn, f"(&(crcGrantee={alice.id})(crcGrantTarget={bob.readers_dn('contact')}))"
    ):
        people.revoke_grant(grant, None)
    assert level(bob, alice.dn) == "basic"


def test_person_grant_follows_a_move(setup, admin):
    bob, alice = setup["bob"], setup["alice"]
    people.create_grant(alice.id, bob, "contact", None, "", admin.id, None)
    people.move_person(bob, setup["home"], admin.dn, bob.csn)
    moved = people.find_person(bob.id, admin.dn)
    time.sleep(0.5)  # refint rewrites asynchronously
    grant = people.list_grants(admin.dn, f"(crcGrantee={alice.id})")[0]
    assert grant.target_dn.lower() == moved.readers_dn("contact").lower()


def test_only_memberbase_fills_person_readers_groups(setup, admin, world):
    chair = world.chair(setup["other"], "Olga Cizí")
    group = {"objectClass": ["crcGroup"], "cn": ["readers-basic"]}
    with pytest.raises(d.Denied):
        d.add(setup["bob"].readers_dn("basic"), group, chair.dn)
    people.create_grant(chair.id, setup["bob"], "basic", None, "", chair.id, None)
    with pytest.raises(d.Denied):
        d.add_values(setup["bob"].readers_dn("basic"), "member", [setup["alice"].dn], chair.dn)


# ── Service account: carrying out requests and the members job ──────────────


def test_memberbase_service_moves_people(setup):
    people.move_person(setup["bob"], setup["home"], None, None)
    assert people.find_person(setup["bob"].id, None).unit_dn.lower() == setup["home"].dn.lower()


def test_members_job_strips_archived_people_of_roles_and_chair(setup, world, admin):
    chair = world.chair(setup["home"], "Klára Končící", roles=["medcover:member"])
    d.modify(chair.dn, {"crcMemberStatus": ["former"]}, admin.dn)
    assert people.repair_members() >= 2
    assert people.memberships(chair.dn)[0] == set()
    assert people.memberships(chair.dn)[1] == set()


def test_chair_cannot_reopen_or_self_file_requests(setup, world, admin):
    other_chair = world.chair(setup["other"], "Olga Cizí")
    dn = _request(setup["other"], setup["alice"])
    d.swap(dn, "crcRequestStatus", "pending", "approved", other_chair.dn)
    d.swap(dn, "crcRequestStatus", "approved", "done", other_chair.dn)
    for old, new in [("done", "pending"), ("done", "approved")]:
        with pytest.raises(d.Denied):
            d.swap(dn, "crcRequestStatus", old, new, other_chair.dn)
    with pytest.raises(d.Denied):
        d.modify(dn, {"crcRequestStatus": ["pending"]}, other_chair.dn)
    with pytest.raises(d.Denied):
        _request(setup["other"], other_chair)


def test_odd_times_in_requests_do_not_break_listing(setup, admin):
    _request(setup["other"], setup["alice"], crcExpiresAt=["2099010100Z"])
    [req] = approvals.list_requests(setup["alice"].dn)
    assert req.expires_at.year == 2099 and req.requested_by_name == "Alice Domácí"
    assert people.parse_ldap_time("20990101") is None


# ── Certificates („Osvědčení“) ───────────────────────────────────────────────


def _cert(person, as_dn, name="Kurz"):
    people.save_certificate(person, None, name, "Úřad", datetime(2024, 1, 1).date(), None, as_dn)


def _cert_names(person, as_dn):
    return [c.name for c in people.certificates_of(person, as_dn)]


def test_certificates_hidden_from_own_branch_shown_to_person_and_readers(setup, world, admin):
    alice, bob = setup["alice"], setup["bob"]
    _cert(alice, admin.dn)
    assert _cert_names(alice, alice.dn) == ["Kurz"]
    assert _cert_names(alice, setup["anna"].dn) == []
    assert _cert_names(alice, bob.dn) == []
    assert _cert_names(alice, world.person(setup["home"], "Dana", roles=["memberbase:district-coordinator"]).dn)


@pytest.mark.parametrize("to_person", [False, True])
def test_records_grant_shows_name_and_certificates_not_contact(setup, admin, to_person):
    alice, bob = setup["alice"], setup["bob"]
    _cert(alice, admin.dn)
    people.create_grant(bob.id, alice if to_person else setup["home"], "records", None, "", admin.id, None)
    assert level(alice, bob.dn) == "basic"
    assert _cert_names(alice, bob.dn) == ["Kurz"]
    assert _cert_names(alice, setup["ext"].dn) == []


def test_records_grant_on_external_users(setup, admin):
    ext = setup["ext"]
    _cert(ext, admin.dn)
    assert _cert_names(ext, ext.dn) == ["Kurz"]
    assert _cert_names(ext, setup["alice"].dn) == []
    people.create_grant(setup["alice"].id, people.get_unit("external", admin.dn), "records", None, "", admin.id, None)
    assert level(ext, setup["alice"].dn) == "basic"
    assert _cert_names(ext, setup["alice"].dn) == ["Kurz"]


def test_contact_and_extended_grants_do_not_show_certificates(setup, admin):
    _cert(setup["bob"], admin.dn)
    for grant_level in ("contact", "extended"):
        people.create_grant(setup["alice"].id, setup["other"], grant_level, None, "", admin.id, None)
    assert _cert_names(setup["bob"], setup["alice"].dn) == []


def test_chair_manages_certificates_of_own_unit_only(setup, admin, chair):
    alice = setup["alice"]
    _cert(alice, chair.dn)
    (cert,) = people.certificates_of(alice, chair.dn)
    people.save_certificate(alice, cert, "Nový", "Úřad", cert.issued, None, chair.dn, cert.csn)
    people.delete_certificate(people.certificates_of(alice, chair.dn)[0], chair.dn)
    assert _cert_names(alice, chair.dn) == []
    _cert(setup["bob"], admin.dn)
    assert _cert_names(setup["bob"], chair.dn) == []
    with pytest.raises(d.Denied):
        _cert(setup["bob"], chair.dn)
    with pytest.raises(d.Denied):
        _cert(setup["anna"], setup["alice"].dn)


def test_chair_reads_but_cannot_change_certificates_of_privileged_people(setup, world, admin, chair):
    boss = world.person(setup["home"], "Olga Oprávněná", roles=["medcover:coordinator"])
    _cert(boss, admin.dn)
    _cert(chair, admin.dn)
    for target in (boss, chair):
        (cert,) = people.certificates_of(target, chair.dn)
        with pytest.raises(d.Denied):
            _cert(target, chair.dn, "Další")
        with pytest.raises(d.Denied):
            people.delete_certificate(cert, chair.dn)
        with pytest.raises(d.Denied):
            people.save_certificate(target, cert, "Změna", "Úřad", cert.issued, None, chair.dn, cert.csn)


def test_person_cannot_change_own_certificates(setup, admin):
    alice = setup["alice"]
    _cert(alice, admin.dn)
    (cert,) = people.certificates_of(alice, alice.dn)
    with pytest.raises(d.Denied):
        people.save_certificate(alice, cert, "Změna", "Úřad", cert.issued, None, alice.dn, cert.csn)
    with pytest.raises(d.Denied):
        _cert(alice, alice.dn, "Vlastní")


def test_sync_account_does_not_read_certificates(setup, admin):
    person = setup["alice"]
    people.toggle_role(
        person, next(r for r in people.list_roles(admin.dn) if r.key == "medcover:member"), True, admin.dn
    )
    _cert(person, admin.dn)
    conn = service_conn("medcover-sync", os.environ["TEST_SYNC_PASSWORD"])
    found = conn.search_s(person.dn, ldap.SCOPE_SUBTREE, "(objectClass=*)", ["cn"])
    assert found and not [dn for dn, _ in found if dn.lower().startswith("crccertificateid=")]


def test_only_the_service_account_creates_unit_readers_groups(setup, chair):
    with pytest.raises(d.Denied):
        d.add(f"cn=readers-x,{setup['home'].dn}", {"objectClass": ["crcGroup"], "cn": ["readers-x"]}, chair.dn)
    with pytest.raises(d.Denied):
        d.add(f"cn=readers-x,{people.external_dn()}", {"objectClass": ["crcGroup"], "cn": ["readers-x"]}, chair.dn)


def test_members_job_adds_missing_readers_groups(setup, admin):
    for unit in (setup["home"], people.get_unit("external", admin.dn)):
        d.delete(unit.readers_dn("records"), admin.dn)
    assert people.repair_members() >= 2
    assert d.get(setup["home"].readers_dn("records"), ["cn"], admin.dn) is not None
    assert d.get(f"cn=readers-records,{people.external_dn()}", ["cn"], admin.dn) is not None
    for dn in (setup["home"].readers_dn("records"), f"cn=readers-records,{people.external_dn()}"):
        with pytest.raises(d.Denied):
            d.delete(dn, None)  # the service account adds readers groups, never removes them
