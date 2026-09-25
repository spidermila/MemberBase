"""Validation of submitted form values (CSRF is checked globally)."""

import re
from datetime import UTC, datetime, time

from memberbase.people import PRAGUE

# Phone: 9 bare digits, or +/00 followed by 10-15 digits (spaces ignored).
_PHONE_RE = re.compile(r"^\d{9}$|^(\+|00)\d{10,15}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_NAME = 120
MAX_NOTE = 200


def clean_name(raw: str) -> tuple[str, str | None]:
    name = " ".join(raw.split())
    if not name:
        return name, "Vyplňte jméno."
    if len(name) > MAX_NAME:
        return name, f"Jméno může mít nejvýše {MAX_NAME} znaků."
    return name, None


def clean_note(raw: str) -> str:
    """Free text on one line, cut to MAX_NOTE."""
    return " ".join(raw.split())[:MAX_NOTE]


def clean_email(raw: str) -> tuple[str, str | None]:
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email):
        return email, "Zadejte platný e-mail."
    return email, None


def clean_phone(raw: str) -> tuple[str, str | None]:
    phone = raw.strip().replace(" ", "")
    if phone and not _PHONE_RE.match(phone):
        return phone, "Telefon zadejte jako 9 číslic nebo s předvolbou (+420…)."
    return phone, None


def clean_date(raw: str) -> tuple[datetime | None, str | None]:
    """Optional date (YYYY-MM-DD) → end of that day in Prague, as UTC."""
    if not raw.strip():
        return None, None
    try:
        day = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        return None, "Zadejte platné datum."
    return datetime.combine(day, time(23, 59, 59), PRAGUE).astimezone(UTC), None
