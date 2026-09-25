# Changelog

All notable changes to MemberBase are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- OpenLDAP directory image (Debian `slapd`, LDAPS only, Argon2 hashes, `accesslog` change log) with the `crc` schema, the district tree and access rules enforcing Místní skupina visibility, grants and roles. (#1)
- Keycloak realm `crc`: LDAP federation (passwords written through LDAP, everything else read-only), single sign-on for MemberBase and MedCover, mandatory second factor for admin roles, passkeys, Czech login theme in MedCover colours. (#1)
- MemberBase („Evidence členů“) MVP: member list with search and filters, create/edit/move people, invitations, activate/deactivate/archive/restore, MedCover and MemberBase role assignment (also in batch), Místní skupiny, MedCover qualifications, visibility grants with expiry, change log from `accesslog`, second-factor reset, own profile. (#1)
- Jobs: grant expiry and repair of each Místní skupina's members group (`flask expire-grants`, `flask repair-members`). (#1)
- MemberBase has its own logo (also on the Keycloak login page), favicon and home-screen icon instead of MedCover's. (#1)
- The „Pozvánky“ page sends invitations to the selected people in one go. (#3)
- Bulk move of the selected people to another Místní skupina from the member list. (#4)

### Changed
- New people start with status `new` („Nepozvaný“) and become `invited` („Pozvaný“) only when an invitation is sent. Keycloak does not see `new` people, so they cannot log in or reset a password, and the access rules deny them everything like other non-active people. (#3)
- The `district-coordinator` role is displayed as "OS koordinátor" instead of "Okresní koordinátor".
- The navbar is a shade darker than MedCover's plain Bootstrap danger red, and its text uses the same light colour as MedCover's navbar.

### Fixed
- Keycloak realm: logging out of MedCover returns to MedCover's login page instead of stopping at "Invalid redirect uri" (the `medcover` client now allows `${MEDCOVER_URL}/auth/login` after logout). (#5)
- Keycloak login theme: after a password reset opened from the email in a new session, the „Účet byl aktualizován“ page links back to the application instead of ending there (a small `info.ftl` override; Keycloak itself hides the link there). The `medcover` client gets a base URL for that link. (#5)
