#!/bin/bash
# Runs once, on first initialization of the Postgres data volume, as the
# container superuser. Creates the §12 S4 role split:
#
#   mc_migrate  — owns the mission_control database and all DDL (Alembic)
#   mc_app      — runtime role for the API; table grants are applied by the
#                 baseline migration (audit_log ends up SELECT+INSERT only)
#   mc_readonly — read-only role for adapters/reporting where possible
#
# Passwords come from the container environment (docker-compose.yml ←
# .env / /etc/mission-control/env). Keep them alphanumeric: values are
# interpolated into the SQL below.
set -euo pipefail

: "${MC_DB_MIGRATE_PASSWORD:?MC_DB_MIGRATE_PASSWORD is required}"
: "${MC_DB_APP_PASSWORD:?MC_DB_APP_PASSWORD is required}"
: "${MC_DB_READONLY_PASSWORD:?MC_DB_READONLY_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE mc_migrate LOGIN PASSWORD '${MC_DB_MIGRATE_PASSWORD}';
    CREATE ROLE mc_app LOGIN PASSWORD '${MC_DB_APP_PASSWORD}';
    CREATE ROLE mc_readonly LOGIN PASSWORD '${MC_DB_READONLY_PASSWORD}';
    CREATE DATABASE mission_control OWNER mc_migrate;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname mission_control <<-EOSQL
    -- The migration role owns the schema; nobody else may CREATE in it.
    ALTER SCHEMA public OWNER TO mc_migrate;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    GRANT USAGE ON SCHEMA public TO mc_app, mc_readonly;
EOSQL

echo "mission-control roles and database created"
