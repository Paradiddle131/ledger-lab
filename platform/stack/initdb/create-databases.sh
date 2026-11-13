#!/bin/sh
# Run by the PostgreSQL image's entrypoint once, when the server starts on an empty volume:
# creates each database LEDGER_LAB_DATABASES names (compose.yaml), owned by the stack's user.
set -eu

for name in $LEDGER_LAB_DATABASES; do
	# Through stdin, where psql quotes the name as an identifier (`-c` would not interpolate it).
	echo 'CREATE DATABASE :"name";' |
		psql -v ON_ERROR_STOP=1 -v name="$name" --username "$POSTGRES_USER" --dbname postgres
done
