import os


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} environment variable is required")
    return value


def from_env() -> dict:
    base_dn = _env("LDAP_BASE_DN", "dc=example,dc=org")
    return {
        "SECRET_KEY": _env("SECRET_KEY"),
        # Own name: MedCover on the same host uses Flask's default "session",
        # and browsers do not separate cookies by port.
        "SESSION_COOKIE_NAME": "memberbase_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _env("SESSION_COOKIE_SECURE", "true") == "true",
        "PERMANENT_SESSION_LIFETIME": int(_env("SESSION_LIFETIME_SECONDS", "28800")),
        "LDAP_URI": _env("LDAP_URI", "ldaps://openldap:1636"),
        "LDAP_CA_CERT": _env("LDAP_CA_CERT", "/certs/ca.crt"),
        "LDAP_BASE_DN": base_dn,
        "LDAP_BIND_DN": _env("LDAP_BIND_DN", f"cn=memberbase,ou=services,{base_dn}"),
        "LDAP_BIND_PASSWORD": _env("LDAP_BIND_PASSWORD"),
        # Where browsers reach Keycloak. "{scheme}" and "{hostname}" are
        # filled from the current request, so a dev stack works under any
        # host name; production sets a fixed URL.
        "KEYCLOAK_PUBLIC_URL": _env("KEYCLOAK_PUBLIC_URL", "") or "{scheme}://{hostname}:8180",
        # Where this container reaches Keycloak (tokens, keys, admin API).
        "KEYCLOAK_INTERNAL_URL": _env("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080"),
        # Mailpit only exists in dev; used for the debug-mode "Admin" menu.
        "MAILPIT_PUBLIC_URL": _env("MAILPIT_PUBLIC_URL", "") or "{scheme}://{hostname}:8025",
        "KEYCLOAK_REALM": _env("KEYCLOAK_REALM", "crc"),
        "OIDC_CLIENT_ID": _env("OIDC_CLIENT_ID", "memberbase"),
        "OIDC_CLIENT_SECRET": _env("OIDC_CLIENT_SECRET"),
        # Sensitive actions need a login no older than this.
        "STEP_UP_SECONDS": int(_env("STEP_UP_SECONDS", "300")),
        "MEDCOVER_ROLES_URL": _env("MEDCOVER_ROLES_URL", ""),
        "SMTP_HOST": _env("SMTP_HOST", ""),
        "SMTP_PORT": int(_env("SMTP_PORT", "587")),
        "SMTP_USER": _env("SMTP_USER", ""),
        "SMTP_PASSWORD": _env("SMTP_PASSWORD", ""),
        "SMTP_STARTTLS": _env("SMTP_STARTTLS", "true") == "true",
        "MAIL_FROM": _env("MAIL_FROM", "evidence@localhost"),
        "GIT_COMMIT": _env("GIT_COMMIT", "dev"),
    }
