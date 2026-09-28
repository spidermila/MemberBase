#!/bin/bash
# Look into the directory backups in Blob storage (az CLI, signed in with an
# Entra account that has Storage Blob Data Reader on the container).
#   directory-backups.sh list                       complete backups, oldest first
#   directory-backups.sh show <stamp> [main|accesslog|config]
#                                                   print one LDIF (pipe to grep)
#   directory-backups.sh download <stamp> [dir]     fetch and verify a backup
# BACKUP_ACCOUNT is the storage account; BACKUP_CONTAINER defaults to directory.
# Restoring is done by the openldap container itself: see DEVOPS.md.
set -euo pipefail

: "${BACKUP_ACCOUNT:?set BACKUP_ACCOUNT to the storage account name}"
CONTAINER=${BACKUP_CONTAINER:-directory}

az_blob() {
    az storage blob "$@" --account-name "$BACKUP_ACCOUNT" --container-name "$CONTAINER" \
        --auth-mode login --only-show-errors
}

case "${1:-}" in
    list)
        # A backup is complete once its SHA256SUMS is uploaded (always last).
        az_blob list --num-results '*' --query "[?ends_with(name, '/SHA256SUMS')].name" -o tsv |
            sed 's#/SHA256SUMS$##' | sort
        ;;
    show)
        stamp=${2:?usage: show <stamp> [main|accesslog|config]}
        tmp=$(mktemp)
        trap 'rm -f "$tmp"' EXIT
        az_blob download --name "$stamp/${3:-main}.ldif.gz" --file "$tmp" -o none
        zcat "$tmp"
        ;;
    download)
        stamp=${2:?usage: download <stamp> [dir]}
        dir=${3:-directory-backup-$stamp}
        mkdir -p "$dir"
        az storage blob download-batch --account-name "$BACKUP_ACCOUNT" --source "$CONTAINER" \
            --auth-mode login --only-show-errors --pattern "$stamp/*" --destination "$dir" -o none
        (cd "$dir/$stamp" && sha256sum -c SHA256SUMS)
        echo "Backup $stamp verified in $dir/$stamp (personal data: delete it when done)"
        ;;
    *)
        sed -n '2,9p' "$0" >&2
        exit 2
        ;;
esac
