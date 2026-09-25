"""Žádosti: filing, listing and deciding requests (see memberbase.approvals)."""

from datetime import UTC, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from memberbase import approvals, forms, mail, people
from memberbase.auth import login_required, me, require
from memberbase.directory import StaleEntry, escape_filter_chars

bp = Blueprint("requests", __name__, url_prefix="/requests")

MAX_NAMES = 50
NAME_ROWS = 5


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
        what = f"přesun osoby {req.move_subject_name}" if req.type == "move" else "přístup k údajům členů"
        for to in approvals.approver_emails(req.unit):
            mail.send(
                to,
                f"Nová žádost: {req.type_label}",
                f"Dobrý den,\n\n{req.requested_by_name} žádá o {what} (místní skupina „{req.unit.name}“).\n"
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


@bp.route("/access", methods=["GET", "POST"])
@require("request.create")
def new_access() -> str | Response:
    units = [u for u in people.list_units(me().dn) if u.dn.lower() != me().person.unit_dn.lower()]
    form = request.form
    if request.method == "POST":
        errors = []
        names: dict[str, list[str]] = {}
        rows = list(zip(form.getlist("name"), form.getlist("unit")))
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
            req_ids = approvals.file_access(names, units, level, expires_at, note, me().person, me().dn)
            _announce(req_ids)
            flash(f"Odesláno žádostí: {len(req_ids)} (jedna za každou místní skupinu).", "success")
            return redirect(url_for("requests.index"))
        for error in errors:
            flash(error, "danger")
    return render_template(
        "requests/new_access.html",
        units=units,
        levels=people.LEVELS,
        form=form,
        rows=max(NAME_ROWS, len(form.getlist("name"))),
    )


def _members(req: approvals.Request) -> list[people.Person]:
    return people.search_people(me().dn, unit=req.unit, statuses=people.CURRENT_STATUSES)


@bp.route("/<request_id>")
@login_required
def detail(request_id: str) -> str:
    req = _request_or_404(request_id)
    ctx: dict = {"req": req, "levels": people.LEVELS, "decides": req.pending and _decides(req)}
    if req.type == "access":
        members = _members(req)
        ctx["members"] = members
        ctx["rows"] = [(name, *approvals.suggest(name, members)) for name in req.access_names]
        ctx["granted"] = [p for p in members if p.id in req.access_subjects]
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
    if approve and req.type == "access":
        chosen = {request.form.get(f"subject_{i}", "") for i in range(len(req.access_names))}
        subjects = [p for p in _members(req) if p.id in chosen]
        if not subjects:
            flash("Nevybrali jste nikoho. Pokud nikomu přístup nedáte, žádost zamítněte.", "warning")
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
