"""Directory data model: people, Místní skupiny, app roles, qualifications,
visibility grants. Functions taking `as_dn` run as that person (the directory
decides what they may see or change); `as_dn=None` means MemberBase's own
service account and is used only where the design says so (login lookup,
activation of invited people, the grant and consistency jobs)."""

import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import ldap

from memberbase import directory as d
from memberbase.directory import Entry, escape_dn_chars, escape_filter_chars

STATUSES = {
    "new": "Nepozvaný",
    "invited": "Pozvaný",
    "active": "Aktivní",
    "inactive": "Neaktivní",
    "former": "Archivovaný",
}
LEVELS = {"basic": "Jméno", "contact": "Jméno a kontakt", "extended": "Rozšířené údaje"}
APPS = {"medcover": "MedCover", "memberbase": "Evidence členů"}
EXTERNAL_SLUG = "external"

PERSON_ATTRS = [
    "crcMemberId",
    "cn",
    "givenName",
    "sn",
    "mail",
    "telephoneNumber",
    "crcMemberStatus",
    "crcMemberKind",
    "crcStatusChangedAt",
    "entryUUID",
]


def sort_key(text: str) -> str:
    """Case- and accent-insensitive ordering, good enough for Czech names."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def new_id() -> str:
    return str(uuid.uuid4())


def now_ldap() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%SZ")


def parse_ldap_time(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def split_name(full_name: str) -> tuple[str, str]:
    """inetOrgPerson needs a surname: the last word; the rest is the given name."""
    parts = full_name.split()
    return " ".join(parts[:-1]), parts[-1]


# ── Units (Místní skupiny) ────────────────────────────────────────────────────


@dataclass
class Unit:
    dn: str
    id: str
    slug: str
    name: str

    @property
    def is_external(self) -> bool:
        return self.slug == EXTERNAL_SLUG

    @property
    def members_dn(self) -> str:
        return f"cn=members,{self.dn}"

    def readers_dn(self, level: str) -> str:
        return f"cn=readers-{level},{self.dn}"


def units_base() -> str:
    return f"ou=units,{d.base_dn()}"


def external_dn() -> str:
    return f"ou={EXTERNAL_SLUG},{d.base_dn()}"


def _unit(entry: Entry) -> Unit:
    return Unit(entry.dn, entry.first("crcUnitId"), entry.first("ou"), entry.first("displayName") or entry.first("ou"))


def list_units(as_dn: str | None, include_external: bool = False) -> list[Unit]:
    attrs = ["ou", "crcUnitId", "displayName"]
    units = [_unit(e) for e in d.search(units_base(), "(objectClass=crcUnit)", attrs, ldap.SCOPE_ONELEVEL, as_dn)]
    units.sort(key=lambda u: sort_key(u.name))
    if include_external:
        units += [_unit(e) for e in d.search(external_dn(), "(objectClass=crcUnit)", attrs, ldap.SCOPE_BASE, as_dn)]
    return units


def get_unit(unit_id: str, as_dn: str | None) -> Unit | None:
    for unit in list_units(as_dn, include_external=True):
        if unit.id == unit_id:
            return unit
    return None


def slugify(name: str) -> str:
    ascii_name = sort_key(name)
    slug = "".join(c if c.isalnum() else "-" for c in ascii_name)
    return "-".join(p for p in slug.split("-") if p) or "skupina"


def create_unit(name: str, as_dn: str) -> Unit:
    slug = slugify(name)
    taken = {u.slug for u in list_units(as_dn, include_external=True)}
    base_slug, n = slug, 2
    while slug in taken:
        slug, n = f"{base_slug}-{n}", n + 1
    unit = Unit(f"ou={escape_dn_chars(slug)},{units_base()}", new_id(), slug, name)
    d.add(
        unit.dn,
        {"objectClass": ["organizationalUnit", "crcUnit"], "ou": [slug], "crcUnitId": [unit.id], "displayName": [name]},
        as_dn,
    )
    for cn in ["members", *(f"readers-{level}" for level in LEVELS)]:
        d.add(f"cn={cn},{unit.dn}", {"objectClass": ["crcGroup"], "cn": [cn]}, as_dn)
    return unit


def rename_unit(unit: Unit, name: str, as_dn: str) -> None:
    d.modify(unit.dn, {"displayName": [name]}, as_dn)


# ── People ───────────────────────────────────────────────────────────────────


@dataclass
class Person:
    dn: str
    id: str
    name: str
    email: str
    phone: str
    status: str
    kind: str
    csn: str
    entry_uuid: str
    status_changed_at: datetime | None = None
    unit: Unit | None = None
    roles: set[str] = field(default_factory=set)

    @property
    def unit_dn(self) -> str:
        return self.dn.split(",", 1)[1]

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)


def _person(entry: Entry) -> Person:
    return Person(
        dn=entry.dn,
        id=entry.first("crcMemberId"),
        name=entry.first("cn"),
        email=entry.first("mail"),
        phone=entry.first("telephoneNumber"),
        status=entry.first("crcMemberStatus"),
        kind=entry.first("crcMemberKind"),
        csn=entry.first("entryCSN"),
        entry_uuid=entry.first("entryUUID"),
        status_changed_at=parse_ldap_time(entry.first("crcStatusChangedAt")),
    )


def _attach_units(people: list[Person], as_dn: str | None) -> None:
    units = {u.dn.lower(): u for u in list_units(as_dn, include_external=True)}
    for person in people:
        person.unit = units.get(person.unit_dn.lower())


def search_people(
    as_dn: str,
    q: str = "",
    unit: Unit | None = None,
    statuses: list[str] | None = None,
) -> list[Person]:
    parts = ["(objectClass=crcMember)"]
    q = "".join(c for c in q if c.isprintable())
    if q:
        esc = escape_filter_chars(q)
        # Phone matching ignores non-digits, so only digit queries search it.
        phone = f"(telephoneNumber=*{esc}*)" if q.replace(" ", "").lstrip("+").isdigit() else ""
        parts.append(f"(|(cn=*{esc}*)(mail=*{esc}*){phone})")
    if statuses:
        parts.append("(|" + "".join(f"(crcMemberStatus={escape_filter_chars(s)})" for s in statuses) + ")")
    filterstr = "(&" + "".join(parts) + ")"
    if unit is None:
        entries = d.search(units_base(), filterstr, PERSON_ATTRS, as_dn=as_dn)
        entries += d.search(external_dn(), filterstr, PERSON_ATTRS, ldap.SCOPE_ONELEVEL, as_dn)
    else:
        entries = d.search(unit.dn, filterstr, PERSON_ATTRS, ldap.SCOPE_ONELEVEL, as_dn)
    people = [_person(e) for e in entries]
    _attach_units(people, as_dn)
    people.sort(key=lambda p: sort_key(p.name))
    return people


def find_person(member_id: str, as_dn: str | None) -> Person | None:
    """Look a person up by crcMemberId. Returns None if absent or not visible."""
    filterstr = f"(&(objectClass=crcMember)(crcMemberId={escape_filter_chars(member_id)}))"
    entries = d.search(units_base(), filterstr, PERSON_ATTRS, as_dn=as_dn)
    entries += d.search(external_dn(), filterstr, PERSON_ATTRS, ldap.SCOPE_ONELEVEL, as_dn)
    if not entries:
        return None
    person = _person(entries[0])
    _attach_units([person], as_dn)
    return person


def create_person(name: str, email: str, phone: str, unit: Unit, as_dn: str) -> Person:
    member_id = new_id()
    dn = f"uid={member_id},{unit.dn}"
    given, surname = split_name(name)
    d.add(
        dn,
        {
            "objectClass": ["inetOrgPerson", "crcMember"],
            "uid": [member_id],
            "crcMemberId": [member_id],
            "cn": [name],
            "givenName": [given] if given else [],
            "sn": [surname],
            "mail": [email],
            "telephoneNumber": [phone] if phone else [],
            "crcMemberStatus": ["new"],
            "crcMemberKind": ["external" if unit.is_external else "member"],
            "crcStatusChangedAt": [now_ldap()],
        },
        as_dn,
    )
    if not unit.is_external:
        d.add_values(unit.members_dn, "member", [dn], as_dn)
    person = find_person(member_id, as_dn)
    assert person is not None
    return person


def update_person(person: Person, changes: dict[str, str], as_dn: str, csn: str) -> None:
    """Change name, email and/or phone. `csn` is the entryCSN the form was
    built from; a concurrent change raises StaleEntry."""
    mods: dict[str, list[str]] = {}
    if "name" in changes:
        given, surname = split_name(changes["name"])
        mods |= {"cn": [changes["name"]], "givenName": [given] if given else [], "sn": [surname]}
    if "email" in changes:
        mods["mail"] = [changes["email"]]
    if "phone" in changes:
        mods["telephoneNumber"] = [changes["phone"]] if changes["phone"] else []
    d.modify(person.dn, mods, as_dn, csn=csn)


def set_status(person: Person, status: str, as_dn: str | None, csn: str | None = None) -> None:
    d.modify(person.dn, {"crcMemberStatus": [status], "crcStatusChangedAt": [now_ldap()]}, as_dn, csn=csn)


def activate_invited(person: Person) -> None:
    """First login of an invited person. Runs as the service account; the
    assertion makes sure only an invited entry is switched to active."""
    d.modify(
        person.dn,
        {"crcMemberStatus": ["active"], "crcStatusChangedAt": [now_ldap()]},
        None,
        assertion="(crcMemberStatus=invited)",
    )


def move_person(person: Person, target: Unit, as_dn: str, csn: str) -> None:
    """Move to another Místní skupina (or to/from external users).

    The rename runs under the form's entryCSN, so a concurrent edit fails
    cleanly. refint rewrites the person's DN in role and readers groups, but
    asynchronously, so the old branch's cn=members entry is removed before
    the rename (and put back if the rename fails). A failure after the
    rename is fixed by the nightly members repair.
    """
    old_members = None if person.unit_dn.lower() == external_dn().lower() else f"cn=members,{person.unit_dn}"
    if old_members:
        d.delete_values(old_members, "member", [person.dn], as_dn)
    try:
        new_dn = d.move(person.dn, target.dn, as_dn, csn=csn)
    except d.StaleEntry:
        if old_members:
            d.add_values(old_members, "member", [person.dn], as_dn)
        raise
    d.modify(new_dn, {"crcMemberKind": ["external" if target.is_external else "member"]}, as_dn)
    if not target.is_external:
        d.add_values(target.members_dn, "member", [new_dn], as_dn)


# ── App roles ────────────────────────────────────────────────────────────────


@dataclass
class Role:
    dn: str
    app: str
    name: str
    label: str
    members: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.app}:{self.name}"


def list_roles(as_dn: str | None, with_members: bool = False) -> list[Role]:
    roles = []
    for app in APPS:
        base = f"ou=roles,ou={app},ou=apps,{d.base_dn()}"
        attrs = ["cn", "description"] + (["member"] if with_members else [])
        for e in d.search(base, "(objectClass=crcGroup)", attrs, ldap.SCOPE_ONELEVEL, as_dn):
            members = {m.lower() for m in e.all("member")}
            roles.append(Role(e.dn, app, e.first("cn"), e.first("description") or e.first("cn"), members))
    roles.sort(key=lambda r: (list(APPS).index(r.app), sort_key(r.label)))
    return roles


def roles_of(person_dn: str) -> set[str]:
    """Role keys ("app:name") of a person, read by the service account."""
    return {r.key for r in list_roles(None, with_members=True) if person_dn.lower() in r.members}


def set_roles(person: Person, wanted: set[str], as_dn: str) -> tuple[set[str], set[str]]:
    roles = {r.key: r for r in list_roles(as_dn, with_members=True)}
    current = {k for k, r in roles.items() if person.dn.lower() in r.members}
    wanted &= set(roles)
    for key in wanted - current:
        d.add_values(roles[key].dn, "member", [person.dn], as_dn)
    for key in current - wanted:
        d.delete_values(roles[key].dn, "member", [person.dn], as_dn)
    return wanted - current, current - wanted


def toggle_role(person: Person, role: Role, add: bool, as_dn: str) -> None:
    (d.add_values if add else d.delete_values)(role.dn, "member", [person.dn], as_dn)


def remove_all_roles(person: Person, as_dn: str) -> None:
    set_roles(person, set(), as_dn)


# ── Qualifications (MedCover namespace) ──────────────────────────────────────


@dataclass
class Qualification:
    dn: str
    id: str
    name: str
    description: str
    parents: list[str]
    can_be_rp: bool
    csn: str


def qualifications_base() -> str:
    return f"ou=qualifications,ou=medcover,ou=apps,{d.base_dn()}"


def list_qualifications(as_dn: str | None) -> list[Qualification]:
    attrs = ["crcQualificationId", "cn", "description", "crcParent", "crcCanBeRp"]
    found = d.search(qualifications_base(), "(objectClass=crcQualification)", attrs, ldap.SCOPE_ONELEVEL, as_dn)
    quals = [
        Qualification(
            e.dn,
            e.first("crcQualificationId"),
            e.first("cn"),
            e.first("description"),
            e.all("crcParent"),
            e.first("crcCanBeRp") == "TRUE",
            e.first("entryCSN"),
        )
        for e in found
    ]
    quals.sort(key=lambda q: sort_key(q.name))
    return quals


def save_qualification(
    qual: Qualification | None, name: str, description: str, parents: list[str], can_be_rp: bool, as_dn: str
) -> None:
    attrs = {
        "cn": [name],
        "description": [description] if description else [],
        "crcParent": parents,
        "crcCanBeRp": ["TRUE" if can_be_rp else "FALSE"],
    }
    if qual is None:
        qual_id = new_id()
        d.add(
            f"crcQualificationId={qual_id},{qualifications_base()}",
            {"objectClass": ["crcQualification", "crcMedCoverQualification"], "crcQualificationId": [qual_id]} | attrs,
            as_dn,
        )
    else:
        d.modify(qual.dn, attrs, as_dn, csn=qual.csn)


def holders(qual_id: str, as_dn: str) -> list[str]:
    """DNs of the people holding a qualification."""
    filterstr = f"(&(objectClass=crcHolding)(crcQualificationRef={escape_filter_chars(qual_id)}))"
    found = d.search(d.base_dn(), filterstr, ["crcHoldingId"], as_dn=as_dn)
    return [e.parent_dn for e in found]


def delete_qualification(qual: Qualification, as_dn: str) -> bool:
    """Delete an unused qualification. Returns False if someone holds it or
    another qualification names it as a parent."""
    if holders(qual.id, as_dn) or any(qual.id in q.parents for q in list_qualifications(as_dn)):
        return False
    d.delete(qual.dn, as_dn)
    return True


def holdings_of(person: Person, as_dn: str) -> dict[str, str]:
    """crcQualificationRef → holding DN."""
    found = d.search(person.dn, "(objectClass=crcHolding)", ["crcQualificationRef"], ldap.SCOPE_ONELEVEL, as_dn)
    return {e.first("crcQualificationRef"): e.dn for e in found}


def set_holdings(person: Person, wanted: set[str], as_dn: str) -> tuple[set[str], set[str]]:
    current = holdings_of(person, as_dn)
    wanted &= {q.id for q in list_qualifications(as_dn)}
    for qual_id in wanted - set(current):
        holding_id = new_id()
        d.add(
            f"crcHoldingId={holding_id},{person.dn}",
            {"objectClass": ["crcHolding"], "crcHoldingId": [holding_id], "crcQualificationRef": [qual_id]},
            as_dn,
        )
    for qual_id in set(current) - wanted:
        d.delete(current[qual_id], as_dn)
    return wanted - set(current), set(current) - wanted


# ── Visibility grants ────────────────────────────────────────────────────────


@dataclass
class Grant:
    dn: str
    id: str
    grantee: str
    target_dn: str
    expires_at: datetime | None
    approved_by: str
    description: str

    @property
    def level(self) -> str:
        return self.target_dn.split(",", 1)[0].removeprefix("cn=readers-")

    @property
    def target_unit_dn(self) -> str:
        return self.target_dn.split(",", 1)[1]

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


def grants_base() -> str:
    return f"ou=grants,{d.base_dn()}"


def list_grants(as_dn: str | None, filterstr: str = "(objectClass=crcGrant)") -> list[Grant]:
    attrs = ["crcGrantId", "crcGrantee", "crcGrantTarget", "crcExpiresAt", "crcApprovedBy", "description"]
    return [
        Grant(
            e.dn,
            e.first("crcGrantId"),
            e.first("crcGrantee"),
            e.first("crcGrantTarget"),
            parse_ldap_time(e.first("crcExpiresAt")),
            e.first("crcApprovedBy"),
            e.first("description"),
        )
        for e in d.search(grants_base(), filterstr, attrs, ldap.SCOPE_ONELEVEL, as_dn)
    ]


def grantee_member_dn(grantee: str, as_dn: str | None) -> str | None:
    """The readers-group member for a grantee: the person's DN, or a
    Místní skupina's cn=members group."""
    unit = get_unit(grantee, as_dn)
    if unit is not None:
        return None if unit.is_external else unit.members_dn
    person = find_person(grantee, as_dn)
    return person.dn if person else None


def create_grant(
    grantee: str,
    target: Unit,
    level: str,
    expires_at: datetime | None,
    description: str,
    approved_by: str,
    as_dn: str,
) -> None:
    member_dn = grantee_member_dn(grantee, as_dn)
    if member_dn is None:
        raise ValueError(grantee)
    grant_id = new_id()
    d.add(
        f"crcGrantId={grant_id},{grants_base()}",
        {
            "objectClass": ["crcGrant"],
            "crcGrantId": [grant_id],
            "crcGrantee": [grantee],
            "crcGrantTarget": [target.readers_dn(level)],
            "crcExpiresAt": [expires_at.strftime("%Y%m%d%H%M%SZ")] if expires_at else [],
            "crcApprovedBy": [approved_by],
            "description": [description] if description else [],
        },
        as_dn,
    )
    d.add_values(target.readers_dn(level), "member", [member_dn], as_dn)


def revoke_grant(grant: Grant, as_dn: str | None) -> None:
    """Delete the grant record, and the readers membership unless another
    live grant still gives the same grantee the same group."""
    d.delete(grant.dn, as_dn)
    same = (
        f"(&(crcGrantee={escape_filter_chars(grant.grantee)})(crcGrantTarget={escape_filter_chars(grant.target_dn)}))"
    )
    if any(not g.expired for g in list_grants(as_dn, same)):
        return
    member_dn = grantee_member_dn(grant.grantee, as_dn)
    if member_dn is not None:
        d.delete_values(grant.target_dn, "member", [member_dn], as_dn)


def expire_grants() -> int:
    """Scheduled job (service account): revoke every grant past its expiry."""
    expired = [g for g in list_grants(None, f"(crcExpiresAt<={now_ldap()})") if g.expired]
    for grant in expired:
        revoke_grant(grant, None)
    return len(expired)


def repair_members() -> int:
    """Scheduled job (service account): make every cn=members group equal to
    the people actually inside the Místní skupina. Returns groups fixed."""
    fixed = 0
    for unit in list_units(None):
        actual = {e.dn for e in d.search(unit.dn, "(objectClass=crcMember)", ["crcMemberId"], ldap.SCOPE_ONELEVEL)}
        group = d.get(unit.members_dn, ["member"])
        recorded = set(group.all("member")) if group else set()
        if {x.lower() for x in actual} != {x.lower() for x in recorded}:
            d.modify(unit.members_dn, {"member": sorted(actual)}, None)
            fixed += 1
    return fixed
