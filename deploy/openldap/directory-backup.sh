#!/bin/bash
# Back up the directory to Blob storage, or restore it from there.
#   directory-backup backup           slapcat cn=config, the main database and
#                                     accesslog, check they load, upload them
#   directory-backup restore <stamp>  load a backup into an empty directory
# A backup is <stamp>/{config,main,accesslog}.ldif.gz plus <stamp>/SHA256SUMS,
# uploaded last: a backup without SHA256SUMS is incomplete.
# BACKUP_CONTAINER_URL is the container URL. With a SAS (?sv=...) it is used
# as is; without, the Container Apps managed identity supplies a token
# (AZURE_CLIENT_ID picks a user-assigned identity).
set -Eeuo pipefail  # -E: the ERR trap in restore() fires inside functions

: "${BACKUP_CONTAINER_URL:?required}"
: "${LDAP_BASE_DN:=dc=example,dc=org}"  # same default as entrypoint.sh
CONF=/var/lib/ldap/slapd.d
FILES=(config main accesslog)

blob() {  # blob <GET|PUT> <name> [curl args...]
    local method=$1 name=$2 base=${BACKUP_CONTAINER_URL%%\?*} query="" auth=()
    shift 2
    if [[ "$BACKUP_CONTAINER_URL" == *\?* ]]; then
        query="?${BACKUP_CONTAINER_URL#*\?}"
    else
        local token
        token=$(curl -fsS --max-time 30 -H "X-IDENTITY-HEADER: $IDENTITY_HEADER" \
            "$IDENTITY_ENDPOINT?api-version=2019-08-01&resource=https://storage.azure.com/${AZURE_CLIENT_ID:+&client_id=$AZURE_CLIENT_ID}" |
            sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')
        auth=(-H "Authorization: Bearer $token")
    fi
    curl -fsS --retry 3 --max-time 300 -X "$method" -H "x-ms-version: 2023-11-03" "${auth[@]}" "$@" "$base/$name$query"
}

dump() {  # dump <file stem> <slapcat selector...>
    slapcat -F "$CONF" "${@:2}" -l "$work/$1.ldif"
}

backup() {
    local stamp f
    stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
    dump config -n 0
    dump main -b "$LDAP_BASE_DN"
    dump accesslog -b cn=accesslog
    # Prove the dump restores: load it into a scratch directory, with the
    # database paths moved there so the live files are never opened.
    mkdir -p "$work/check/slapd.d" "$work/check/data" "$work/check/accesslog"
    sed "s#^olcDbDirectory: /var/lib/ldap/#olcDbDirectory: $work/check/#" "$work/config.ldif" > "$work/check.ldif"
    slapadd -n 0 -F "$work/check/slapd.d" -l "$work/check.ldif"
    slapadd -F "$work/check/slapd.d" -b "$LDAP_BASE_DN" -l "$work/main.ldif"
    slapadd -F "$work/check/slapd.d" -b cn=accesslog -l "$work/accesslog.ldif"
    cd "$work"
    for f in "${FILES[@]}"; do
        gzip -n "$f.ldif"
        blob PUT "$stamp/$f.ldif.gz" -H "x-ms-blob-type: BlockBlob" --data-binary "@$f.ldif.gz"
    done
    sha256sum ./*.ldif.gz > SHA256SUMS
    blob PUT "$stamp/SHA256SUMS" -H "x-ms-blob-type: BlockBlob" --data-binary @SHA256SUMS
    echo "Directory backup $stamp uploaded"
}

restore() {
    local stamp=${1:?usage: directory-backup restore <stamp>} f
    if [[ -e "$CONF" || -n "$(ls -A /var/lib/ldap/data /var/lib/ldap/accesslog 2>/dev/null)" ]]; then
        echo "Refusing to restore: /var/lib/ldap already holds a directory" >&2
        exit 1
    fi
    # A failed restore leaves nothing behind, so the next start does not run
    # a half-loaded directory.
    trap 'rm -rf "$CONF" /var/lib/ldap/data /var/lib/ldap/accesslog' ERR
    cd "$work"
    blob GET "$stamp/SHA256SUMS" -o SHA256SUMS
    for f in "${FILES[@]}"; do
        blob GET "$stamp/$f.ldif.gz" -o "$f.ldif.gz"
    done
    [[ $(wc -l < SHA256SUMS) -eq ${#FILES[@]} ]] || { echo "SHA256SUMS does not list every file" >&2; false; }
    sha256sum -c --quiet SHA256SUMS
    gunzip ./*.ldif.gz
    mkdir -p "$CONF" /var/lib/ldap/data /var/lib/ldap/accesslog
    slapadd -q -n 0 -F "$CONF" -l config.ldif
    slapadd -q -F "$CONF" -b "$LDAP_BASE_DN" -l main.ldif
    slapadd -q -F "$CONF" -b cn=accesslog -l accesslog.ldif
    trap - ERR
    echo "Directory restored from backup $stamp"
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
case "${1:-}" in
    backup) backup ;;
    restore) restore "${2:-}" ;;
    *) echo "usage: directory-backup backup | restore <stamp>" >&2; exit 2 ;;
esac
