"""Requests („Žádosti“): someone asks, the Chairs of the Místní skupina the
request is filed under decide (Admins may decide any). Two kinds:

- move: a Chair asks to move one of their people into another Místní skupina;
  it is filed under the destination.
- access: someone asks to see named people of a Místní skupina; the Chair
  matches the names to their people and approves some, all or none.

Filing and deciding run as the person. Carrying out an approved request runs
as MemberBase's service account: no Chair may write both ends of a move, nor
grants. It re-checks what the decision relied on before it acts, so an
approved request that no longer holds ends as „failed“. The requester is the
person named by crcRequestedByDn, which the access rules tie to whoever filed
the request; the other fields are only what the filer claims.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import ldap

from memberbase import directory as d
from memberbase import people
from memberbase.auth import Me
from memberbase.directory import escape_filter_chars

log = logging.getLogger(__name__)

TYPES = {"move": "Přesun do místní skupiny", "access": "Přístup k údajům"}
STATUSES = {
    "pending": "Čeká na rozhodnutí",
    "approved": "Schválena",
    "rejected": "Zamítnuta",
    "done": "Vyřízena",
    "failed": "Nepodařilo se provést",
}
ATTRS = [
    "crcRequestId",
    "crcRequestType",
    "crcRequestStatus",
    "crcRequestedByDn",
    "crcRequestNotify",
    "crcDecidedAt",
    "description",
    "crcMoveSubject",
    "crcMoveSubjectName",
    "crcMoveFromUnit",
    "crcAccessName",
    "crcAccessSubject",
    "crcAccessLevel",
    "crcExpiresAt",
    "createTimestamp",
]


@dataclass
class Request:
    dn: str
    id: str
    type: str
    status: str
    unit: people.Unit  # filed under; its Chairs decide
    requested_by_dn: str
    requested_by_name: str  # looked up, never taken from the request
    note: str
    csn: str
    created_at: datetime | None
    decided_at: datetime | None = None
    notify: list[str] = field(default_factory=list)
    move_subject: str = ""
    move_subject_name: str = ""
    move_from: people.Unit | None = None
    access_names: list[str] = field(default_factory=list)
    access_subjects: list[str] = field(default_factory=list)
    access_level: str = ""
    expires_at: datetime | None = None

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)

    @property
    def type_label(self) -> str:
        return TYPES.get(self.type, self.type)


def list_requests(as_dn: str, filterstr: str = "") -> list[Request]:
    """Requests the person may read, newest first. Requesters' names are read
    by the service account: the decider may not see the requester, and a name
    stored in the request would be only what the filer claims."""
    units = {u.dn.lower(): u for u in people.list_units(as_dn)}
    by_id = {u.id: u for u in units.values()}
    found = d.search(people.units_base(), f"(&(objectClass=crcRequest){filterstr})", ATTRS, as_dn=as_dn)
    names = {}
    for dn in {e.first("crcRequestedByDn") for e in found}:
        entry = d.get(dn, ["cn"])
        names[dn] = entry.first("cn") if entry else "—"
    reqs = []
    for e in found:
        reqs.append(
            Request(
                dn=e.dn,
                id=e.first("crcRequestId"),
                type=e.first("crcRequestType"),
                status=e.first("crcRequestStatus"),
                unit=units[e.dn.split(",", 2)[2].lower()],
                requested_by_dn=e.first("crcRequestedByDn"),
                requested_by_name=names[e.first("crcRequestedByDn")],
                note=e.first("description"),
                csn=e.first("entryCSN"),
                created_at=people.parse_ldap_time(e.first("createTimestamp")),
                decided_at=people.parse_ldap_time(e.first("crcDecidedAt")),
                notify=e.all("crcRequestNotify"),
                move_subject=e.first("crcMoveSubject"),
                move_subject_name=e.first("crcMoveSubjectName"),
                move_from=by_id.get(e.first("crcMoveFromUnit")),
                access_names=sorted(e.all("crcAccessName"), key=people.sort_key),
                access_subjects=e.all("crcAccessSubject"),
                access_level=e.first("crcAccessLevel"),
                expires_at=people.parse_ldap_time(e.first("crcExpiresAt")),
            )
        )
    reqs.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return reqs


def get_request(request_id: str, as_dn: str) -> Request | None:
    found = list_requests(as_dn, f"(crcRequestId={escape_filter_chars(request_id)})")
    return found[0] if found else None


def _file(unit: people.Unit, requester: people.Person, attrs: dict[str, list[str]], as_dn: str) -> str:
    request_id = people.new_id()
    d.add(
        f"crcRequestId={request_id},{unit.requests_dn}",
        {
            "crcRequestId": [request_id],
            "crcRequestedByDn": [requester.dn],
            "crcRequestStatus": ["pending"],
        }
        | attrs,
        as_dn,
    )
    return request_id


def file_move(subject: people.Person, target: people.Unit, note: str, requester: people.Person, as_dn: str) -> str:
    """Ask the Chairs of `target` to take `subject` in. The other Chairs of
    the subject's Místní skupina may read the request too."""
    assert subject.unit is not None
    notify = people.chair_members(subject.unit, as_dn) - {requester.dn.lower()}
    return _file(
        target,
        requester,
        {
            "objectClass": ["crcRequest", "crcMoveRequest"],
            "crcRequestType": ["move"],
            "crcRequestNotify": sorted(notify),
            "crcMoveSubject": [subject.id],
            "crcMoveSubjectName": [subject.name],
            "crcMoveFromUnit": [subject.unit.id],
            "description": [note] if note else [],
        },
        as_dn,
    )


def file_access(
    names: dict[str, list[str]],
    units: list[people.Unit],
    level: str,
    expires_at: datetime | None,
    note: str,
    requester: people.Person,
    as_dn: str,
) -> list[str]:
    """One request per Místní skupina (`names`: unit id → names typed).
    Returns the request ids."""
    return [
        _file(
            unit,
            requester,
            {
                "objectClass": ["crcRequest", "crcAccessRequest"],
                "crcRequestType": ["access"],
                "crcAccessName": names[unit.id],
                "crcAccessLevel": [level],
                "crcExpiresAt": [people.ldap_time(expires_at)] if expires_at else [],
                "description": [note],
            },
            as_dn,
        )
        for unit in units
        if unit.id in names
    ]


def _norm(text: str) -> str:
    return " ".join(people.sort_key(text).split())


def suggest(name: str, members: list[people.Person]) -> tuple[people.Person | None, list[people.Person]]:
    """The one member whose name equals `name` ignoring case and accents (if
    exactly one does), and the members sharing at least one word with it."""
    exact = [p for p in members if _norm(p.name) == _norm(name)]
    words = set(_norm(name).split())
    similar = [p for p in members if words & set(_norm(p.name).split())]
    return (exact[0] if len(exact) == 1 else None), similar


def decide(req: Request, approve: bool, me: Me, subjects: list[people.Person]) -> str | None:
    """Record the decision (as the decider, under the entryCSN the page was
    built from), then carry an approval out. Returns why it could not be
    carried out, or None."""
    changes = {"crcDecidedBy": [me.person.id], "crcDecidedAt": [people.now_ldap()]}
    if approve and req.type == "access":
        changes["crcAccessSubject"] = sorted(p.id for p in subjects)
    status = "approved" if approve else "rejected"
    d.swap(req.dn, "crcRequestStatus", "pending", status, me.dn, changes, csn=req.csn)
    if not approve:
        return None
    problem: str | None = "Adresář změnu odmítl."
    try:
        problem = _move(req, me) if req.type == "move" else _grant(req, me, subjects)
    except d.Denied, d.StaleEntry, d.Conflict, ldap.LDAPError:
        log.exception("carrying out request %s failed", req.id)
    finally:
        d.swap(req.dn, "crcRequestStatus", "approved", "failed" if problem else "done", me.dn)
    return problem


def _requester(req: Request) -> people.Person | None:
    """The active requester, found by crcRequestedByDn: the access rules tie
    that to whoever filed the request. Its other fields are the filer's say."""
    person = people.person_at(req.requested_by_dn, None)
    return person if person is not None and person.status == "active" else None


def _move(req: Request, me: Me) -> str | None:
    subject = people.find_person(req.move_subject, None)
    if (
        subject is None
        or subject.status == "former"
        or req.move_from is None
        or subject.unit_dn.lower() != req.move_from.dn.lower()
    ):
        return "Osoba mezitím změnila místní skupinu nebo byla archivována."
    if subject.name != req.move_subject_name:
        return "Jméno osoby se od podání žádosti změnilo. Podejte žádost znovu."
    requester = _requester(req)
    if requester is None:
        return "Žadatel už není aktivní."
    roles, chairs = people.memberships(requester.dn)
    by_admin = people.ADMIN_ROLE in roles
    if not by_admin and subject.unit_dn.lower() not in chairs:
        return "Žadatel není předsedou místní skupiny, ze které se osoba přesouvá."
    if not (by_admin or me.is_admin) and people.is_privileged(subject):
        return "Osobu s rozšířeným oprávněním nebo předsedu místní skupiny smí přesunout jen Admin."
    people.move_person(subject, req.unit, None, None)
    return None


def _grant(req: Request, me: Me, subjects: list[people.Person]) -> str | None:
    requester = _requester(req)
    if requester is None:
        return "Žadatel už není aktivní."
    roles, chairs = people.memberships(requester.dn)
    if not (people.ADMIN_ROLE in roles or chairs):
        return "Žadatel už není předsedou místní skupiny ani Adminem."
    if req.unit.dn.lower() in chairs or requester.id == me.person.id:
        return "O přístupu nemůže rozhodnout sám žadatel."
    if req.access_level not in people.LEVELS:
        return "Žádost má neplatný rozsah údajů."
    if req.expires_at is not None and req.expires_at <= datetime.now(UTC):
        return "Požadovaná platnost už uplynula."
    for person in subjects:
        people.create_grant(requester.id, person, req.access_level, req.expires_at, req.note, me.person.id, None)
    return None


def approver_emails(unit: people.Unit) -> list[str]:
    """Where to announce a new request: the Místní skupina's active Chairs,
    or the Admins if it has none. Read by the service account, because the
    requester may not see them; the addresses are never shown."""
    admins = next(r.members for r in people.list_roles(None, with_members=True) if r.key == people.ADMIN_ROLE)
    return _active_mails(people.chair_members(unit, None)) or _active_mails(admins)


def _active_mails(dns: set[str] | list[str]) -> list[str]:
    found = [d.get(dn, ["mail", "crcMemberStatus"]) for dn in dns]
    return sorted({e.first("mail") for e in found if e and e.first("crcMemberStatus") == "active"})


def party_emails(req: Request) -> list[str]:
    """Where to announce a decision: the requester and, for a move, the
    notified people who still chair the Místní skupina the person left
    (service account, for the same reason; the requester could add others)."""
    notify: set[str] = set()
    if req.move_from is not None:
        notify = {dn.lower() for dn in req.notify} & people.chair_members(req.move_from, None)
    return _active_mails([req.requested_by_dn, *notify])
