from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from memberbase import auth, forms, people
from memberbase.auth import login_required, me
from memberbase.directory import StaleEntry

bp = Blueprint("main", __name__)


@bp.route("/")
def index() -> str | Response:
    if me() is None:
        return render_template("main/landing.html")
    return redirect(url_for("members.index"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile() -> str | Response:
    # Read as the person themselves: they see what the directory shows them.
    person = people.find_person(me().person.id, me().dn)
    assert person is not None
    if request.method == "POST":
        phone, error = forms.clean_phone(request.form.get("phone", ""))
        if error:
            flash(error, "danger")
        else:
            try:
                people.update_person(person, {"phone": phone}, me().dn, request.form.get("csn", ""))
                flash("Telefon uložen.", "success")
            except StaleEntry:
                flash("Údaje se mezitím změnily. Zkontrolujte je a uložte znovu.", "warning")
        return redirect(url_for("main.profile"))
    quals = {q.id: q for q in people.list_qualifications(me().dn)}
    held = [quals[q].name for q in people.holdings_of(person, me().dn) if q in quals]
    roles = [r for r in people.list_roles(me().dn) if r.key in me().roles]
    return render_template(
        "main/profile.html", person=person, held=sorted(held, key=people.sort_key), roles=roles, kc=auth.KC_ACTIONS
    )


@bp.route("/changelog")
def changelog() -> str:
    return render_template("main/changelog.html")
