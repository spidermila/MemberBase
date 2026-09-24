"""Directory access rules: who may read or change what. Every rule is checked
for both the allowed and the denied case, against the real OpenLDAP image."""

import os
from datetime import UTC, datetime, timedelta

import ldap
import pytest

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


def test_memberbase_service_may_only_change_status(setup):
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
