#!/bin/bash
# First start: create a TLS certificate (unless one is mounted), render
# cn=config and the initial tree from ldif/, load them with slapadd.
# Every start: run slapd on LDAPS (port 1636) and the local ldapi socket.
set -euo pipefail

: "${LDAP_BASE_DN:=dc=example,dc=org}"
: "${LDAP_OWN_BRANCH_LEVEL:=contact}"
: "${LDAP_LOG_LEVEL:=none}"
: "${LDAP_TLS_HOSTNAMES:=openldap,localhost}"
TEMPLATES=/etc/ldap/memberbase
CONF=/var/lib/ldap/slapd.d
ROOT="gidNumber=$(id -g)+uidNumber=$(id -u),cn=peercred,cn=external,cn=auth"
BASE_RE=$(printf '%s' "$LDAP_BASE_DN" | sed 's/[.[\*^$()+?{|]/\\&/g')

if [[ ! -f /certs/tls.crt ]]; then
    echo "Creating a self-signed CA and server certificate in /certs"
    san=$(printf 'DNS:%s,' ${LDAP_TLS_HOSTNAMES//,/ })
    openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=MemberBase LDAP CA" \
        -keyout /certs/ca.key -out /certs/ca.crt 2>/dev/null
    openssl req -newkey rsa:3072 -nodes -subj "/CN=${LDAP_TLS_HOSTNAMES%%,*}" \
        -keyout /certs/tls.key -out /tmp/tls.csr 2>/dev/null
    openssl x509 -req -in /tmp/tls.csr -CA /certs/ca.crt -CAkey /certs/ca.key -CAcreateserial \
        -days 3650 -extfile <(printf 'subjectAltName=%s' "${san%,}") -out /certs/tls.crt 2>/dev/null
    chmod 600 /certs/ca.key /certs/tls.key
fi

hash_password() {
    slappasswd -o module-load=argon2 -h '{ARGON2}' -s "$1"
}

# Replace {{NAME}} with the value of the environment variable R_NAME.
render() {
    perl -e 'local $/; my $s = <STDIN>; $s =~ s/\{\{(\w+)\}\}/exists $ENV{"R_$1"} ? $ENV{"R_$1"} : die "unset: $1\n"/ge; print $s;'
}

if [[ ! -f "$CONF/cn=config.ldif" ]]; then
    echo "Initialising directory ${LDAP_BASE_DN}"
    : "${LDAP_KEYCLOAK_PASSWORD:?required on first start}"
    : "${LDAP_MEDCOVER_SYNC_PASSWORD:?required on first start}"
    : "${LDAP_MEMBERBASE_PASSWORD:?required on first start}"
    : "${BOOTSTRAP_ADMIN_EMAIL:?required on first start}"
    mkdir -p "$CONF" /var/lib/ldap/data /var/lib/ldap/accesslog

    own_branch=""
    if [[ "$LDAP_OWN_BRANCH_LEVEL" == "contact" ]]; then
        own_branch="  by dn.regex=\"^uid=[^,]+,ou=\$2,ou=units,${BASE_RE}\$\" read"
    fi
    first_rdn=${LDAP_BASE_DN%%,*}
    export R_DC=${first_rdn#dc=}
    export R_BASE_DN="$LDAP_BASE_DN" R_BASE_RE="$BASE_RE" R_ROOT="$ROOT" R_LOG_LEVEL="$LDAP_LOG_LEVEL"
    export R_OWN_BRANCH_CONTACT="$own_branch"
    mc_users=""  # the MedCover roles, as in people.MEDCOVER_ROLES
    for role in admin coordinator member viewer debriefing-manager; do
        mc_users="${mc_users:+$mc_users | }[cn=$role,ou=roles,ou=medcover,ou=apps,${LDAP_BASE_DN}]/member"
    done
    export R_MEDCOVER_USERS="($mc_users)"
    export R_ACCESS_RULES
    R_ACCESS_RULES=$(render < "$TEMPLATES/access-rules.ldif" | grep -v '^#' | grep -v '^$')
    export R_DISTRICT_ID="${LDAP_DISTRICT_ID:-$(cat /proc/sys/kernel/random/uuid)}"
    export R_UNIT_ID; R_UNIT_ID=$(cat /proc/sys/kernel/random/uuid)
    export R_ADMIN_ID; R_ADMIN_ID=$(cat /proc/sys/kernel/random/uuid)
    export R_UNIT_SLUG="${BOOTSTRAP_UNIT_SLUG:-ukazkova}"
    export R_UNIT_NAME="${BOOTSTRAP_UNIT_NAME:-Ukázková místní skupina}"
    export R_ADMIN_EMAIL="$BOOTSTRAP_ADMIN_EMAIL"
    export R_ADMIN_NAME="${BOOTSTRAP_ADMIN_NAME:-Správce}"
    export R_KEYCLOAK_PW; R_KEYCLOAK_PW=$(hash_password "$LDAP_KEYCLOAK_PASSWORD")
    export R_SYNC_PW; R_SYNC_PW=$(hash_password "$LDAP_MEDCOVER_SYNC_PASSWORD")
    export R_MEMBERBASE_PW; R_MEMBERBASE_PW=$(hash_password "$LDAP_MEMBERBASE_PASSWORD")
    admin_pw_line=""
    if [[ -n "${BOOTSTRAP_ADMIN_PASSWORD:-}" ]]; then
        admin_pw_line="userPassword: $(hash_password "$BOOTSTRAP_ADMIN_PASSWORD")"
    fi
    export R_ADMIN_PASSWORD_LINE="$admin_pw_line"

    render < "$TEMPLATES/config.ldif" > /tmp/config.ldif
    render < "$TEMPLATES/tree.ldif" > /tmp/tree.ldif
    slapadd -n 0 -F "$CONF" -l /tmp/config.ldif
    slapadd -F "$CONF" -b "$LDAP_BASE_DN" -l /tmp/tree.ldif
    rm -f /tmp/config.ldif /tmp/tree.ldif
fi

exec slapd -d "${LDAP_DEBUG:-0}" -F "$CONF" -h "ldaps://:1636/ ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/"
