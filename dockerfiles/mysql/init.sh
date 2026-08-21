#!/bin/bash
# Runs inside the MySQL container after the database and user are created by Docker entrypoint.
# Imports the VAG catalog schema and data from the dump files.
set -e

DB="${MYSQL_DATABASE:-vag_petka}"
ROOT_PASS="${MYSQL_ROOT_PASSWORD:-rootpassword}"
MYSQL_CMD="mysql -u root -p${ROOT_PASS}"

echo "[init] Importing schema: create_db.sql ..."
$MYSQL_CMD "$DB" < /dumps/create_db.sql
echo "[init] Schema imported."

echo "[init] Importing data: vag_dump.sql (this may take several minutes) ..."
$MYSQL_CMD "$DB" < /dumps/vag_dump.sql
echo "[init] Data imported. VAG catalog database is ready."
