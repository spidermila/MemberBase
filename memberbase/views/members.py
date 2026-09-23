"""Členové: list, detail, create, edit, status, move, roles, qualifications,
invitations and second-factor reset."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from memberbase import forms, history, keycloak, mail, people
from memberbase.auth import login_required, me, recent_login, require, step_up
from memberbase.directory import Conflict, StaleEntry

bp = Blueprint("members", __name__, url_prefix="/members")

STALE = "Záznam mezitím změnil někdo jiný. Zkontrolujte údaje a akci opakujte."
EMAIL_TAKEN = "Tento e-mail už používá jiná osoba."
KEYCLOAK_FAILED = "Změna je uložena, ale přihlašovací služba neodpověděla: {}. Zkuste to prosím znovu."

# action → (allowed from, new status, flash message)
STATUS_ACTIONS = {
    "activate": ({"inactive"}, "active", "Osoba je aktivní."),
    "deactivate": ({"active", "invited"}, "inactive", "Osoba je deaktivována."),
    "archive": ({"invited", "active", "inactive"}, "former", "Osoba je archivována."),
    "restore": ({"former"}, "inactive", "Osoba je obnovena jako neaktivní."),
}


def _person_or_404(member_id: str) -> people.Person:
    person = people.find_person(member_id, me().dn)
    if person is None:
        abort(404)
    return person


def _back(person: people.Person) -> Response:
    return redirect(url_for("members.detail", member_id=person.id))


@bp.route("/")
@login_required
def index() -> str:
    units = people.list_units(me().dn, include_external=True)
    unit = next((u for u in units if u.id == request.args.get("unit")), None)
    archived = request.args.get("archived") == "1"
    statuses = ["former"] if archived else ["invited", "active", "inactive"]
    found = people.search_people(me().dn, request.args.get("q", "").strip(), unit, statuses)
    can_see_roles = me().can("member.view_all")
    roles = people.list_roles(me().dn, with_members=can_see_roles)
    role_filter = request.args.get("role", "")
    if can_see_roles:
        for person in found:
            person.roles = {r.key for r in roles if person.dn.lower() in r.members}
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
@require("member.edit")
def create() -> str | Response:
    units = people.list_units(me().dn, include_external=True)
    form = request.form
    if request.method == "POST":
        name, e1 = forms.clean_name(form.get("name", ""))
        email, e2 = forms.clean_email(form.get("email", ""))
        phone, e3 = forms.clean_phone(form.get("phone", ""))
        unit = next((u for u in units if u.id == form.get("unit")), None)
        errors = [e for e in (e1, e2, e3) if e] + ([] if unit else ["Vyberte místní skupinu."])
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
    ctx: dict = {"person": person, "status_actions": STATUS_ACTIONS}
    if me().can("role.assign"):
        ctx["roles"] = people.list_roles(me().dn, with_members=True)
        person.roles = {r.key for r in ctx["roles"] if person.dn.lower() in r.members}
    ctx["quals"] = people.list_qualifications(me().dn)
    ctx["held"] = set(people.holdings_of(person, me().dn))
    if me().can("member.move"):
        ctx["units"] = [u for u in people.list_units(me().dn, include_external=True) if u.dn != person.unit_dn]
    return render_template("members/detail.html", **ctx)


def _notify_email_change(person: people.Person, new_email: str) -> None:
    mail.send(
        person.email,
        "Změna e-mailu v Evidenci členů",
        f"Dobrý den,\n\nváš přihlašovací e-mail v Evidenci členů byl změněn na {new_email}.\n"
        "Pokud o změně nevíte, kontaktujte ihned správce oblastního spolku.\n",
    )


@bp.route("/<member_id>/edit", methods=["POST"])
@require("member.edit")
def edit(member_id: str) -> Response:
    person = _person_or_404(member_id)
    name, e1 = forms.clean_name(request.form.get("name", ""))
    email, e2 = forms.clean_email(request.form.get("email", ""))
    phone, e3 = forms.clean_phone(request.form.get("phone", ""))
    errors = [e for e in (e1, e2, e3) if e]
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
    else:
        flash("Údaje uloženy.", "success")
        if email_changed:
            _notify_email_change(person, email)
    return _back(person)


@bp.route("/<member_id>/status/<action>", methods=["POST"])
@require("member.status")
def change_status(member_id: str, action: str) -> Response:
    if action not in STATUS_ACTIONS:
        abort(404)
    person = _person_or_404(member_id)
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
    flash(f"Role uloženy (přidáno {len(added)}, odebráno {len(removed)}).", "success")
    return _back(person)


@bp.route("/<member_id>/qualifications", methods=["POST"])
@require("qualification.manage")
def qualifications(member_id: str) -> Response:
    person = _person_or_404(member_id)
    added, removed = people.set_holdings(person, set(request.form.getlist("quals")), me().dn)
    flash(f"Kvalifikace uloženy (přidáno {len(added)}, odebráno {len(removed)}).", "success")
    return _back(person)


def _send_invite(person: people.Person) -> None:
    try:
        keycloak.send_invite(person.email, url_for("main.index", _external=True))
    except keycloak.KeycloakError as exc:
        flash(f"Pozvánku se nepodařilo odeslat: {exc}.", "danger")
    else:
        flash(f"Pozvánka odeslána na {person.email}.", "success")


@bp.route("/<member_id>/invite", methods=["POST"])
@require("member.edit")
def invite(member_id: str) -> Response:
    person = _person_or_404(member_id)
    if person.status != "invited":
        flash("Pozvánku lze poslat jen pozvané osobě.", "warning")
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
    for member_id in ids:
        person = people.find_person(member_id, me().dn)
        if person is None or (action == "add" and person.status == "former"):
            continue
        if (person.dn.lower() in role.members) == (action == "remove"):
            people.toggle_role(person, role, action == "add", me().dn)
            changed += 1
    verb = "přidána" if action == "add" else "odebrána"
    flash(f"Role „{role.label}“ {verb} u {changed} osob, {len(ids) - changed} beze změny.", "success")
    return redirect(url_for("members.index"))


@bp.route("/invites")
@require("member.edit")
def invites() -> str:
    return render_template("members/invites.html", people=people.search_people(me().dn, statuses=["invited"]))


@bp.route("/<member_id>/cancel-invite", methods=["POST"])
@require("member.edit")
def cancel_invite(member_id: str) -> Response:
    person = _person_or_404(member_id)
    if person.status == "invited":
        people.set_status(person, "former", me().dn)
        flash(f"Pozvánka pro {person.name} je zrušena, osoba je archivována.", "success")
    return redirect(url_for("members.invites"))
