"""Change log („Historie změn“) rendered from slapd's accesslog database.
slapd writes it for every successful change, with the real person (proxied
identity), old and new values; nobody can edit it."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from memberbase import approvals
from memberbase import directory as d
from memberbase import people
from memberbase.directory import escape_filter_chars

ACCESSLOG_BASE = "cn=accesslog"
LIMIT = 300

TYPES = {"add": "Vytvoření", "modify": "Změna", "delete": "Odstranění", "modrdn": "Přesun"}
ATTR_LABELS = {
    "cn": "Jméno",
    "mail": "E-mail",
    "telephoneNumber": "Telefon",
    "crcMemberStatus": "Stav",
    "crcMemberKind": "Druh",
    "member": "Člen",
    "displayName": "Název",
    "description": "Popis",
    "crcParent": "Nadřazená kvalifikace",
    "crcCanBeRp": "Může být zodpovědná osoba",
    "crcQualificationRef": "Kvalifikace",
    "crcGrantee": "Komu",
    "crcGrantTarget": "Úroveň a skupina",
    "crcExpiresAt": "Platnost do",
    "crcRequestStatus": "Stav žádosti",
    "crcDecidedBy": "Kdo rozhodl",
    "crcAccessName": "Požadovaná jména",
    "crcAccessSubject": "Zpřístupněné osoby",
    "crcAccessLevel": "Rozsah",
    "crcMoveSubjectName": "Koho přesunout",
    "userPassword": "Heslo",
}
# Bookkeeping attributes, or ones derived from those shown.
HIDDEN = {
    "entryCSN",
    "entryUUID",
    "modifiersName",
    "modifyTimestamp",
    "creatorsName",
    "createTimestamp",
    "structuralObjectClass",
    "objectClass",
    "givenName",
    "sn",
    "uid",
    "crcStatusChangedAt",
    "crcMemberId",
    "crcUnitId",
    "crcHoldingId",
    "crcGrantId",
    "crcQualificationId",
    "crcRequestId",
    "crcRequestType",
    "crcRequestedByDn",
    "crcRequestNotify",
    "crcDecidedAt",
    "crcMoveSubject",
    "crcMoveFromUnit",
}
OPS = {"+": "přidáno", "-": "odebráno", "=": "nastaveno", "": "smazáno"}


@dataclass
class Change:
    when: datetime
    actor: str
    action: str
    target: str
    details: list[str] = field(default_factory=list)


class Labels:
    """Turns DNs and ids in log records into names an admin recognises."""

    def __init__(self, as_dn: str) -> None:
        base = d.base_dn().lower()
        self.dns: dict[str, str] = {
            f"cn=memberbase,ou=services,{base}": "Evidence členů (automaticky)",
            f"cn=keycloak,ou=services,{base}": "Přihlašování (Keycloak)",
        }
        for person in people.search_people(as_dn):
            self.dns[person.dn.lower()] = person.name
        for unit in people.list_units(as_dn, include_external=True):
            self.dns[unit.dn.lower()] = unit.name
            self.dns[unit.members_dn.lower()] = f"všichni z {unit.name}"
            for level, label in people.LEVELS.items():
                self.dns[unit.readers_dn(level).lower()] = f"{label} – {unit.name}"
        for role in people.list_roles(as_dn):
            self.dns[role.dn.lower()] = f"role {people.APPS[role.app]}: {role.label}"
        self.ids = {q.id: q.name for q in people.list_qualifications(as_dn)}
        self.ids |= {p_id: name for p_id, name in self._person_ids()}

    def _person_ids(self) -> list[tuple[str, str]]:
        return [
            (dn.split(",", 1)[0].removeprefix("uid="), name) for dn, name in self.dns.items() if dn.startswith("uid=")
        ]

    def dn(self, dn: str) -> str:
        key = dn.lower()
        if key in self.dns:
            return self.dns[key]
        rdn = key.split(",", 1)[0]
        if rdn.startswith("crcholdingid=") and "," in key:
            return f"kvalifikace – {self.dn(key.split(',', 1)[1])}"
        if rdn.startswith("crcgrantid="):
            return "sdílení údajů"
        if rdn.startswith("crcrequestid="):
            return "žádost"
        if rdn.startswith("cn=readers-") and key.split(",", 1)[1] in self.dns:
            level = people.LEVELS.get(rdn.removeprefix("cn=readers-"), rdn)
            return f"{level} – {self.dns[key.split(',', 1)[1]]}"
        if rdn.startswith("crcqualificationid="):
            return f"kvalifikace {self.ids.get(rdn.split('=', 1)[1], '')}".strip()
        if key.endswith("cn=peercred,cn=external,cn=auth"):
            return "Správa serveru"
        return dn

    def value(self, attr: str, value: str) -> str:
        if attr == "userPassword":
            return "••••"
        if attr in {"member", "crcGrantTarget"}:
            return self.dn(value)
        if attr in {
            "crcParent",
            "crcQualificationRef",
            "crcGrantee",
            "crcApprovedBy",
            "crcDecidedBy",
            "crcAccessSubject",
        }:
            return self.ids.get(value, value)
        if attr == "crcMemberStatus":
            return people.STATUSES.get(value, value)
        if attr == "crcAccessLevel":
            return people.LEVELS.get(value, value)
        if attr == "crcRequestStatus":
            return approvals.STATUSES.get(value, value)
        if attr == "crcCanBeRp":
            return "ano" if value == "TRUE" else "ne"
        return value


def _parse_mod(mod: str) -> tuple[str, str, str]:
    """ "attr:+ value" → (attr, "+", value); "attr:" → (attr, "", "")."""
    attr, _, rest = mod.partition(":")
    if not rest:
        return attr, "", ""
    return attr, rest[0], rest[2:]


def _details(entry: d.Entry, labels: Labels) -> list[str]:
    lines = []
    if entry.first("reqType") == "modrdn":
        lines.append(f"do: {labels.dn(entry.first('reqNewSuperior'))}")
    for mod in entry.all("reqMod"):
        attr, op, value = _parse_mod(mod)
        if attr in HIDDEN:
            continue
        label = ATTR_LABELS.get(attr, attr)
        if entry.first("reqType") == "add":
            lines.append(f"{label}: {labels.value(attr, value)}")
        else:
            lines.append(f"{label} {OPS.get(op, op)}{': ' + labels.value(attr, value) if value else ''}")
    old = [o.split(": ", 1) for o in entry.all("reqOld")]
    changed = {_parse_mod(m)[0] for m in entry.all("reqMod")}
    for attr, value in (o for o in old if len(o) == 2):
        if attr in changed and attr not in HIDDEN and entry.first("reqType") == "modify":
            lines.append(f"{ATTR_LABELS.get(attr, attr)} předtím: {labels.value(attr, value)}")
    return lines


def changes(as_dn: str, person: people.Person | None = None) -> list[Change]:
    filterstr = "(&(reqResult=0)(|(reqType=add)(reqType=modify)(reqType=delete)(reqType=modrdn))"
    if person is not None:
        dn = escape_filter_chars(person.dn)
        filterstr += (
            f"(|(reqEntryUUID={escape_filter_chars(person.entry_uuid)})(reqDN:dnSubtreeMatch:={dn})"
            f"(reqMod=member:+ {dn})(reqMod=member:- {dn}))"
        )
    filterstr += ")"
    attrs = ["reqStart", "reqType", "reqDN", "reqAuthzID", "reqMod", "reqOld", "reqNewSuperior"]
    entries = d.search(ACCESSLOG_BASE, filterstr, attrs, as_dn=as_dn)
    entries.sort(key=lambda e: e.first("reqStart"), reverse=True)
    labels = Labels(as_dn)
    return [
        Change(
            when=datetime.strptime(e.first("reqStart")[:14], "%Y%m%d%H%M%S")
            .replace(tzinfo=UTC)
            .astimezone(people.PRAGUE),
            actor=labels.dn(e.first("reqAuthzID")),
            action=TYPES.get(e.first("reqType"), e.first("reqType")),
            target=labels.dn(e.first("reqDN")),
            details=_details(e, labels),
        )
        for e in entries[:LIMIT]
    ]
