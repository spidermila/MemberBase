"""The only module that talks LDAP.

Owns connection setup, proxied authorization (every change and every
person-facing read runs as the logged-in person, so the directory's access
rules decide), optimistic locking via an assertion on entryCSN, and escaping
of every value placed in a filter or DN.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import ldap
from flask import current_app, g
from ldap.controls.libldap import AssertionControl
from ldap.controls.simple import ProxyAuthzControl
from ldap.dn import escape_dn_chars
from ldap.filter import escape_filter_chars

__all__ = ["Entry", "StaleEntry", "Denied", "Conflict", "TooMany", "escape_filter_chars", "escape_dn_chars"]


class StaleEntry(Exception):
    """The entry changed since the form was loaded."""


class Denied(Exception):
    """The directory refused the operation for this person."""


class Conflict(Exception):
    """A unique value (e.g. email) is already used by another entry."""


class TooMany(Exception):
    """A search matched more entries than the server's size limit."""


@dataclass
class Entry:
    dn: str
    attrs: dict[str, list[str]] = field(default_factory=dict)

    def first(self, name: str, default: str = "") -> str:
        values = self.attrs.get(name)
        return values[0] if values else default

    def all(self, name: str) -> list[str]:
        return list(self.attrs.get(name, []))

    @property
    def parent_dn(self) -> str:
        return self.dn.split(",", 1)[1]


def connect() -> ldap.ldapobject.LDAPObject:
    cfg = current_app.config
    conn = ldap.initialize(cfg["LDAP_URI"])
    conn.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
    conn.set_option(ldap.OPT_REFERRALS, 0)
    conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
    conn.set_option(ldap.OPT_X_TLS_CACERTFILE, cfg["LDAP_CA_CERT"])
    conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
    conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
    conn.simple_bind_s(cfg["LDAP_BIND_DN"], cfg["LDAP_BIND_PASSWORD"])
    return conn


def _conn() -> ldap.ldapobject.LDAPObject:
    if "ldap_conn" not in g:
        g.ldap_conn = connect()
    return g.ldap_conn


def close(_exc: BaseException | None = None) -> None:
    conn = g.pop("ldap_conn", None)
    if conn is not None:
        conn.unbind_s()


def base_dn() -> str:
    return current_app.config["LDAP_BASE_DN"]


def _controls(as_dn: str | None, assertion: str | None = None) -> list:
    controls: list = []
    if as_dn is not None:
        controls.append(ProxyAuthzControl(criticality=True, authzId=f"dn:{as_dn}".encode()))
    if assertion is not None:
        controls.append(AssertionControl(criticality=True, filterstr=assertion))
    return controls


def csn_assertion(csn: str | None) -> str | None:
    """Optimistic lock: the write succeeds only if the entry is unchanged."""
    return None if csn is None else f"(entryCSN={escape_filter_chars(csn)})"


def _decode(attrs: dict[str, list[bytes]]) -> dict[str, list[str]]:
    return {name: [v.decode() for v in values] for name, values in attrs.items()}


def _encode(values: Iterable[str]) -> list[bytes]:
    return [v.encode() for v in values]


def search(
    base: str,
    filterstr: str = "(objectClass=*)",
    attrs: list[str] | None = None,
    scope: int = ldap.SCOPE_SUBTREE,
    as_dn: str | None = None,
) -> list[Entry]:
    """Search as `as_dn` (proxied) or as the service account when None.

    A missing base returns an empty list: to a person without access, an
    entry they may not see looks the same as one that does not exist.
    """
    attrlist = None if attrs is None else [*attrs, "entryCSN"]
    try:
        result = _conn().search_ext_s(base, scope, filterstr, attrlist, serverctrls=_controls(as_dn))
    except ldap.NO_SUCH_OBJECT:
        return []
    except ldap.SIZELIMIT_EXCEEDED as exc:
        raise TooMany() from exc
    return [Entry(dn, _decode(a)) for dn, a in result if dn is not None]


def get(dn: str, attrs: list[str] | None = None, as_dn: str | None = None) -> Entry | None:
    found = search(dn, attrs=attrs, scope=ldap.SCOPE_BASE, as_dn=as_dn)
    return found[0] if found else None


def _write(call: Callable[..., Any], *args: Any, as_dn: str | None, assertion: str | None = None) -> None:
    try:
        call(*args, serverctrls=_controls(as_dn, assertion))
    except ldap.ASSERTION_FAILED as exc:
        raise StaleEntry() from exc
    except ldap.INSUFFICIENT_ACCESS as exc:
        raise Denied() from exc
    except ldap.CONSTRAINT_VIOLATION as exc:
        raise Conflict() from exc


def add(dn: str, attrs: dict[str, list[str]], as_dn: str | None) -> None:
    _write(_conn().add_ext_s, dn, [(k, _encode(v)) for k, v in attrs.items() if v], as_dn=as_dn)


def modify(
    dn: str, changes: dict[str, list[str]], as_dn: str | None, csn: str | None = None, assertion: str | None = None
) -> None:
    """Replace each attribute in `changes`; an empty list deletes it."""
    mods = [(ldap.MOD_REPLACE, k, _encode(v) or None) for k, v in changes.items()]
    _write(_conn().modify_ext_s, dn, mods, as_dn=as_dn, assertion=assertion or csn_assertion(csn))


def swap(
    dn: str,
    attr: str,
    old: str,
    new: str,
    as_dn: str | None,
    changes: dict[str, list[str]] | None = None,
    csn: str | None = None,
) -> None:
    """Replace one value of `attr` by another as a delete and an add, so that
    value-specific access rules apply; `changes` are replaced alongside."""
    mods: list[tuple[int, str, list[bytes] | None]] = [
        (ldap.MOD_DELETE, attr, _encode([old])),
        (ldap.MOD_ADD, attr, _encode([new])),
    ]
    mods += [(ldap.MOD_REPLACE, k, _encode(v) or None) for k, v in (changes or {}).items()]
    _write(_conn().modify_ext_s, dn, mods, as_dn=as_dn, assertion=csn_assertion(csn))


def add_values(dn: str, attr: str, values: list[str], as_dn: str | None) -> None:
    try:
        _write(_conn().modify_ext_s, dn, [(ldap.MOD_ADD, attr, _encode(values))], as_dn=as_dn)
    except ldap.TYPE_OR_VALUE_EXISTS:
        pass


def delete_values(dn: str, attr: str, values: list[str], as_dn: str | None) -> None:
    """Remove values; absent values, or an absent entry, are no error."""
    try:
        _write(_conn().modify_ext_s, dn, [(ldap.MOD_DELETE, attr, _encode(values))], as_dn=as_dn)
    except ldap.NO_SUCH_ATTRIBUTE, ldap.NO_SUCH_OBJECT:
        pass


def delete(dn: str, as_dn: str | None) -> None:
    _write(_conn().delete_ext_s, dn, as_dn=as_dn)


def move(dn: str, new_superior: str, as_dn: str | None, csn: str | None = None) -> str:
    rdn = dn.split(",", 1)[0]
    _write(_conn().rename_s, dn, rdn, new_superior, 1, as_dn=as_dn, assertion=csn_assertion(csn))
    return f"{rdn},{new_superior}"
