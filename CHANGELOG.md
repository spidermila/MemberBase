# Changelog

All notable changes to MemberBase are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- OpenLDAP directory image (Debian `slapd`, LDAPS only, Argon2 hashes, `accesslog` change log) with the `crc` schema, the district tree and access rules enforcing Místní skupina visibility, grants and roles.
- Keycloak realm `crc`: LDAP federation (passwords written through LDAP, everything else read-only), single sign-on for MemberBase and MedCover, mandatory second factor for admin roles, passkeys, Czech login theme in MedCover colours.
- MemberBase („Evidence členů“) MVP: member list with search and filters, create/edit/move people, invitations, activate/deactivate/archive/restore, MedCover and MemberBase role assignment (also in batch), Místní skupiny, MedCover qualifications, visibility grants with expiry, change log from `accesslog`, second-factor reset, own profile.
- Jobs: grant expiry and repair of each Místní skupina's members group (`flask expire-grants`, `flask repair-members`).
- MemberBase has its own logo (also on the Keycloak login page), favicon and home-screen icon instead of MedCover's.
