"""Místní skupiny, Kvalifikace, Sdílení údajů, Historie změn, Oprávnění rolí."""

import requests
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from memberbase import forms, history, keycloak, people
from memberbase.auth import me, require
from memberbase.directory import StaleEntry
from memberbase.permissions import PERMISSION_LABELS, ROLE_LABELS, ROLE_PERMISSIONS

bp = Blueprint("admin", __name__)


# ── Místní skupiny ───────────────────────────────────────────────────────────


@bp.route("/units", methods=["GET", "POST"])
@require("unit.manage")
def units() -> str | Response:
    if request.method == "POST":
        name, error = forms.clean_name(request.form.get("name", ""))
        if error:
            flash(error, "danger")
        else:
            people.create_unit(name, me().dn)
            flash(f"Místní skupina {name} je vytvořena.", "success")
        return redirect(url_for("admin.units"))
    return render_template("admin/units.html", units=people.list_units(me().dn))


@bp.route("/units/<unit_id>", methods=["POST"])
@require("unit.manage")
def rename_unit(unit_id: str) -> Response:
    unit = people.get_unit(unit_id, me().dn)
    if unit is None or unit.is_external:
        abort(404)
    name, error = forms.clean_name(request.form.get("name", ""))
    if error:
        flash(error, "danger")
    else:
        people.rename_unit(unit, name, me().dn)
        flash("Název uložen.", "success")
    return redirect(url_for("admin.units"))


# ── Kvalifikace ──────────────────────────────────────────────────────────────


def _qual_or_404(qual_id: str) -> people.Qualification:
    qual = next((q for q in people.list_qualifications(me().dn) if q.id == qual_id), None)
    if qual is None:
        abort(404)
    return qual


@bp.route("/qualifications")
@require("qualification.manage")
def qualifications() -> str:
    quals = people.list_qualifications(me().dn)
    counts = {q.id: len(people.holders(q.id, me().dn)) for q in quals}
    return render_template("admin/qualifications.html", quals=quals, counts=counts, names={q.id: q.name for q in quals})


@bp.route("/qualifications/new", methods=["GET", "POST"])
@bp.route("/qualifications/<qual_id>", methods=["GET", "POST"])
@require("qualification.manage")
def qualification(qual_id: str | None = None) -> str | Response:
    qual = _qual_or_404(qual_id) if qual_id else None
    others = [q for q in people.list_qualifications(me().dn) if qual is None or q.id != qual.id]
    if request.method == "POST":
        name, error = forms.clean_name(request.form.get("name", ""))
        parents = [p for p in request.form.getlist("parents") if p in {q.id for q in others}]
        if error:
            flash(error, "danger")
        else:
            try:
                people.save_qualification(
                    qual,
                    name,
                    request.form.get("description", "").strip(),
                    parents,
                    bool(request.form.get("can_be_rp")),
                    me().dn,
                )
            except StaleEntry:
                flash("Kvalifikaci mezitím změnil někdo jiný. Zkontrolujte ji a uložte znovu.", "warning")
            else:
                flash("Kvalifikace uložena.", "success")
                return redirect(url_for("admin.qualifications"))
    holders = []
    if qual is not None:
        wanted = {dn.lower() for dn in people.holders(qual.id, me().dn)}
        holders = [p for p in people.search_people(me().dn) if p.dn.lower() in wanted]
    return render_template("admin/qualification.html", qual=qual, others=others, holders=holders)


@bp.route("/qualifications/<qual_id>/delete", methods=["POST"])
@require("qualification.manage")
def delete_qualification(qual_id: str) -> Response:
    qual = _qual_or_404(qual_id)
    if people.delete_qualification(qual, me().dn):
        flash(f"Kvalifikace {qual.name} je smazána.", "success")
    else:
        flash("Kvalifikaci někdo má nebo je nadřazenou jiné kvalifikaci, nelze ji smazat.", "warning")
    return redirect(url_for("admin.qualifications"))


# ── Sdílení údajů ────────────────────────────────────────────────────────────


@bp.route("/grants", methods=["GET", "POST"])
@require("grant.manage")
def grants() -> str | Response:
    units = people.list_units(me().dn, include_external=True)
    everyone = people.search_people(me().dn, statuses=["new", "invited", "active", "inactive"])
    if request.method == "POST":
        form = request.form
        target = next((u for u in units if u.id == form.get("target")), None)
        level = form.get("level", "")
        grantee = form.get("grantee_unit") if form.get("grantee_kind") == "unit" else form.get("grantee_person")
        expires_at, error = forms.clean_date(form.get("expires", ""))
        if target is None or level not in people.LEVELS or not grantee:
            error = error or "Vyplňte komu, čí údaje a v jakém rozsahu."
        if grantee and target is not None and grantee == target.id:
            error = "Místní skupina vidí své členy už sama."
        if error:
            flash(error, "danger")
        else:
            assert target is not None and grantee is not None
            try:
                people.create_grant(
                    grantee, target, level, expires_at, form.get("description", "").strip(), me().person.id, me().dn
                )
            except ValueError:
                flash("Příjemce nebyl nalezen.", "danger")
            else:
                flash("Sdílení je nastaveno.", "success")
        return redirect(url_for("admin.grants"))
    names = {u.id: u.name for u in units} | {p.id: p.name for p in everyone}
    names |= {me().person.id: me().person.name}
    return render_template(
        "admin/grants.html",
        grants=sorted(people.list_grants(me().dn), key=lambda g: people.sort_key(names.get(g.grantee, ""))),
        units=units,
        unit_names={u.dn.lower(): u.name for u in units},
        people=everyone,
        names=names,
        levels=people.LEVELS,
    )


@bp.route("/grants/<grant_id>/revoke", methods=["POST"])
@require("grant.manage")
def revoke_grant(grant_id: str) -> Response:
    grant = next((g for g in people.list_grants(me().dn) if g.id == grant_id), None)
    if grant is None:
        abort(404)
    people.revoke_grant(grant, me().dn)
    flash("Sdílení je zrušeno.", "success")
    return redirect(url_for("admin.grants"))


# ── Historie a oprávnění ─────────────────────────────────────────────────────


@bp.route("/history")
@require("history.view")
def global_history() -> str:
    return render_template("admin/history.html", changes=history.changes(me().dn))


def medcover_roles() -> tuple[dict | None, str]:
    """MedCover's live role → permission mapping, or None and the reason."""
    url = current_app.config["MEDCOVER_ROLES_URL"]
    if not url:
        return None, "MedCover zatím mapování rolí nevystavuje."
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {keycloak.service_token()}"}, timeout=10)
        resp.raise_for_status()
        return resp.json(), ""
    except requests.RequestException, keycloak.KeycloakError, ValueError:
        return None, "MedCover teď neodpovídá, zkuste to později."


@bp.route("/permissions")
@require("role.assign")
def permissions() -> str:
    medcover, reason = medcover_roles()
    return render_template(
        "admin/permissions.html",
        own={ROLE_LABELS[r]: sorted(PERMISSION_LABELS[p] for p in perms) for r, perms in ROLE_PERMISSIONS.items()},
        medcover=medcover,
        reason=reason,
    )
