"""MemberBase role → permission mapping. Roles live in the directory
(ou=roles,ou=memberbase,ou=apps); what they may do lives here. The directory's
access rules enforce the same split independently.

The MS Chair is not an app role but membership of cn=chair of a Místní
skupina; CHAIR_UNIT_PERMISSIONS apply only to the people of that Místní
skupina."""

ADMIN = "admin"
DISTRICT_COORDINATOR = "district-coordinator"
CHAIR = "chair"

ROLE_LABELS = {ADMIN: "Admin", DISTRICT_COORDINATOR: "OS koordinátor", CHAIR: "Předseda MS"}

PERMISSION_LABELS = {
    "member.view_all": "Vidí všechny místní skupiny a externí uživatele",
    "member.edit": "Upravuje osoby, vytváří je a zve",
    "member.status": "Aktivuje, deaktivuje, archivuje a obnovuje osoby",
    "member.move": "Přesouvá osoby mezi místními skupinami",
    "role.assign": "Přiřazuje role aplikací",
    "mfa.reset": "Resetuje dvoufázové ověření",
    "unit.manage": "Spravuje místní skupiny",
    "qualification.manage": "Spravuje kvalifikace",
    "grant.manage": "Spravuje sdílení údajů",
    "history.view": "Vidí historii změn",
    "request.create": "Žádá o přístup k údajům osob z jiných místních skupin",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    ADMIN: set(PERMISSION_LABELS),
    DISTRICT_COORDINATOR: {"member.view_all"},
    CHAIR: {"request.create"},
}
CHAIR_UNIT_PERMISSIONS = {"member.edit", "member.status", "qualification.manage"}


def permissions_for(roles: set[str]) -> set[str]:
    return set().union(*(ROLE_PERMISSIONS.get(r, set()) for r in roles))
