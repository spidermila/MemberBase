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
- MS Chair („Předseda MS“): an Admin appoints Chairs of a Místní skupina (`cn=chair` under it) in the person's role form. A Chair creates, edits, invites, activates, deactivates and archives the people of their own Místní skupina and manages their MedCover qualifications. The directory enforces the scope. Chairs cannot change people who hold a privileged role (MemberBase Admin or OS koordinátor, MedCover admin or coordinator) or another Chair; only Admins can.
- Requests („Žádosti“): a Chair asks to move one of their people to another Místní skupina, and that Místní skupina's Chair approves or rejects. A Chair or Admin asks to see named people of other Místní skupiny (typed names, a level and an optional end date), with one request per Místní skupina. Its Chair matches each name to a member (an exact match ignoring case and accents is preselected) or skips it. Deciders and requesters are emailed. MemberBase's service account carries out approved requests after re-checking them. The requester is whoever filed the request (the access rules make them name themselves); a request's status only moves forward, and nobody decides their own.
- Grants for a single person (`cn=readers-<level>` under the person entry), created by approved access requests and listed and revocable on the „Sdílení údajů“ page.

### Changed
- New people start with status `new` („Nepozvaný“) and become `invited` („Pozvaný“) only when an invitation is sent. Keycloak does not see `new` people, so they cannot log in or reset a password, and the access rules deny them everything like other non-active people. (#3)
- The `district-coordinator` role is displayed as "OS koordinátor" instead of "Okresní koordinátor".
- The navbar is a shade darker than MedCover's plain Bootstrap danger red, and its text uses the same light colour as MedCover's navbar.
- `flask repair-members` also removes archived people from app roles and `cn=chair`. A Chair who archives someone cannot change roles. It also creates `cn=chair` and `ou=requests` in Místní skupiny that predate them.
- The service account (`cn=memberbase`) may now rename people, write their `crcMemberKind` and `uid`, add children under Místní skupiny and people, change `cn=chair` membership, fill per-person readers groups and remove role members. These rights serve the requests and the members job. Since the account can already act as any person, they don't widen what it can reach, but a leaked service password could now delete people directly.
- The global „Historie změn“ shows the last 30 days (the last day after a burst of changes). It used to read the whole `accesslog`, which fails with a size-limit error once the log holds more than 5000 records.
- Fewer directory searches per page: the logged-in person takes 2 searches per request instead of 6 (roles and Chair memberships come from one search), finding people and listing Místní skupiny take one search each instead of two, batch actions look up all selected people at once, and the „Kvalifikace“ list counts holders in one search. The Keycloak service token is fetched once per request.
- Upgrading an existing directory: with `ldapmodify` over `ldapi:///`, add the new schema (`crcAttr:19`–`31`, `crcClass:9`–`11`), `olcAddContentAcl: TRUE`, the new indexes, the `refint` attributes (`crcRequestedByDn`, `crcRequestNotify`, `crcGrantTarget`) and the `unique` URI for `crcRequestId`. Replace `olcAccess` with `access-rules.ldif` rendered for the deployment, the way `entrypoint.sh` renders it; the own-Místní-skupina clause now refers to `$2`, not `$1`. Then run `flask repair-members`.

### Fixed
- Keycloak realm: logging out of MedCover returns to MedCover's login page instead of stopping at "Invalid redirect uri" (the `medcover` client now allows `${MEDCOVER_URL}/auth/login` after logout). (#5)
- Keycloak login theme: after a password reset opened from the email in a new session, the „Účet byl aktualizován“ page links back to the application instead of ending there (a small `info.ftl` override; Keycloak itself hides the link there). The `medcover` client gets a base URL for that link. (#5)

### Security
- `olcAddContentAcl` is on: whoever adds an entry needs add access to every attribute in it. Nobody may set `authzTo`/`authzFrom` outside `ou=services`.
