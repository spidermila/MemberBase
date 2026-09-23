"""MemberBase role → permission mapping. Roles live in the directory
(ou=roles,ou=memberbase,ou=apps); what they may do lives here. The directory's
access rules enforce the same split independently."""

ADMIN = "admin"
DISTRICT_COORDINATOR = "district-coordinator"

ROLE_LABELS = {ADMIN: "Admin", DISTRICT_COORDINATOR: "Okresní koordinátor"}

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
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    ADMIN: set(PERMISSION_LABELS),
    DISTRICT_COORDINATOR: {"member.view_all"},
}


def permissions_for(roles: set[str]) -> set[str]:
    return set().union(*(ROLE_PERMISSIONS.get(r, set()) for r in roles))
