# Production Keycloak: built for SQL Server / Azure SQL, with the crc realm
# (imported on the first start only) and the Czech login theme baked in.
# The dev stacks run the official image with the realm and theme mounted.
FROM quay.io/keycloak/keycloak:26.7.4 AS build
ENV KC_DB=mssql \
    KC_TRANSACTION_XA_ENABLED=false \
    KC_HEALTH_ENABLED=true \
    KC_VAULT=file
RUN /opt/keycloak/bin/kc.sh build && mkdir /opt/keycloak/vault

FROM quay.io/keycloak/keycloak:26.7.4
COPY --from=build /opt/keycloak/ /opt/keycloak/
COPY themes/crc /opt/keycloak/themes/crc
COPY realm-crc.json /opt/keycloak/data/import/realm-crc.json
# Secrets referenced as ${vault.<key>} in the realm are files named
# crc_<key> here, mounted from Container Apps secrets. Empty by default:
# Keycloak refuses to start if the directory is missing.
ENV KC_VAULT_DIR=/opt/keycloak/vault
CMD ["start", "--optimized", "--import-realm"]
