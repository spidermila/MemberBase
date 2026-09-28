"""Žádosti: filing, listing and deciding requests (see memberbase.approvals)."""

from collections import defaultdict
from datetime import UTC, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from memberbase import approvals, forms, mail, people
from memberbase.auth import login_required, me
from memberbase.directory import StaleEntry, escape_filter_chars

bp = Blueprint("requests", __name__, url_prefix="/requests")

MAX_NAMES = 50
NAME_ROWS = 5
# Filed on the „Nová žádost“ page (permissions.REQUEST_PERMISSIONS says who may):
# type → its form and what it is for.
NEW_TYPES = {
    "access": (
        "requests.new_access",
        "Údaje jmenovaných lidí z jiných místních skupin. Předseda jejich místní skupiny rozhodne, komu je zpřístupní.",
    ),
    "certificates": (
        "requests.new_certificates",
        "Osvědčení lidí celé místní skupiny nebo jen jmenovaných. "
        "Předseda je doplní a po vyřízení uvidíte jejich přehled.",
    ),
}


def _decides(req: approvals.Request) -> bool:
    return me().is_admin or me().is_chair_of(req.unit.dn)


def _request_or_404(request_id: str) -> approvals.Request:
    req = approvals.get_request(request_id, me().dn)
    if req is None:
        abort(404)
    return req


def _announce(req_ids: list[str]) -> None:
    for req_id in req_ids:
        req = approvals.get_request(req_id, me().dn)
        assert req is not None
        link = url_for("requests.detail", request_id=req.id, _external=True)
        for to in approvals.approver_emails(req.unit):
            mail.send(
                to,
                f"Nová žádost: {req.type_label}",
                f"Dobrý den,\n\n{req.requested_by_name} žádá o {req.wants}.\n"
                f"Rozhodnout můžete v Evidenci členů: {link}\n",
            )


@bp.route("/")
@login_required
def index() -> str:
    reqs = approvals.list_requests(me().dn)
    to_decide = [r for r in reqs if r.pending and _decides(r)]
    shown = {r.id for r in to_decide}
    mine = [r for r in reqs if r.requested_by_dn.lower() == me().dn.lower() and r.id not in shown]
    shown |= {r.id for r in mine}
    others = [r for r in reqs if r.id not in shown]
    return render_template("requests/index.html", to_decide=to_decide, mine=mine, others=others)


@bp.route("/new")
@login_required
def new() -> str:
    offered = [(approvals.TYPES[t], endpoint, text) for t, (endpoint, text) in NEW_TYPES.items() if me().may_file(t)]
    if not offered:
        abort(403)
    return render_template("requests/new.html", offered=offered)


@bp.route("/move/<member_id>", methods=["POST"])
@login_required
def new_move(member_id: str) -> Response:
    person = people.find_person(member_id, me().dn)
    if person is None or not me().is_chair_of(person.unit_dn) or person.status == "former":
        abort(403)
    target = next((u for u in people.list_units(me().dn) if u.id == request.form.get("unit")), None)
    pending = f"(crcMoveSubject={escape_filter_chars(person.id)})(crcRequestStatus=pending)"
    if target is None or target.dn == person.unit_dn:
        flash("Vyberte jinou místní skupinu.", "warning")
    elif people.is_privileged(person):
        flash("Osobu s rozšířeným oprávněním nebo předsedu místní skupiny přesouvá jen Admin.", "warning")
    elif approvals.list_requests(me().dn, pending):
        flash("Žádost o přesun této osoby už čeká na rozhodnutí.", "warning")
    else:
        note = forms.clean_note(request.form.get("note", ""))
        _announce([approvals.file_move(person, target, note, me().person, me().dn)])
        flash(f"Žádost o přesun do „{target.name}“ je odeslána k rozhodnutí.", "success")
    return redirect(url_for("members.detail", member_id=person.id))


def _may_file(request_type: str) -> None:
    if not me().may_file(request_type):
        abort(403)


def _names(units: list[people.Unit]) -> tuple[dict[str, list[str]], list[str]]:
    """The names typed in the form's name rows by unit id, and what is wrong with them."""
    errors = []
    names: dict[str, list[str]] = {}
    rows = list(zip(request.form.getlist("name"), request.form.getlist("unit")))
    if len(rows) > MAX_NAMES:
        errors.append(f"Najednou lze žádat nejvýše o {MAX_NAMES} jmen.")
    for raw, unit_id in rows:
        if not raw.strip():
            continue
        name, error = forms.clean_name(raw)
        if error:
            errors.append(error)
        elif unit_id not in {u.id for u in units}:
            errors.append(f"U jména „{name}“ vyberte místní skupinu.")
        elif name.casefold() not in {n.casefold() for n in names.get(unit_id, [])}:
            names.setdefault(unit_id, []).append(name)
    return names, errors


def _filed(req_ids: list[str]) -> Response:
    _announce(req_ids)
    flash(f"Odesláno žádostí: {len(req_ids)} (jedna za každou místní skupinu).", "success")
    return redirect(url_for("requests.index"))


def _form(template: str, units: list[people.Unit], errors: list[str], **ctx: object) -> str:
    for error in errors:
        flash(error, "danger")
    rows = max(NAME_ROWS, len(request.form.getlist("name")))
    return render_template(template, units=units, form=request.form, rows=rows, **ctx)


@bp.route("/access", methods=["GET", "POST"])
@login_required
def new_access() -> str | Response:
    _may_file("access")
    units = [u for u in people.list_units(me().dn) if u.dn.lower() != me().person.unit_dn.lower()]
    form = request.form
    errors: list[str] = []
    if request.method == "POST":
        names, errors = _names(units)
        level = form.get("level", "")
        expires_at, error = forms.clean_date(form.get("expires", ""))
        note = forms.clean_note(form.get("note", ""))
        if error:
            errors.append(error)
        elif expires_at is not None and expires_at <= datetime.now(UTC):
            errors.append("Platnost musí skončit v budoucnu.")
        if level not in people.LEVELS:
            errors.append("Vyberte rozsah údajů.")
        if not note:
            errors.append("Napište, k čemu údaje potřebujete.")
        if not names and not errors:
            errors.append("Napište aspoň jedno jméno.")
        if not errors:
            return _filed(approvals.file_access(names, units, level, expires_at, note, me().person, me().dn))
    return _form("requests/new_access.html", units, errors, levels=people.LEVELS)


@bp.route("/certificates", methods=["GET", "POST"])
@login_required
def new_certificates() -> str | Response:
    _may_file("certificates")
    # A Chair does not ask their own Místní skupina (the directory refuses it unless they are an Admin).
    units = [u for u in people.list_units(me().dn) if not me().is_chair_of(u.dn)]
    errors: list[str] = []
    if request.method == "POST":
        names, errors = _names(units)
        # A whole Místní skupina covers any names typed for it.
        names |= {u.id: [] for u in units if u.id in request.form.getlist("whole")}
        note = forms.clean_note(request.form.get("note", ""))
        if not note:
            errors.append("Napište, k čemu přehled potřebujete.")
        if not names and not errors:
            errors.append("Vyberte místní skupinu nebo napište aspoň jedno jméno.")
        if not errors:
            return _filed(approvals.file_certificates(names, units, note, me().person, me().dn))
    return _form("requests/new_certificates.html", units, errors)


def _members(req: approvals.Request) -> list[people.Person]:
    return people.search_people(me().dn, unit=req.unit, statuses=people.CURRENT_STATUSES)


def _report(
    req: approvals.Request, members: list[people.Person]
) -> list[tuple[people.Person, list[people.Certificate]]]:
    """The `members` a done certificate request covers, each with the
    certificates the viewer may read."""
    certs = defaultdict(list)
    for cert in people.list_certificates(me().dn, req.unit.dn):
        certs[cert.person_dn.lower()].append(cert)
    covered = [p for p in members if not req.named or p.id in req.access_subjects]
    return [(p, certs[p.dn.lower()]) for p in covered]


@bp.route("/<request_id>")
@login_required
def detail(request_id: str) -> str:
    req = _request_or_404(request_id)
    ctx: dict = {"req": req, "levels": people.LEVELS, "decides": req.pending and _decides(req)}
    report = req.type == "certificates" and req.status == "done"
    members = _members(req) if req.named or report else []
    if req.named:
        ctx["members"] = members
        ctx["rows"] = [(name, *approvals.suggest(name, members)) for name in req.access_names]
        ctx["granted"] = [p for p in members if p.id in req.access_subjects]
    if report:
        ctx["report"] = _report(req, members)
    return render_template("requests/detail.html", **ctx)


@bp.route("/<request_id>/decide", methods=["POST"])
@login_required
def decide(request_id: str) -> Response:
    req = _request_or_404(request_id)
    if not _decides(req):
        abort(403)
    back = redirect(url_for("requests.detail", request_id=req.id))
    if not req.pending:
        flash("O žádosti už je rozhodnuto.", "warning")
        return back
    action = request.form.get("action")
    if action not in {"approve", "reject"}:
        abort(400)
    approve = action == "approve"
    subjects: list[people.Person] = []
    if approve and req.named:
        chosen = {request.form.get(f"subject_{i}", "") for i in range(len(req.access_names))}
        subjects = [p for p in _members(req) if p.id in chosen]
        if not subjects:
            flash("Vyberte aspoň jednu osobu, nebo žádost zamítněte.", "warning")
            return back
    req.csn = request.form.get("csn", "")
    try:
        problem = approvals.decide(req, approve, me(), subjects)
    except StaleEntry:
        flash("Žádost mezitím změnil někdo jiný. Zkontrolujte ji a rozhodněte znovu.", "warning")
        return back
    if problem:
        flash(f"Žádost je schválena, ale nepodařilo se ji provést: {problem}", "danger")
    else:
        flash("Žádost je vyřízena." if approve else "Žádost je zamítnuta.", "success")
    decided = approvals.get_request(req.id, me().dn)
    assert decided is not None
    link = url_for("requests.detail", request_id=req.id, _external=True)
    outcome = {
        "done": "byla schválena a vyřízena.",
        "rejected": "byla zamítnuta.",
        "failed": f"byla schválena, ale nepodařilo se ji provést: {problem}",
    }[decided.status]
    for to in approvals.party_emails(decided):
        mail.send(
            to,
            f"Žádost: {decided.status_label}",
            f"Dobrý den,\n\nžádost „{decided.type_label}“ (místní skupina „{req.unit.name}“) {outcome}\n"
            f"Žadatel: {decided.requested_by_name}\nPodrobnosti: {link}\n",
        )
    return back
