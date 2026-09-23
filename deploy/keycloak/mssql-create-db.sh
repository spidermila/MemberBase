#!/bin/bash
# Create Keycloak's database on SQL Server / Azure SQL, as the Keycloak docs
# recommend: UTF-8 collation (Czech names) and READ_COMMITTED_SNAPSHOT.
# Idempotent. Used by MedCover's dev stack against its MSSQL container.
#
# Environment: MSSQL_HOST, MSSQL_SA_PASSWORD, KEYCLOAK_DB_PASSWORD,
#              SQLCMD (default /opt/mssql-tools18/bin/sqlcmd)
set -euo pipefail
SQLCMD="${SQLCMD:-/opt/mssql-tools18/bin/sqlcmd}"
sql() { "$SQLCMD" -S "$MSSQL_HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -b "$@"; }

for i in $(seq 1 30); do
    sql -Q "SELECT 1" >/dev/null 2>&1 && break
    echo "Waiting for SQL Server ($i/30)..."; sleep 2
done

sql -Q "
IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'keycloak')
BEGIN
    CREATE DATABASE keycloak COLLATE Czech_100_CI_AS_SC_UTF8;
    ALTER DATABASE keycloak SET READ_COMMITTED_SNAPSHOT ON;
END
IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = 'keycloak')
    CREATE LOGIN keycloak WITH PASSWORD = '${KEYCLOAK_DB_PASSWORD}';
"
sql -d keycloak -Q "
IF NOT EXISTS (SELECT name FROM sys.database_principals WHERE name = 'keycloak')
BEGIN
    CREATE USER keycloak FOR LOGIN keycloak;
    ALTER ROLE db_owner ADD MEMBER keycloak;
END
"
echo "Keycloak database ready."
