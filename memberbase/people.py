"""Directory data model: people, Místní skupiny, app roles, qualifications,
certificates, visibility grants. Functions taking `as_dn` run as that person (the directory
decides what they may see or change); `as_dn=None` means MemberBase's own
service account and is used only where the design says so (login lookup,
activation of invited people, the grant and consistency jobs)."""

import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
STATUS_ORDER = {status: i for i, status in enumerate(STATUSES)}
# "records" is not above "extended": it shows the name and certificates only.
LEVELS = {"basic": "Jméno", "contact": "Jméno a kontakt", "extended": "Rozšířené údaje", "records": "Jméno a osvědčení"}
# Everyone not archived.
CURRENT_STATUSES = ["new", "invited", "active", "inactive"]
APPS = {"medcover": "MedCover", "memberbase": "Evidence členů"}
ADMIN_ROLE = "memberbase:admin"
EXTERNAL_SLUG = "external"
PRAGUE = ZoneInfo("Europe/Prague")

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


def ldap_time(moment: datetime) -> str:
    """datetime (UTC) → GeneralizedTime."""
    return moment.strftime("%Y%m%d%H%M%SZ")


def now_ldap() -> str:
    return ldap_time(datetime.now(UTC))


def parse_ldap_time(value: str) -> datetime | None:
    """GeneralizedTime (UTC) → datetime; minutes and seconds may be missing.
    A value that does not parse counts as missing: the directory accepts
    forms this app never writes."""
    digits = value[:14].rstrip("Z")
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d%H"):
        try:
            return datetime.strptime(digits, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def full_name(surname: str, given_name: str) -> str:
    """Names are always shown surname first."""
    return f"{surname} {given_name}".strip()


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

    @property
    def chair_dn(self) -> str:
        return f"cn=chair,{self.dn}"

    @property
    def requests_dn(self) -> str:
        return f"ou=requests,{self.dn}"

    def readers_dn(self, level: str) -> str:
        return f"cn=readers-{level},{self.dn}"

    @property
    def member_kind(self) -> str:
        """crcMemberKind of the people inside."""
        return "external" if self.is_external else "member"


REQUESTS_OU = {"objectClass": ["organizationalUnit"], "ou": ["requests"]}


def _group(cn: str) -> dict[str, list[str]]:
    return {"objectClass": ["crcGroup"], "cn": [cn]}


def units_base() -> str:
    return f"ou=units,{d.base_dn()}"


def external_dn() -> str:
    return f"ou={EXTERNAL_SLUG},{d.base_dn()}"


def _unit(entry: Entry) -> Unit:
    return Unit(entry.dn, entry.first("crcUnitId"), entry.first("ou"), entry.first("displayName") or entry.first("ou"))


def list_units(as_dn: str | None, include_external: bool = False) -> list[Unit]:
    """Místní skupiny by name, then (if asked) the external users."""
    found = [
        _unit(e)
        for e in d.search(d.base_dn(), "(objectClass=crcUnit)", ["ou", "crcUnitId", "displayName"], as_dn=as_dn)
    ]
    units = sorted((u for u in found if not u.is_external), key=lambda u: sort_key(u.name))
    return units + [u for u in found if u.is_external] if include_external else units


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
    for cn in ["members", "chair", *(f"readers-{level}" for level in LEVELS)]:
        d.add(f"cn={cn},{unit.dn}", _group(cn), as_dn)
    d.add(unit.requests_dn, REQUESTS_OU, as_dn)
    return unit


def rename_unit(unit: Unit, name: str, as_dn: str) -> None:
    d.modify(unit.dn, {"displayName": [name]}, as_dn)


# ── People ───────────────────────────────────────────────────────────────────


@dataclass
class Person:
    dn: str
    id: str
    surname: str
    given_name: str
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
    def name(self) -> str:
        # From sn and givenName, not cn: entries from before surname-first names keep "Given Surname" in cn.
        return full_name(self.surname, self.given_name)

    @property
    def unit_dn(self) -> str:
        return self.dn.split(",", 1)[1]

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)

    def readers_dn(self, level: str) -> str:
        """Readers group of a grant for just this person, created on demand."""
        return f"cn=readers-{level},{self.dn}"


def _person(entry: Entry) -> Person:
    return Person(
        dn=entry.dn,
        id=entry.first("crcMemberId"),
        surname=entry.first("sn"),
        given_name=entry.first("givenName"),
        email=entry.first("mail"),
        phone=entry.first("telephoneNumber"),
        status=entry.first("crcMemberStatus"),
        kind=entry.first("crcMemberKind"),
        csn=entry.first("entryCSN"),
        entry_uuid=entry.first("entryUUID"),
        status_changed_at=parse_ldap_time(entry.first("crcStatusChangedAt")),
    )


def _attach_units(people: list[Person], as_dn: str | None, units: list[Unit] | None = None) -> None:
    by_dn = {u.dn.lower(): u for u in (units or list_units(as_dn, include_external=True))}
    for person in people:
        person.unit = by_dn.get(person.unit_dn.lower())


def search_people(
    as_dn: str,
    q: str = "",
    unit: Unit | None = None,
    statuses: list[str] | None = None,
    ids: list[str] | None = None,
    units: list[Unit] | None = None,
) -> list[Person]:
    """People matching all given criteria, by name. `units` (every visible
    Místní skupina and the external users) saves listing them again."""
    parts = ["(objectClass=crcMember)"]
    q = "".join(c for c in q if c.isprintable())
    if q:
        esc = escape_filter_chars(q)
        # Phone matching ignores non-digits, so only digit queries search it.
        phone = f"(telephoneNumber=*{esc}*)" if q.replace(" ", "").lstrip("+").isdigit() else ""
        parts.append(f"(|(cn=*{esc}*)(mail=*{esc}*){phone})")
    if statuses:
        parts.append("(|" + "".join(f"(crcMemberStatus={escape_filter_chars(s)})" for s in statuses) + ")")
    if ids is not None:
        parts.append("(|" + "".join(f"(crcMemberId={escape_filter_chars(i)})" for i in ids) + ")")
    filterstr = "(&" + "".join(parts) + ")"
    if unit is None:
        entries = d.search(d.base_dn(), filterstr, PERSON_ATTRS, as_dn=as_dn)
    else:
        entries = d.search(unit.dn, filterstr, PERSON_ATTRS, ldap.SCOPE_ONELEVEL, as_dn)
    people = [_person(e) for e in entries]
    _attach_units(people, as_dn, units)
    people.sort(key=lambda p: sort_key(p.name))
    return people


def find_person(member_id: str, as_dn: str | None, with_unit: bool = True) -> Person | None:
    """Look a person up by crcMemberId. Returns None if absent or not visible.
    Without `with_unit`, `unit` stays None and listing the units is saved."""
    filterstr = f"(&(objectClass=crcMember)(crcMemberId={escape_filter_chars(member_id)}))"
    entries = d.search(d.base_dn(), filterstr, PERSON_ATTRS, as_dn=as_dn)
    if not with_unit:
        return _person(entries[0]) if entries else None
    return _first_person(entries, as_dn)


def person_at(dn: str, as_dn: str | None) -> Person | None:
    """The person with this DN. Returns None if absent or not visible."""
    return _first_person(d.search(dn, "(objectClass=crcMember)", PERSON_ATTRS, ldap.SCOPE_BASE, as_dn), as_dn)


def _first_person(entries: list[Entry], as_dn: str | None) -> Person | None:
    if not entries:
        return None
    person = _person(entries[0])
    _attach_units([person], as_dn)
    return person


def create_person(surname: str, given_name: str, email: str, phone: str, unit: Unit, as_dn: str) -> Person:
    member_id = new_id()
    dn = f"uid={member_id},{unit.dn}"
    d.add(
        dn,
        {
            "objectClass": ["inetOrgPerson", "crcMember"],
            "uid": [member_id],
            "crcMemberId": [member_id],
            "cn": [full_name(surname, given_name)],
            "givenName": [given_name] if given_name else [],
            "sn": [surname],
            "mail": [email],
            "telephoneNumber": [phone] if phone else [],
            "crcMemberStatus": ["new"],
            "crcMemberKind": [unit.member_kind],
            "crcStatusChangedAt": [now_ldap()],
        },
        as_dn,
    )
    if not unit.is_external:
        try:
            d.add_values(unit.members_dn, "member", [dn], as_dn)
        except d.Denied:
            pass  # a Chair: joins at first login or by the members repair
    person = find_person(member_id, as_dn)
    assert person is not None
    return person


def update_person(person: Person, changes: dict[str, str], as_dn: str, csn: str) -> None:
    """Change name (surname and given_name together), email and/or phone.
    `csn` is the entryCSN the form was built from; a concurrent change raises
    StaleEntry."""
    mods: dict[str, list[str]] = {}
    if "surname" in changes:
        surname, given = changes["surname"], changes["given_name"]
        mods |= {"cn": [full_name(surname, given)], "givenName": [given] if given else [], "sn": [surname]}
    if "email" in changes:
        mods["mail"] = [changes["email"]]
    if "phone" in changes:
        mods["telephoneNumber"] = [changes["phone"]] if changes["phone"] else []
    d.modify(person.dn, mods, as_dn, csn=csn)


def set_status(person: Person, status: str, as_dn: str | None, csn: str | None = None) -> None:
    d.modify(person.dn, {"crcMemberStatus": [status], "crcStatusChangedAt": [now_ldap()]}, as_dn, csn=csn)


def activate_invited(person: Person) -> None:
    """First login of an invited person. Runs as the service account; the
    assertion makes sure only an invited entry is switched to active. Also
    joins the Místní skupina's cn=members (a Chair who added them could not)."""
    d.modify(
        person.dn,
        {"crcMemberStatus": ["active"], "crcStatusChangedAt": [now_ldap()]},
        None,
        assertion="(crcMemberStatus=invited)",
    )
    if person.unit is not None and not person.unit.is_external:
        d.add_values(person.unit.members_dn, "member", [person.dn], None)


def move_person(person: Person, target: Unit, as_dn: str | None, csn: str | None) -> None:
    """Move to another Místní skupina (or to/from external users).

    The rename runs under the form's entryCSN, so a concurrent edit fails
    cleanly. refint rewrites the person's DN in role and readers groups, but
    asynchronously, so the old branch's cn=members entry is removed before
    the rename (and put back if the rename fails). A failure after the
    rename is fixed by the nightly members repair. Chairing the old
    Místní skupina ends with the move.
    """
    old_members = None if person.unit_dn.lower() == external_dn().lower() else f"cn=members,{person.unit_dn}"
    if old_members:
        d.delete_values(f"cn=chair,{person.unit_dn}", "member", [person.dn], as_dn)
        d.delete_values(old_members, "member", [person.dn], as_dn)
    try:
        new_dn = d.move(person.dn, target.dn, as_dn, csn=csn)
    except d.StaleEntry:
        if old_members:
            d.add_values(old_members, "member", [person.dn], as_dn)
        raise
    d.modify(new_dn, {"crcMemberKind": [target.member_kind]}, as_dn)
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


def list_roles(as_dn: str | None, with_members: bool = False, apps: tuple[str, ...] = tuple(APPS)) -> list[Role]:
    roles = []
    for app in apps:
        base = f"ou=roles,ou={app},ou=apps,{d.base_dn()}"
        attrs = ["cn", "description"] + (["member"] if with_members else [])
        for e in d.search(base, "(objectClass=crcGroup)", attrs, ldap.SCOPE_ONELEVEL, as_dn):
            members = {m.lower() for m in e.all("member")}
            roles.append(Role(e.dn, app, e.first("cn"), e.first("description") or e.first("cn"), members))
    roles.sort(key=lambda r: (list(APPS).index(r.app), sort_key(r.label)))
    return roles


# Holding any of these gives access to MedCover and the MedCover grant. The
# directory's access rules use the same list (entrypoint.sh, MEDCOVER_USERS).
MEDCOVER_ROLES = ("admin", "coordinator", "member", "viewer", "debriefing-manager")


def has_medcover_access(role_keys: set[str]) -> bool:
    return any(f"medcover:{role}" in role_keys for role in MEDCOVER_ROLES)


def role_keys(person: Person, roles: list[Role]) -> set[str]:
    """Which of `roles` (listed with members) the person holds."""
    return {r.key for r in roles if person.dn.lower() in r.members}


def memberships(person_dn: str) -> tuple[set[str], set[str]]:
    """Role keys ("app:name") of a person, and the DNs (lower case) of the
    Místní skupiny they chair, in one search by the service account."""
    filterstr = f"(&(objectClass=crcGroup)(member={escape_filter_chars(person_dn)}))"
    apps, units = f",ou=apps,{d.base_dn()}".lower(), f",{units_base()}".lower()
    roles, chairs = set(), set()
    for e in d.search(d.base_dn(), filterstr, ["cn"]):
        dn = e.dn.lower()
        if dn.endswith(apps):
            roles.add(f"{dn.split(',')[2].removeprefix('ou=')}:{e.first('cn')}")
        elif dn.startswith("cn=chair,") and dn.endswith(units):
            chairs.add(e.parent_dn.lower())
    return roles, chairs


def set_roles(person: Person, wanted: set[str], as_dn: str) -> tuple[set[str], set[str]]:
    listed = list_roles(as_dn, with_members=True)
    roles = {r.key: r for r in listed}
    current = role_keys(person, listed)
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
    if is_chair(person, as_dn):
        set_chair(person, False, as_dn)


# Roles whose holders only Admins may edit, change status of, or move (the
# directory's access rules list the same roles, plus Chairs).
PRIVILEGED_ROLES = {ADMIN_ROLE, "memberbase:district-coordinator", "medcover:admin", "medcover:coordinator"}


def chair_members(unit: Unit, as_dn: str | None) -> set[str]:
    """DNs (lower case) of the Chairs of a Místní skupina, if visible."""
    group = d.get(unit.chair_dn, ["member"], as_dn)
    return {m.lower() for m in group.all("member")} if group else set()


def is_chair(person: Person, as_dn: str | None) -> bool:
    """Chairs their own Místní skupina, as far as `as_dn` can see."""
    unit = person.unit
    return unit is not None and not unit.is_external and person.dn.lower() in chair_members(unit, as_dn)


def set_chair(person: Person, add: bool, as_dn: str) -> None:
    """Make a person Chair of their own Místní skupina, or stop."""
    (d.add_values if add else d.delete_values)(f"cn=chair,{person.unit_dn}", "member", [person.dn], as_dn)


def is_privileged(person: Person) -> bool:
    """Holds a privileged role or chairs a Místní skupina (service account)."""
    roles, chairs = memberships(person.dn)
    return bool(roles & PRIVILEGED_ROLES) or bool(chairs)


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


def holding_counts(as_dn: str) -> Counter[str]:
    """How many people hold each qualification (by id)."""
    found = d.search(d.base_dn(), "(objectClass=crcHolding)", ["crcQualificationRef"], as_dn=as_dn)
    return Counter(e.first("crcQualificationRef") for e in found)


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


# ── Certificates („Osvědčení“): certifications, training, diplomas ──────────

EXPIRING_DAYS = 90


@dataclass
class Certificate:
    dn: str
    id: str
    name: str
    issuer: str
    issued: date | None
    expires: date | None  # None: does not expire
    csn: str

    @property
    def person_dn(self) -> str:
        return self.dn.split(",", 1)[1]

    @property
    def state(self) -> str:
        """valid | expiring | expired; valid through the expiry day (Prague)."""
        today = datetime.now(PRAGUE).date()
        if self.expires is None or self.expires >= today + timedelta(days=EXPIRING_DAYS):
            return "valid"
        return "expired" if self.expires < today else "expiring"


def _day(value: str) -> date | None:
    moment = parse_ldap_time(value)
    return moment.date() if moment else None


def list_certificates(as_dn: str, base_dn: str | None = None, scope: int = ldap.SCOPE_SUBTREE) -> list[Certificate]:
    """Certificates under `base_dn` (default: everywhere) that `as_dn` may
    read, newest first."""
    attrs = ["crcCertificateId", "cn", "crcIssuer", "crcValidFrom", "crcValidUntil"]
    found = d.search(base_dn or d.base_dn(), "(objectClass=crcCertificate)", attrs, scope, as_dn)
    certs = [
        Certificate(
            e.dn,
            e.first("crcCertificateId"),
            e.first("cn"),
            e.first("crcIssuer"),
            _day(e.first("crcValidFrom")),
            _day(e.first("crcValidUntil")),
            e.first("entryCSN"),
        )
        for e in found
    ]
    certs.sort(key=lambda c: c.issued or date.min, reverse=True)
    return certs


def certificates_of(person: Person, as_dn: str) -> list[Certificate]:
    return list_certificates(as_dn, person.dn, ldap.SCOPE_ONELEVEL)


def save_certificate(
    person: Person,
    cert: Certificate | None,
    name: str,
    issuer: str,
    issued: date,
    expires: date | None,
    as_dn: str,
    csn: str = "",
) -> None:
    """Create a certificate of `person`, or change `cert`. `csn` is the
    entryCSN the form was built from; a concurrent change raises StaleEntry."""
    attrs = {
        "cn": [name],
        "crcIssuer": [issuer] if issuer else [],
        "crcValidFrom": [f"{issued:%Y%m%d}000000Z"],
        "crcValidUntil": [f"{expires:%Y%m%d}000000Z"] if expires else [],
    }
    if cert is None:
        cert_id = new_id()
        d.add(
            f"crcCertificateId={cert_id},{person.dn}",
            {"objectClass": ["crcCertificate"], "crcCertificateId": [cert_id]} | attrs,
            as_dn,
        )
    else:
        d.modify(cert.dn, attrs, as_dn, csn=csn)


def delete_certificate(cert: Certificate, as_dn: str) -> None:
    d.delete(cert.dn, as_dn)


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
    def target_owner_dn(self) -> str:
        """The Místní skupina, or the one person, whose data the grant shows."""
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
    target: Unit | Person,
    level: str,
    expires_at: datetime | None,
    description: str,
    approved_by: str,
    as_dn: str | None,
) -> None:
    """Let a person or a Místní skupina see a Místní skupina, or one person."""
    member_dn = grantee_member_dn(grantee, as_dn)
    if member_dn is None:
        raise ValueError(grantee)
    if isinstance(target, Person):
        try:
            d.add(target.readers_dn(level), _group(f"readers-{level}"), as_dn)
        except ldap.ALREADY_EXISTS:
            pass
    grant_id = new_id()
    d.add(
        f"crcGrantId={grant_id},{grants_base()}",
        {
            "objectClass": ["crcGrant"],
            "crcGrantId": [grant_id],
            "crcGrantee": [grantee],
            "crcGrantTarget": [target.readers_dn(level)],
            "crcExpiresAt": [ldap_time(expires_at)] if expires_at else [],
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
    the people actually inside the Místní skupina, and take roles and
    chairing away from archived people (an archiving Chair cannot), and add
    missing cn=chair, readers groups and ou=requests. Returns entries fixed."""
    fixed = 0
    all_units = list_units(None, include_external=True)
    units = [u for u in all_units if not u.is_external]
    # Místní skupiny (and external users) from before a readers level existed.
    readers = {e.dn.lower() for e in d.search(d.base_dn(), "(&(objectClass=crcGroup)(cn=readers-*))", ["cn"])}
    for unit in all_units:
        for level in LEVELS:
            if unit.readers_dn(level).lower() not in readers:
                d.add(unit.readers_dn(level), _group(f"readers-{level}"), None)
                fixed += 1
    former = {e.dn.lower() for e in d.search(d.base_dn(), "(&(objectClass=crcMember)(crcMemberStatus=former))", ["cn"])}
    groups = {r.dn: r.members for r in list_roles(None, with_members=True)}
    groups |= {u.chair_dn: chair_members(u, None) for u in units}
    for dn, members in groups.items():
        if stale := sorted(members & former):
            d.delete_values(dn, "member", stale, None)
            fixed += 1
    for unit in units:
        # Místní skupiny created before Chairs and requests existed.
        if d.get(unit.chair_dn, ["cn"]) is None:
            d.add(unit.chair_dn, _group("chair"), None)
            fixed += 1
        if d.get(unit.requests_dn, ["ou"]) is None:
            d.add(unit.requests_dn, REQUESTS_OU, None)
            fixed += 1
        actual = {e.dn for e in d.search(unit.dn, "(objectClass=crcMember)", ["crcMemberId"], ldap.SCOPE_ONELEVEL)}
        group = d.get(unit.members_dn, ["member"])
        recorded = set(group.all("member")) if group else set()
        if {x.lower() for x in actual} != {x.lower() for x in recorded}:
            d.modify(unit.members_dn, {"member": sorted(actual)}, None)
            fixed += 1
    return fixed
