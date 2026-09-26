# MemberBase – Evidence členů

Member directory of an Oblastní spolek of the Czech Red Cross. One OpenLDAP
directory holds people, passwords, app roles, qualifications and visibility
grants; Keycloak is the only login (OIDC, TOTP, passkeys); this Flask app
administers the directory. MedCover and future apps log in through the same
Keycloak and read only what their own directory account allows (MedCover's
account may also activate an invited MedCover user at their first MedCover
login).

UI is Czech („Evidence členů“); code and docs are English.

## Layout

| Path | What |
| --- | --- |
| `memberbase/` | Flask app. `directory.py` is the only LDAP code (proxied authorization, escaping, optimistic locking); `people.py` the directory model; `auth.py` OIDC login and permissions |
| `deploy/openldap/` | OpenLDAP image: schema, `cn=config`, access rules, initial tree (LDIF templates rendered on first start) |
| `deploy/keycloak/` | Realm export (`realm-crc.json`) and the login theme |
| `tests/` | pytest against a throwaway OpenLDAP; Keycloak is mocked |

## How it works

- Every read or change a person makes runs **as that person** (LDAP proxied
  authorization, RFC 4370). The directory's access rules decide what they may
  see or change, not the app. MemberBase's own account is used only to look
  up the person logging in, activate invited people, run the jobs, carry out
  approved requests, and for requests: look up requesters' names, whom to
  email, and whether a person is privileged.
- Visibility: people see their own Místní skupina at the `contact` level
  (name, email, phone). Other branches and external users are visible only
  through a grant, i.e. membership of `cn=readers-<level>` of the target
  branch or of one person (a person, or a branch's `cn=members` group).
  MedCover access is a fixed grant: anyone holding a MedCover role reads
  every other holder at the `contact` level, with their MedCover
  qualifications and roles. District Coordinators read everything, Admins change everything, MS Chairs
  (`cn=chair` of a branch) change the people of their own branch.
- Certificates („Osvědčení“) are `crcCertificate` entries under the person.
  Only the person, their Chairs, District Coordinators, Admins and holders of
  a `records` grant read them; the rest of the branch does not.
- Requests: moves between branches and access to named people are requested,
  filed under the deciding branch (`ou=requests`) and decided by its Chairs.
  MemberBase's own account carries out an approved request, since neither
  side alone may write it.
- Change log: slapd's `accesslog` records every change with the real person,
  old and new values; nobody can edit it.
- Concurrent edits: writes carry an assertion on the `entryCSN` the form was
  built from.
- Roles live in the directory (`ou=roles,ou=<app>,ou=apps`); what a role may
  do lives in each app (`memberbase/permissions.py`).

## Development

```bash
cp .env.example .env
docker compose up --build -d      # OpenLDAP, Keycloak :8180, Mailpit :8025, MemberBase :5100
```

Log in at http://localhost:5100 as `BOOTSTRAP_ADMIN_EMAIL` /
`BOOTSTRAP_ADMIN_PASSWORD`; admins must set up an authenticator app on first
login. All email lands in Mailpit (http://localhost:8025).

Passkeys need a secure context: use `localhost` (an SSH tunnel is fine) or
HTTPS. The Keycloak admin console is at http://localhost:8180/admin.

Running together with MedCover, CI, releases and production: see `DEVOPS.md`.

## Tests

```bash
./scripts/test.sh                 # fresh OpenLDAP, full suite, 100 % line and branch coverage
./scripts/test.sh --no-cov tests/test_access_rules.py
```

`tests/test_access_rules.py` is the access-rule matrix: every rule is checked
for an allowed and a denied case.

## Dependencies

Edit `requirements*.in`, then `./scripts/compile_requirements.sh` (compiles
with hashes inside the runtime image).

## Configuration

Environment variables (see `memberbase/config.py`): `SECRET_KEY`,
`LDAP_URI`, `LDAP_CA_CERT`, `LDAP_BASE_DN`, `LDAP_BIND_PASSWORD`,
`KEYCLOAK_PUBLIC_URL` (browser-facing; `{scheme}` and `{hostname}` are
filled from the request, default `{scheme}://{hostname}:8180`), `KEYCLOAK_INTERNAL_URL`, `OIDC_CLIENT_SECRET`,
`STEP_UP_SECONDS`, `MEDCOVER_ROLES_URL`, `SMTP_*`, `MAIL_FROM`.

The OpenLDAP image initialises itself on first start from
`LDAP_KEYCLOAK_PASSWORD`, `LDAP_MEDCOVER_SYNC_PASSWORD`,
`LDAP_MEMBERBASE_PASSWORD`, `BOOTSTRAP_ADMIN_EMAIL` (and optional
`BOOTSTRAP_ADMIN_NAME`, `BOOTSTRAP_ADMIN_PASSWORD`, `BOOTSTRAP_UNIT_SLUG`, `BOOTSTRAP_UNIT_NAME`,
`LDAP_BASE_DN`, `LDAP_OWN_BRANCH_LEVEL=contact|basic`). Root access is only
possible inside the container over `ldapi:///`
(`ldapsearch -Y EXTERNAL -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ …`).

## Licence

MIT. See `THIRD_PARTY_NOTICES` for OpenLDAP.
