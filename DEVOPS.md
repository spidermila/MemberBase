# MemberBase — DevOps Reference

This document covers how MemberBase is developed, tested, run together with
MedCover, and deployed. MedCover's side of the same process is in
[MedCover's `DEVOPS.md`](https://github.com/spidermila/MedCover/blob/main/DEVOPS.md).
Code layout and configuration variables are in `README.md`.

---

## Two Repositories, One Stack

MemberBase and MedCover are developed in separate repositories and run as one
stack per Oblastní spolek. Each repository owns its own services; they meet
only through a small, documented contract.

| Repository | Compose project | Services | Owns |
|---|---|---|---|
| MemberBase (public) | `memberbase` | `openldap`, `keycloak`, `keycloak-db`, `mailpit`, `memberbase`, `memberbase-jobs` | Directory schema, access rules and initial tree; Keycloak realm and login theme; the MemberBase app |
| MedCover (public) | `medcover` | `web`, `scheduler`, `db`, `azurite` | MedCover code; its OIDC login and directory sync settings |
| Infrastructure (private) | — (Bicep) | the production stack | Host names, secrets, pinned image versions, Azure resources |

Rules:

- **No cross-repository paths.** Neither repository builds, mounts or reads
  files from the other's checkout. Checkouts can live anywhere.
- **Each service is defined once**, in the repository that owns it. MedCover's
  compose files never define directory or Keycloak services, and this
  repository never defines MedCover services.
- **Dev stacks connect over one Docker network**, `crc-dev`. Production
  connects the same services inside one Azure Container Apps environment.
- **Production runs images, never source.** Each repository publishes its own
  images; the private infrastructure repository pins which versions run.
- **Real deployment details stay out of both public repositories.** Host
  names, the base DN, the `crcDistrictId`, secrets and Azure resource names
  belong in untracked `.env` files or the infrastructure repository.

### The contract between MemberBase and MedCover

Everything MedCover relies on. A change to any row is a contract change (see
[Changing the contract](#changing-the-contract)).

| Item | Value |
|---|---|
| Directory service name | `openldap`, LDAPS on port 1636, CA certificate in the volume `memberbase-ldap-certs` (dev) or a secret (prod) |
| Sync account | `cn=medcover-sync,ou=services,<base DN>`; which attributes and subtrees it may read is defined by the access rules in `deploy/openldap/ldif/access-rules.ldif`. Its one write: activating an invited MedCover user at their first MedCover login, as a single modify of the person entry with delete `crcMemberStatus: invited`, add `crcMemberStatus: active` and replace `crcStatusChangedAt` (a replace of `crcMemberStatus` is refused). `noSuchAttribute` means the person is no longer invited (activated by MemberBase or changed meanwhile): read the status again. The directory cannot check that this happens only at a login; it trusts MedCover to activate only then |
| Keycloak service name | `keycloak`, HTTP port 8080 inside the network |
| OIDC issuer | `<Keycloak URL>/realms/crc` |
| MedCover client | client ID `medcover`, confidential, redirect `<MedCover URL>/auth/callback`, back-channel logout `<MedCover URL>/auth/backchannel-logout` |
| Token claims | `crc_member_id` (person identifier), `medcover_roles` |
| Roles endpoint | MedCover serves `GET /api/roles`; MemberBase reads it from `MEDCOVER_ROLES_URL` |
| Shared secrets | `MEDCOVER_CLIENT_SECRET` and `LDAP_MEDCOVER_SYNC_PASSWORD` must have the same value on both sides |

---

## Local Development

### MemberBase only

```bash
cp .env.example .env
docker compose up --build -d
```

| URL | What |
|---|---|
| http://localhost:5100 | MemberBase (bootstrap admin `admin@example.org` / `admin-heslo-123`; an authenticator app is required at first login) |
| http://localhost:8180/admin | Keycloak admin console (`admin` / `admin`) |
| http://localhost:8025 | Mailpit (all email: invitations, password resets) |

`memberbase` mounts the checkout read-only and runs `flask run --debug`, so
Python and template edits reload on the next request. Keycloak mounts the
login theme with caching off, so theme edits show on the next page load.
`memberbase-jobs` runs from the image: rebuild it after code changes.

Any host name of the dev machine works; Keycloak has no fixed host name in
dev. Passkeys need a secure context: `localhost` (an SSH tunnel works) or
HTTPS. Passkeys are bound to the Keycloak host name, so changing it
invalidates them.

### MemberBase and MedCover together

1. Start this stack first (as above). It creates the network `crc-dev` and
   the volume `memberbase-ldap-certs`.
2. In MedCover, enable its MemberBase overlay (see MedCover's `DEVOPS.md`)
   and start MedCover. The overlay only attaches `web` and `scheduler` to
   `crc-dev` and sets their OIDC and directory variables.
3. The dev secrets in both `.env.example` files have the same values, so the
   shared secrets match without editing.

To take this stack down while MedCover is attached, stop MedCover first;
otherwise Docker cannot remove the `crc-dev` network.

Ports: MemberBase 5100, Keycloak 8180, Mailpit 8025. MedCover keeps 5000 and
1433. `keycloak-db` publishes no port.

### Behind an HTTPS reverse proxy

Passkeys and Face ID on anything but `localhost` need HTTPS. Give each app
its own host name (for example `evidence.example.org` → 5100 and
`sso.example.org` → 8180) and set in `.env`:

```bash
KEYCLOAK_HOSTNAME=https://sso.example.org
KEYCLOAK_BACKCHANNEL_DYNAMIC=true
KEYCLOAK_PUBLIC_URL=https://sso.example.org
MEMBERBASE_URL=https://evidence.example.org
SESSION_COOKIE_SECURE=true
```

The proxy must pass `X-Forwarded-Proto/Host/Port` and should block `/admin`
on the Keycloak host. Real host names go only into `.env`.

### Real email instead of Mailpit

Two programs send email, and each has its own SMTP settings:

- **Keycloak** (invitations, password resets): the realm's email settings,
  seeded from `KC_SMTP_*` only when the realm is first imported. On a running
  realm, change them in the admin console: realm `crc` → *Realm settings* →
  *Email*, then *Test connection* (sends to the logged-in admin's email).
- **MemberBase** (notices of an email change and a second-factor reset): the
  `SMTP_*` and `MAIL_FROM` environment variables. Only STARTTLS (usually port
  587) is supported, not implicit TLS on 465.

Use the same relay and sender for both; the relay must allow that sender.
Keep the SMTP login out of every repository: put `SMTP_USER` and
`SMTP_PASSWORD` in an env file outside the checkout (mode 600), load it with
`env_file:` from an untracked compose override, set `SMTP_HOST`, `SMTP_PORT`,
`SMTP_STARTTLS` and `MAIL_FROM` under `environment:` in the same override
(they override the Mailpit values), and recreate only `memberbase`.

### Keycloak realm changes

Keycloak imports `deploy/keycloak/realm-crc.json` **only when the realm does
not exist yet**; `MEMBERBASE_URL` and `MEDCOVER_URL` are also read only then.
After editing the realm file, either apply the same change in the admin
console, or drop the `keycloak` database and restart `keycloak` (this loses
registered passkeys and TOTP secrets). Always keep `realm-crc.json` in sync
with what you change live.

### Directory changes

The OpenLDAP image initialises itself on first start (see `README.md`).
Changes to the schema, access rules or initial tree in `deploy/openldap/` do
not reach an initialised directory. Apply them with `ldapmodify` over
`ldapi:///` inside the container, or delete the `ldap_data` volume to start
fresh:

```bash
docker compose exec openldap ldapsearch -Y EXTERNAL -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ -b cn=config
```

Cover every access-rule change with an allowed and a denied case in
`tests/test_access_rules.py`.

---

## Tests

```bash
./scripts/test.sh                                  # fresh OpenLDAP, full suite
./scripts/test.sh --no-cov tests/test_members.py   # subset, skip coverage gate
```

`scripts/test.sh` runs pytest in a container against a throwaway OpenLDAP
(`docker-compose.test.yml`, project `memberbase-test`, data on tmpfs) and
tears it down. Keycloak is mocked. There is no host virtualenv workflow.

The full suite must reach **100 % line and branch coverage**. A scoped run
fails the gate even when every test passes, so use `--no-cov` for subsets.

Lint and type-check the same way CI does:

```bash
docker compose -f docker-compose.test.yml run --rm --no-deps tests \
  sh -c "black --check memberbase tests && isort --check memberbase tests && flake8 memberbase tests && mypy memberbase"
```

Tests that need both apps (OIDC login into MedCover, the directory sync,
back-channel logout, Playwright E2E) live in MedCover. They run against
**published, pinned** MemberBase images, never against a MemberBase checkout.

---

## Images

This repository publishes three images to `ghcr.io/spidermila/`:

| Image | Built from | Runs as |
|---|---|---|
| `memberbase` | `Dockerfile` | `memberbase` (gunicorn) and `memberbase-jobs` |
| `memberbase-openldap` | `deploy/openldap/` | `openldap` |
| `memberbase-keycloak` | official Keycloak image + `deploy/keycloak/` (realm and theme copied in, `kc.sh build` for the MSSQL database) | `keycloak` (`start --optimized`) |

The dev stack builds `memberbase` and `memberbase-openldap` locally and runs
the official Keycloak image with the realm and theme mounted, so edits show
without a rebuild. Production uses the published images only.

Rebuild the dev images after changing `requirements*.txt`, `Dockerfile` or
anything in `deploy/openldap/`:

```bash
docker compose up -d --build memberbase memberbase-jobs openldap
```

---

## Dependency Management

Edit `requirements.in` or `requirements-dev.in`, run
`./scripts/compile_requirements.sh` (compiles with hashes inside the runtime
image) and commit both the `.in` and `.txt` files.

Dependabot watches pip, GitHub Actions, the base images in both Dockerfiles
and the Keycloak image version.

---

## CI/CD Pipeline

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | every PR, push to `main` | black, isort, flake8, mypy in the test image; `./scripts/test.sh` (100 % gate); `realm-crc.json` is valid JSON |
| `release.yml` | tag `v*` | builds and pushes the three images tagged with the version and `latest` |

CI in this repository never checks out MedCover, and MedCover's CI never
checks out this repository. MedCover's integration and E2E workflows pull the
MemberBase image version pinned in MedCover (Dependabot proposes bumps).

Deployment is not triggered from this repository: the infrastructure
repository pins the new version and deploys it.

---

## Versioning & Changelog

Releases are tagged `vX.Y.Z` on `main`. Every user-visible change goes into
both changelogs in the same PR: `CHANGELOG.md` (English, developer) and
`memberbase/templates/main/changelog.html` (Czech, users). A release renames
`[Unreleased]` in both.

### Changing the contract

When a change touches [the contract](#the-contract-between-memberbase-and-medcover):

1. Change MemberBase in a backward-compatible way (add before you remove),
   release it, and note under `### Changed` which MedCover version needs it.
2. Update MedCover to the new contract and bump its pinned MemberBase version.
3. Deploy MemberBase first, then MedCover.
4. Remove the old form in a later MemberBase release.

---

## Production Deployment

One stack per Oblastní spolek, in the same Azure Container Apps environment as
MedCover, defined in the private infrastructure repository.

| Container app | Image | Replicas | State |
|---|---|---|---|
| `openldap` | `memberbase-openldap` | exactly 1 | Azure Files share at `/var/lib/ldap` (database and `cn=config`) |
| `keycloak` | `memberbase-keycloak` | 1 | database `keycloak` on the existing Azure SQL server |
| `memberbase` | `memberbase` | 1 | none |
| `memberbase-jobs` | `memberbase` | Container Apps job, every 15 min | none; runs `flask expire-grants` and `flask repair-members` |

- OpenLDAP is internal only (no ingress outside the environment). Keycloak and
  MemberBase have public HTTPS ingress; the Keycloak admin console is not
  public.
- The LDAPS certificate and CA come from Container Apps secrets mounted at
  `/certs`, so they survive restarts and the same CA is given to Keycloak,
  MemberBase and MedCover.
- `LDAP_BASE_DN`, `LDAP_DISTRICT_ID` and the initial passwords are set on the
  first start only; after that the directory keeps its own state.
- Keycloak's database uses a `_UTF8` collation and `READ_COMMITTED_SNAPSHOT
  ON`.
- The permanent Keycloak host name must be fixed before anyone registers a
  passkey in production: it becomes the passkey RP ID.

### OpenLDAP on Azure Files

Azure Files is the only persistent storage Container Apps can mount, so the
directory's LMDB database lives on an SMB share. LMDB assumes that only one
host opens the database, so:

- `openldap` always runs **exactly one replica** (`minReplicas = maxReplicas
  = 1`), and nothing else mounts its share.
- Updates are **stop, then start**, never rolling: scale `openldap` to 0,
  wait until the old replica is gone, deploy the new image, scale back to 1.
  A rolling revision update would briefly run two slapd processes on the same
  files.
- Before going live, a drill on a test environment must show that slapd
  survives restarts, a new revision rollout and an SMB reconnect with its
  data intact, and that a backup restores.

### Backups

- **Directory:** a nightly `slapcat` of the main and `accesslog` databases,
  run inside the `openldap` container, written to a dump directory on the
  share. Azure Backup for the file share keeps 60 days of snapshots, off the
  server (RPO 1 day, RTO 12 hours).
- **Keycloak:** its Azure SQL database is backed up with MedCover's (TDE,
  point-in-time restore). It holds TOTP secrets, passkeys and sessions, so
  it is restored together with the directory.

### Deploying a new version

1. Merge to `main`, tag `vX.Y.Z`; `release.yml` publishes the images.
2. In the infrastructure repository, pin the new version and deploy. Deploy
   `openldap` with the stop-then-start procedure above.
3. Directory schema or access-rule changes do not apply themselves to an
   existing directory: ship them as an `ldapmodify` step in the release
   notes, applied over `ldapi:///` inside the `openldap` container, and
   tested on the dev stack first.
4. Realm changes do not apply themselves either: apply them through the
   Keycloak admin API and keep `realm-crc.json` identical.

---

## Secrets Management

- Dev secrets have defaults in `.env.example` (`admin-heslo-123` and the
  `dev-*` values); never reuse them anywhere else.
- `.env` is never committed. Production secrets live in the infrastructure
  repository's secret store and are passed to the container apps as secrets.
- Each service has its own directory account (`keycloak`, `medcover-sync`,
  `memberbase`) and its own password. Rotating one means changing it in the
  directory and in the one app that uses it.
- Root access to the directory exists only inside the `openldap` container
  over `ldapi:///`; there is no root password.

### SMTP login in production

The SMTP password is stored once, in Azure Key Vault, and reaches both
programs without being copied into configuration:

- **MemberBase:** a Container Apps secret that references the Key Vault
  secret (`keyVaultUrl` plus the app's managed identity with the *Key Vault
  Secrets User* role), passed as `SMTP_PASSWORD` via `secretRef`. The other
  `SMTP_*` values and `MAIL_FROM` are plain environment variables.
- **Keycloak:** its file vault. Set `KC_VAULT=file` and `KC_VAULT_DIR`
  (build options, so part of the image build when it starts with
  `--optimized`), mount the same Key Vault-backed secret as a secret volume
  in that directory under the file name `crc_smtp-password` (realm, `_`, key),
  and enter `${vault.smtp-password}` as the password in the realm's email
  settings. The Keycloak database then holds only the reference, not the
  password. Leave `KC_SMTP_PASSWORD` unset.
- Rotating: update the Key Vault secret, then restart both container apps
  (a new revision) so they read the new value.

---

## Moving from the shared `medcover` dev project

Until now, the MemberBase dev services ran inside MedCover's compose project
(profile `memberbase`, built from `MEMBERBASE_DIR`). Remaining steps to reach
the layout above:

- [ ] This repository: `keycloak-db` (MSSQL) in `docker-compose.yml`, the
      `crc-dev` network and `memberbase-ldap-certs` volume names, the
      reverse-proxy variables in `.env.example`, `release.yml`, the
      `memberbase-keycloak` image, Dependabot.
- [ ] MedCover: replace the `memberbase` profile with its overlay that joins
      `crc-dev`; update its `DEVOPS.md`.
- [ ] Dev instance: stop and remove the MemberBase containers of the
      `medcover` project, then `docker compose up -d` in the MemberBase
      checkout. The directory and the Keycloak database start empty.
- [ ] Infrastructure repository: the Azure Files share, the container apps
      and job, and the drill above.
