"""Osvědčení: certificates, training and diplomas of people, managed by
Admins and by the Chair of the person's Místní skupina."""

from datetime import date, datetime, timedelta

import pytest

from memberbase import forms, history, people
from tests.conftest import login, text


@pytest.fixture
def home(world):
    return world.unit()


@pytest.fixture
def chair(world, home):
    return world.chair(home)


def add(person, admin, name="Zdravotník ZZA", issued=date(2024, 3, 1), expires=None, issuer="Úřad ČČK"):
    people.save_certificate(person, None, name, issuer, issued, expires, admin.dn)
    return next(c for c in people.certificates_of(person, admin.dn) if c.name == name)


def form(**kw):
    return {"name": "Zdravotník ZZA", "issuer": "Úřad ČČK", "issued": "2024-03-01", "expires": ""} | kw


def test_state_follows_expiry():
    today = datetime.now(people.PRAGUE).date()
    cert = people.Certificate("dn", "id", "n", "i", today, None, "")
    assert cert.state == "valid"
    cert.expires = today + timedelta(days=people.EXPIRING_DAYS)
    assert cert.state == "valid"
    cert.expires = today
    assert cert.state == "expiring"
    cert.expires = today - timedelta(days=1)
    assert cert.state == "expired"


def test_clean_day():
    assert forms.clean_day("") == (None, None)
    assert forms.clean_day("2024-02-29") == (date(2024, 2, 29), None)
    assert forms.clean_day("31. 1. 2024") == (None, "Zadejte platné datum.")
    assert forms.clean_date("x") == (None, "Zadejte platné datum.")
    assert forms.clean_day("0999-01-01") == (None, "Zadejte platné datum.")


def test_chair_adds_edits_and_deletes(client, world, chair, home):
    person = world.person(home, "Jana Členka")
    login(client, chair)
    page = text(client.get(f"/members/{person.id}"))
    assert "Osvědčení" in page and "Zatím žádná osvědčení." in page
    assert "Nové osvědčení" in text(client.get(f"/members/{person.id}/certificates/new"))
    page = text(
        client.post(f"/members/{person.id}/certificates/new", data=form(expires="2027-03-01"), follow_redirects=True)
    )
    assert "Osvědčení „Zdravotník ZZA“ je uloženo." in page and "01. 03. 2024 – 01. 03. 2027" in page
    (cert,) = people.certificates_of(person, chair.dn)
    assert (cert.issuer, cert.issued, cert.expires) == ("Úřad ČČK", date(2024, 3, 1), date(2027, 3, 1))

    url = f"/members/{person.id}/certificates/{cert.id}"
    assert 'value="Úřad ČČK"' in text(client.get(url))
    client.post(url, data=form(name="Instruktor PP", csn=cert.csn))
    (cert,) = people.certificates_of(person, chair.dn)
    assert (cert.name, cert.expires) == ("Instruktor PP", None)

    page = text(client.post(f"{url}/delete", follow_redirects=True))
    assert "Osvědčení „Instruktor PP“ je smazáno." in page
    assert people.certificates_of(person, chair.dn) == []
    assert client.get(url).status_code == 404


def test_validation_and_stale_form(client, world, admin, home):
    person = world.person(home)
    cert = add(person, admin)
    login(client, admin)
    new = f"/members/{person.id}/certificates/new"
    page = text(client.post(new, data=form(name=" ", issued="")))
    assert "Vyplňte název, kdo osvědčení vydal, a datum vydání." in page
    page = text(client.post(new, data=form(issued="1. 3. 2024")))
    assert "Zadejte platné datum." in page and "Vyplňte název" not in page
    page = text(client.post(new, data=form(expires="2020-01-01")))
    assert "Platnost nemůže skončit před datem vydání." in page
    page = text(
        client.post(
            f"/members/{person.id}/certificates/{cert.id}",
            data=form(name="Jiné", csn="20000101000000.000000Z#000000#000#000000"),
            follow_redirects=True,
        )
    )
    # The form reloads with the current values, never the rejected ones.
    assert "Záznam mezitím změnil někdo jiný" in page and 'value="Zdravotník ZZA"' in page and "Jiné" not in page
    assert [c.name for c in people.certificates_of(person, admin.dn)] == ["Zdravotník ZZA"]


def test_chair_cannot_manage_other_units_or_privileged_people(client, world, admin, chair, home):
    stranger = world.person(world.unit(), "Cizí Osoba")
    boss = world.person(home, "Olga Oprávněná", roles=["medcover:coordinator"])
    cert = add(boss, admin)
    login(client, chair)
    assert client.post(f"/members/{stranger.id}/certificates/new", data=form()).status_code == 404
    page = text(client.get(f"/members/{boss.id}"))
    assert "Zdravotník ZZA" in page and "+ Přidat" not in page and "Smazat" not in page
    page = text(client.get("/members/certificates"))
    assert "Olga Oprávněná" in page and "Upravit" not in page
    page = text(client.get(f"/members/{boss.id}/certificates/new", follow_redirects=True))
    assert "Tuto osobu smí měnit jen Admin" in page and "Nové osvědčení" not in page
    page = text(client.post(f"/members/{boss.id}/certificates/new", data=form(), follow_redirects=True))
    assert "Tuto osobu smí měnit jen Admin" in page
    page = text(client.post(f"/members/{boss.id}/certificates/{cert.id}/delete", follow_redirects=True))
    assert "Tuto osobu smí měnit jen Admin" in page
    assert len(people.certificates_of(boss, admin.dn)) == 1


def test_member_sees_own_certificates_but_not_their_branchs(client, world, admin, home):
    alice, anna = world.person(home, "Alice"), world.person(home, "Anna")
    add(alice, admin, "Alicin kurz")
    add(anna, admin, "Annin kurz")
    login(client, alice)
    assert client.get(f"/members/{anna.id}/certificates/new").status_code == 403
    page = text(client.get(f"/members/{anna.id}"))
    assert "Osvědčení" not in page.split("</nav>", 1)[1]
    page = text(client.get("/profile"))
    assert "Alicin kurz" in page and "bez omezení" in page
    page = text(client.get("/members/certificates"))
    assert "Alicin kurz" in page and "Annin kurz" not in page and "Upravit" not in page


def test_overview_filters(client, world, admin, chair, home):
    other = world.unit()
    jana, petr = world.person(home, "Jana Členka"), world.person(other, "Petr Cizí")
    today = datetime.now(people.PRAGUE).date()
    add(jana, admin, "Prošlý kurz", expires=today - timedelta(days=1))
    add(jana, admin, "Končící kurz", expires=today + timedelta(days=10))
    add(petr, admin, "Cizí kurz", issuer="Jiný úřad")
    archived = world.person(home, "Bývalý Člen", status="former")
    add(archived, admin, "Archivní kurz")
    login(client, admin)
    page = text(client.get("/members/certificates"))
    assert all(n in page for n in ("Prošlý kurz", "Končící kurz", "Cizí kurz")) and "Archivní kurz" not in page
    page = text(client.get("/members/certificates", query_string={"state": "expired"}))
    assert "Prošlý kurz" in page and "Končící kurz" not in page and "Zrušit filtry" in page
    page = text(client.get("/members/certificates", query_string={"unit": other.id}))
    assert "Cizí kurz" in page and "Prošlý kurz" not in page
    page = text(client.get("/members/certificates", query_string={"q": "jiny urad"}))
    assert "Cizí kurz" in page and "Končící kurz" not in page

    login(client, chair)
    page = text(client.get("/members/certificates"))
    assert "Končící kurz" in page and "Cizí kurz" not in page and "back=certificates" in page


def test_edit_from_overview_returns_there(client, world, admin, home):
    person = world.person(home)
    cert = add(person, admin)
    login(client, admin)
    url = f"/members/{person.id}/certificates/{cert.id}"
    resp = client.post(url, data=form(csn=cert.csn, back="certificates"))
    assert resp.location.endswith("/members/certificates")
    resp = client.post(f"{url}/delete", data={"back": "certificates"})
    assert resp.location.endswith("/members/certificates")


def test_records_grant_shows_certificates_and_name(client, world, admin, home):
    reader, person = world.person(world.unit(), "Čtenář"), world.person(home, "Jana Členka")
    add(person, admin)
    people.create_grant(reader.id, home, "records", None, "", admin.id, admin.dn)
    login(client, reader)
    page = text(client.get("/members/certificates"))
    assert "Jana Členka" in page and "Zdravotník ZZA" in page


def test_history_of_a_certificate(client, world, admin, home):
    person = world.person(home, "Historie Osoby")
    cert = add(person, admin, expires=date(2030, 1, 31))
    people.save_certificate(person, cert, "Nový název", cert.issuer, cert.issued, None, admin.dn, cert.csn)
    login(client, admin)
    page = text(client.get(f"/members/{person.id}/history"))
    assert "osvědčení – Historie Osoby" in page
    assert "Název: Zdravotník ZZA" in page and "Platnost do: 31. 01. 2030" in page
    assert "Název předtím: Zdravotník ZZA" in page
    assert history.Labels(admin.dn).value("crcValidUntil", "bad") == "bad"
