"""Členové: list, detail, create, edit, status, move, roles, qualifications,
certificates, invitations and second-factor reset."""

import time
from collections.abc import Mapping

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

from memberbase import forms, history, keycloak, mail, people
from memberbase.auth import login_required, me, recent_login, require, step_up
from memberbase.directory import Conflict, Denied, StaleEntry

bp = Blueprint("members", __name__, url_prefix="/members")

INVITABLE = {"new", "invited"}
STALE = "Záznam mezitím změnil někdo jiný. Zkontrolujte údaje a akci opakujte."
EMAIL_TAKEN = "Tento e-mail už používá jiná osoba."
KEYCLOAK_FAILED = "Změna je uložena, ale přihlašovací služba neodpověděla: {}. Zkuste to prosím znovu."
PRIVILEGED = "Tuto osobu smí měnit jen Admin: má roli s rozšířeným oprávněním nebo je předsedou místní skupiny."

# action → (allowed from, new status, flash message)
STATUS_ACTIONS = {
    "activate": ({"inactive"}, "active", "Osoba je aktivní."),
    "deactivate": ({"active", "invited", "new"}, "inactive", "Osoba je deaktivována."),
    "archive": ({"new", "invited", "active", "inactive"}, "former", "Osoba je archivována."),
    "restore": ({"former"}, "inactive", "Osoba je obnovena jako neaktivní."),
}


def _person_or_404(member_id: str) -> people.Person:
    person = people.find_person(member_id, me().dn)
    if person is None:
        abort(404)
    return person


def _back(person: people.Person) -> Response:
    return redirect(url_for("members.detail", member_id=person.id))


def _clean_person(form: Mapping[str, str]) -> tuple[str, str, str, list[str]]:
    """Name, email and phone from a form, and what is wrong with them."""
    name, e1 = forms.clean_name(form.get("name", ""))
    email, e2 = forms.clean_email(form.get("email", ""))
    phone, e3 = forms.clean_phone(form.get("phone", ""))
    return name, email, phone, [e for e in (e1, e2, e3) if e]


def _protected(person: people.Person) -> bool:
    """Chairs are not offered what the directory refuses them (privileged people, themselves)."""
    return me().is_chair_of(person.unit_dn) and not me().is_admin and people.is_privileged(person)


def _managed_or_403(member_id: str, permission: str) -> people.Person:
    """The person, if the logged-in person may do `permission` to them: by
    role anywhere, or as Chair of their Místní skupina."""
    person = _person_or_404(member_id)
    if not me().can_in_unit(permission, person.unit_dn):
        abort(403)
    return person


@bp.route("/")
@login_required
def index() -> str:
    units = people.list_units(me().dn, include_external=True)
    unit = next((u for u in units if u.id == request.args.get("unit")), None)
    archived = request.args.get("archived") == "1"
    statuses = ["former"] if archived else people.CURRENT_STATUSES
    found = people.search_people(me().dn, request.args.get("q", "").strip(), unit, statuses, units=units)
    can_see_roles = me().can("member.view_all")
    roles = people.list_roles(me().dn, with_members=can_see_roles)
    role_filter = request.args.get("role", "")
    if can_see_roles:
        for person in found:
            person.roles = people.role_keys(person, roles)
        if role_filter:
            found = [p for p in found if role_filter in p.roles]
    return render_template(
        "members/index.html",
        people=found,
        units=units,
        unit=unit,
        roles=roles,
        role_filter=role_filter,
        archived=archived,
        can_see_roles=can_see_roles,
        q=request.args.get("q", ""),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    units = [u for u in people.list_units(me().dn, include_external=True) if me().can_in_unit("member.edit", u.dn)]
    if not units:
        abort(403)
    form = request.form
    if request.method == "POST":
        name, email, phone, errors = _clean_person(form)
        unit = next((u for u in units if u.id == form.get("unit")), None)
        if unit is None:
            errors.append("Vyberte místní skupinu.")
        if not errors and unit is not None:
            try:
                person = people.create_person(name, email, phone, unit, me().dn)
            except Conflict:
                errors.append(EMAIL_TAKEN)
            else:
                flash(f"Osoba {name} je vytvořena.", "success")
                if form.get("invite"):
                    _send_invite(person)
                return _back(person)
        for error in errors:
            flash(error, "danger")
    return render_template("members/create.html", units=units, form=form)


@bp.route("/<member_id>")
@login_required
def detail(member_id: str) -> str:
    person = _person_or_404(member_id)
    unit_dn = person.unit_dn
    protected = _protected(person)
    ctx: dict = {
        "person": person,
        "status_actions": STATUS_ACTIONS,
        "protected": protected,
        "can_edit": me().can_in_unit("member.edit", unit_dn) and not protected,
        "can_status": me().can_in_unit("member.status", unit_dn) and not protected,
        "can_quals": me().can_in_unit("qualification.manage", unit_dn) and not protected,
        "can_certs": me().can_in_unit("certificate.manage", unit_dn) and not protected,
        "certs": people.certificates_of(person, me().dn),
        "is_chair": people.is_chair(person, me().dn),
    }
    if me().can("role.assign"):
        ctx["roles"] = people.list_roles(me().dn, with_members=True)
        person.roles = people.role_keys(person, ctx["roles"])
    ctx["quals"] = people.list_qualifications(me().dn)
    ctx["held"] = set(people.holdings_of(person, me().dn))
    if me().can("member.move"):
        ctx["units"] = [u for u in people.list_units(me().dn, include_external=True) if u.dn != person.unit_dn]
    elif me().is_chair_of(unit_dn) and not protected:
        ctx["request_units"] = [u for u in people.list_units(me().dn) if u.dn != person.unit_dn]
    return render_template("members/detail.html", **ctx)


def _notify_email_change(person: people.Person, new_email: str) -> None:
    mail.send(
        person.email,
        "Změna e-mailu v Evidenci členů",
        f"Dobrý den,\n\nváš přihlašovací e-mail v Evidenci členů byl změněn na {new_email}.\n"
        "Pokud o změně nevíte, kontaktujte ihned správce oblastního spolku.\n",
    )


@bp.route("/<member_id>/edit", methods=["POST"])
@login_required
def edit(member_id: str) -> Response:
    person = _managed_or_403(member_id, "member.edit")
    name, email, phone, errors = _clean_person(request.form)
    if errors:
        for error in errors:
            flash(error, "danger")
        return _back(person)
    changes = {"name": name, "email": email, "phone": phone}
    email_changed = email != person.email
    if email_changed and (redirect_to := step_up()):
        return redirect_to
    try:
        people.update_person(person, changes, me().dn, request.form.get("csn", ""))
    except StaleEntry:
        flash(STALE, "warning")
    except Conflict:
        flash(EMAIL_TAKEN, "danger")
    except Denied:
        flash(PRIVILEGED, "warning")
    else:
        flash("Údaje uloženy.", "success")
        if email_changed:
            _notify_email_change(person, email)
    return _back(person)


@bp.route("/<member_id>/status/<action>", methods=["POST"])
@login_required
def change_status(member_id: str, action: str) -> Response:
    if action not in STATUS_ACTIONS:
        abort(404)
    person = _managed_or_403(member_id, "member.status")
    allowed, status, message = STATUS_ACTIONS[action]
    if person.status not in allowed:
        flash("Tuto změnu stavu nelze provést.", "warning")
        return _back(person)
    if person.id == me().person.id:
        flash("Svůj vlastní stav měnit nemůžete.", "warning")
        return _back(person)
    try:
        people.set_status(person, status, me().dn, request.form.get("csn", ""))
    except StaleEntry:
        flash(STALE, "warning")
        return _back(person)
    except Denied:
        flash(PRIVILEGED, "warning")
        return _back(person)
    if status == "former":
        people.remove_all_roles(person, me().dn)
    flash(message, "success")
    if person.status in {"active", "invited"} and status in {"inactive", "former"}:
        try:
            keycloak.logout(person.email)
        except keycloak.KeycloakError as exc:
            flash(KEYCLOAK_FAILED.format(exc), "warning")
    return _back(person)


@bp.route("/<member_id>/move", methods=["POST"])
@require("member.move")
def move(member_id: str) -> Response:
    person = _person_or_404(member_id)
    target = people.get_unit(request.form.get("unit", ""), me().dn)
    if target is None or target.dn == person.unit_dn:
        flash("Vyberte jinou místní skupinu.", "warning")
        return _back(person)
    try:
        people.move_person(person, target, me().dn, request.form.get("csn", ""))
    except StaleEntry:
        flash(STALE, "warning")
    else:
        flash(f"Osoba je přesunuta: {target.name}.", "success")
    return _back(person)


@bp.route("/<member_id>/roles", methods=["POST"])
@require("role.assign")
@recent_login
def roles(member_id: str) -> Response:
    person = _person_or_404(member_id)
    if person.status == "former":
        flash("Archivované osobě nelze přiřadit role.", "warning")
        return _back(person)
    added, removed = people.set_roles(person, set(request.form.getlist("roles")), me().dn)
    if person.unit is not None and not person.unit.is_external:
        is_chair = people.is_chair(person, me().dn)
        if is_chair != bool(request.form.get("chair")):
            people.set_chair(person, not is_chair, me().dn)
            (removed if is_chair else added).add("chair")
    flash(f"Role uloženy (přidáno {len(added)}, odebráno {len(removed)}).", "success")
    return _back(person)


@bp.route("/<member_id>/qualifications", methods=["POST"])
@login_required
def qualifications(member_id: str) -> Response:
    person = _managed_or_403(member_id, "qualification.manage")
    try:
        added, removed = people.set_holdings(person, set(request.form.getlist("quals")), me().dn)
    except Denied:
        flash(PRIVILEGED, "warning")
    else:
        flash(f"Kvalifikace uloženy (přidáno {len(added)}, odebráno {len(removed)}).", "success")
    return _back(person)


# ── Osvědčení ────────────────────────────────────────────────────────────────

CERT_STATES = {"valid": "Platné", "expiring": "Brzy vyprší", "expired": "Prošlé"}


@bp.route("/certificates")
@login_required
def certificates() -> str:
    """Every certificate the logged-in person may read, filterable."""
    units = people.list_units(me().dn, include_external=True)
    unit = next((u for u in units if u.id == request.args.get("unit")), None)
    found = people.search_people(me().dn, unit=unit, statuses=people.CURRENT_STATUSES, units=units)
    owners = {p.dn.lower(): p for p in found}
    q = people.sort_key(request.args.get("q", "").strip())
    state = request.args.get("state", "")
    found_certs = []
    for cert in people.list_certificates(me().dn, unit.dn if unit else None):
        owner = owners.get(cert.person_dn.lower())
        if owner is None or (state and cert.state != state):
            continue
        if q and q not in people.sort_key(f"{owner.name} {cert.name} {cert.issuer}"):
            continue
        found_certs.append((owner, cert))
    shown = {owner.dn: owner for owner, _ in found_certs}.values()
    editable = {o.dn for o in shown if me().can_in_unit("certificate.manage", o.unit_dn) and not _protected(o)}
    rows = [(owner, cert, owner.dn in editable) for owner, cert in found_certs]
    rows.sort(key=lambda row: people.sort_key(row[0].name))
    return render_template(
        "members/certificates.html",
        rows=rows,
        units=units,
        unit=unit,
        state=state,
        states=CERT_STATES,
        q=request.args.get("q", ""),
    )


def _certificate_or_404(person: people.Person, cert_id: str) -> people.Certificate:
    cert = next((c for c in people.certificates_of(person, me().dn) if c.id == cert_id), None)
    if cert is None:
        abort(404)
    return cert


def _cert_back(person: people.Person) -> Response:
    if request.values.get("back") == "certificates":
        return redirect(url_for("members.certificates"))
    return _back(person)


@bp.route("/<member_id>/certificates/new", methods=["GET", "POST"])
@bp.route("/<member_id>/certificates/<cert_id>", methods=["GET", "POST"])
@login_required
def certificate(member_id: str, cert_id: str | None = None) -> str | Response:
    person = _managed_or_403(member_id, "certificate.manage")
    if _protected(person):
        flash(PRIVILEGED, "warning")
        return _back(person)
    cert = _certificate_or_404(person, cert_id) if cert_id else None
    form = request.form
    if request.method == "POST":
        name, issuer = forms.clean_note(form.get("name", "")), forms.clean_note(form.get("issuer", ""))
        issued, e1 = forms.clean_day(form.get("issued", ""))
        expires, e2 = forms.clean_day(form.get("expires", ""))
        errors = [e for e in (e1, e2) if e]
        if not name or not issuer or (issued is None and not e1):
            errors.append("Vyplňte název, kdo osvědčení vydal, a datum vydání.")
        if issued and expires and expires < issued:
            errors.append("Platnost nemůže skončit před datem vydání.")
        if not errors:
            assert issued is not None
            try:
                people.save_certificate(person, cert, name, issuer, issued, expires, me().dn, form.get("csn", ""))
            except StaleEntry:
                # Reload, so the next save cannot overwrite the other change unseen.
                flash(STALE, "warning")
                back = form.get("back") or None
                return redirect(url_for("members.certificate", member_id=person.id, cert_id=cert_id, back=back))
            flash(f"Osvědčení „{name}“ je uloženo.", "success")
            return _cert_back(person)
        for e in errors:
            flash(e, "danger")
    return render_template("members/certificate.html", person=person, cert=cert, form=form)


@bp.route("/<member_id>/certificates/<cert_id>/delete", methods=["POST"])
@login_required
def delete_certificate(member_id: str, cert_id: str) -> Response:
    person = _managed_or_403(member_id, "certificate.manage")
    if _protected(person):
        flash(PRIVILEGED, "warning")
        return _back(person)
    cert = _certificate_or_404(person, cert_id)
    people.delete_certificate(cert, me().dn)
    flash(f"Osvědčení „{cert.name}“ je smazáno.", "success")
    return _cert_back(person)


def _invite(person: people.Person) -> str | None:
    """Email the set-password link; a new person becomes invited first, since
    Keycloak finds only invited and active people. Returns the error, if any."""
    was_new = person.status == "new"
    if was_new:
        people.set_status(person, "invited", me().dn)
    try:
        keycloak.send_invite(person.email, url_for("main.index", _external=True))
    except keycloak.KeycloakError as exc:
        if was_new:
            people.set_status(person, "new", me().dn)
        return str(exc)
    return None


def _send_invite(person: people.Person) -> None:
    try:
        error = _invite(person)
    except Denied:
        error = PRIVILEGED
    if error:
        flash(f"Pozvánku se nepodařilo odeslat: {error}.", "danger")
    else:
        flash(f"Pozvánka odeslána na {person.email}.", "success")


@bp.route("/<member_id>/invite", methods=["POST"])
@login_required
def invite(member_id: str) -> Response:
    person = _managed_or_403(member_id, "member.edit")
    if person.status not in INVITABLE:
        flash("Pozvánku lze poslat jen osobě, která se ještě nepřihlásila.", "warning")
    else:
        _send_invite(person)
    if request.form.get("back") == "invites":
        return redirect(url_for("members.invites"))
    return _back(person)


@bp.route("/<member_id>/mfa-reset", methods=["POST"])
@require("mfa.reset")
@recent_login
def mfa_reset(member_id: str) -> Response:
    person = _person_or_404(member_id)
    try:
        removed = keycloak.reset_mfa(person.email)
    except keycloak.KeycloakError as exc:
        flash(f"Dvoufázové ověření se nepodařilo resetovat: {exc}.", "danger")
        return _back(person)
    flash(f"Dvoufázové ověření resetováno (odebráno {removed} ověřovacích prostředků).", "success")
    mail.send(
        person.email,
        "Reset dvoufázového ověření",
        "Dobrý den,\n\nsprávce Evidence členů odebral vaše prostředky dvoufázového ověření.\n"
        "Při příštím přihlášení si je můžete nastavit znovu.\n"
        "Pokud jste o reset nežádali, kontaktujte ihned správce oblastního spolku.\n",
    )
    return _back(person)


@bp.route("/<member_id>/login-as", methods=["POST"])
@login_required
@recent_login
def login_as(member_id: str) -> Response:
    """Dev-only: switch the admin's own session to another active person,
    without going through Keycloak. Never reachable outside --debug."""
    if not current_app.debug or not me().is_admin:
        abort(404)
    person = _person_or_404(member_id)
    if person.status != "active" or person.id == me().person.id:
        abort(404)
    session.clear()
    session.permanent = True
    session["member_id"] = person.id
    session["auth_time"] = time.time()
    session["id_token"] = ""
    flash(f"Přihlášen jako „{person.name}“ (vývojářský nástroj, jen v DEV).", "warning")
    return redirect(url_for("main.index"))


@bp.route("/<member_id>/history")
@require("history.view")
def person_history(member_id: str) -> str:
    person = _person_or_404(member_id)
    return render_template("members/history.html", person=person, changes=history.changes(me().dn, person))


@bp.route("/batch", methods=["POST"])
@require("role.assign")
@recent_login
def batch() -> Response:
    ids = request.form.getlist("member_ids")
    role_key = request.form.get("role", "")
    action = request.form.get("action", "")
    role = next((r for r in people.list_roles(me().dn, with_members=True) if r.key == role_key), None)
    if not ids or role is None or action not in {"add", "remove"}:
        flash("Vyberte osoby, roli a akci.", "warning")
        return redirect(url_for("members.index"))
    changed = 0
    for person in people.search_people(me().dn, ids=ids):
        if action == "add" and person.status == "former":
            continue
        if (person.dn.lower() in role.members) == (action == "remove"):
            people.toggle_role(person, role, action == "add", me().dn)
            changed += 1
    verb = "přidána" if action == "add" else "odebrána"
    flash(f"Role „{role.label}“ {verb} u {changed} osob, {len(ids) - changed} beze změny.", "success")
    return redirect(url_for("members.index"))


@bp.route("/batch-move", methods=["POST"])
@require("member.move")
def batch_move() -> Response:
    ids = request.form.getlist("member_ids")
    target = people.get_unit(request.form.get("target", ""), me().dn)
    if not ids or target is None:
        flash("Vyberte osoby a cílovou místní skupinu.", "warning")
        return redirect(url_for("members.index"))
    moved, stale = 0, 0
    for person in people.search_people(me().dn, ids=ids):
        if person.unit_dn.lower() == target.dn.lower():
            continue
        try:
            people.move_person(person, target, me().dn, person.csn)
        except StaleEntry:
            stale += 1
        else:
            moved += 1
    flash(f"Přesunuto do „{target.name}“: {moved} osob, {len(ids) - moved - stale} beze změny.", "success")
    if stale:
        flash(f"{stale} osob mezitím změnil někdo jiný a nebyly přesunuty. Zkontrolujte je a akci opakujte.", "warning")
    return redirect(url_for("members.index"))


def _invitable() -> list[people.Person]:
    found = people.search_people(me().dn, statuses=sorted(INVITABLE))
    mine = [p for p in found if me().can_in_unit("member.edit", p.unit_dn)]
    return mine if me().is_admin else [p for p in mine if not people.is_privileged(p)]


@bp.route("/invites")
@login_required
def invites() -> str:
    if not me().manages_people:
        abort(403)
    return render_template("members/invites.html", people=_invitable())


@bp.route("/invites/send", methods=["POST"])
@login_required
def send_invites() -> Response:
    if not me().manages_people:
        abort(403)
    # ponytail: one Keycloak round trip per person, synchronous; fine for tens, a job if it reaches hundreds
    sent, failed = 0, []
    for person in people.search_people(me().dn, statuses=sorted(INVITABLE), ids=request.form.getlist("member_ids")):
        if not me().can_in_unit("member.edit", person.unit_dn):
            continue
        try:
            error = _invite(person)
        except Denied:
            error = "smí jen Admin"
        if error:
            failed.append(f"{person.email} ({error})")
        else:
            sent += 1
    flash(f"Odesláno pozvánek: {sent}.", "success" if sent else "warning")
    if failed:
        flash("Pozvánku se nepodařilo odeslat: " + ", ".join(failed) + ".", "danger")
    return redirect(url_for("members.invites"))


@bp.route("/<member_id>/cancel-invite", methods=["POST"])
@login_required
def cancel_invite(member_id: str) -> Response:
    person = _managed_or_403(member_id, "member.edit")
    if person.status in INVITABLE:
        try:
            people.set_status(person, "former", me().dn)
        except Denied:
            flash(PRIVILEGED, "warning")
        else:
            flash(f"Pozvánka pro {person.name} je zrušena, osoba je archivována.", "success")
    return redirect(url_for("members.invites"))
