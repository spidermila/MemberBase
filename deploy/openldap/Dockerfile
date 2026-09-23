# OpenLDAP for MemberBase: Debian's slapd package, run as the unprivileged
# openldap user, LDAPS and ldapi only. Configuration is rendered from the LDIF
# templates in ldif/ on first start (see entrypoint.sh).
FROM docker.io/library/debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends slapd ldap-utils openssl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && rm -rf /etc/ldap/slapd.d /var/lib/ldap/* \
    && mkdir -p /var/lib/ldap /var/run/slapd /certs \
    && chown -R openldap:openldap /var/lib/ldap /var/run/slapd /certs

COPY ldif/ /etc/ldap/memberbase/
COPY entrypoint.sh /entrypoint.sh

USER openldap
EXPOSE 1636
VOLUME ["/var/lib/ldap", "/certs"]
ENTRYPOINT ["/entrypoint.sh"]
