#!/bin/sh
# Prove the off-site backup can be restored: restore the newest backup of
# the live database into a scratch folder, have Litestream run SQLite's
# full integrity check on it, then compare its row counts with the live
# database's. Nothing live is touched. Exits non-zero if the restore or
# the check fails, or if any table's count differs. Run as root on the
# server:
#
#     sh /opt/brewerypi/scripts/check_backup.sh [DB_PATH]
#
# See "Backups" in docs/deploy-mcp-hetzner.md.
set -eu

DB="${1:-/opt/brewerypi/app.db}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Restoring the newest backup of $DB ..."
litestream restore -integrity-check full -o "$WORK/restored.db" "$DB"
echo "Restored, and the integrity check passed."
echo

python3 -I - "$DB" "$WORK/restored.db" <<'PY'
import sqlite3
import sys

live_path, restored_path = sys.argv[1], sys.argv[2]
live = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
restored = sqlite3.connect(restored_path)


def tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [name for (name,) in rows]


def count(conn, table, present):
    if table not in present:
        return None
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


live_tables, restored_tables = tables(live), tables(restored)
differ = 0
print(f"{'table':32} {'live':>10} {'backup':>10}")
for table in sorted(set(live_tables) | set(restored_tables)):
    a = count(live, table, live_tables)
    b = count(restored, table, restored_tables)
    mark = ""
    if a != b:
        differ += 1
        mark = "  <- differs"
    shown_a = "missing" if a is None else a
    shown_b = "missing" if b is None else b
    print(f"{table:32} {shown_a:>10} {shown_b:>10}{mark}")

print()
if differ:
    print(
        f"{differ} table(s) differ. A reading saved in the last few seconds"
        " can explain one; run this again. If they still differ, the backup"
        " is behind: check 'journalctl -u litestream'."
    )
    sys.exit(1)
else:
    print("Every table matches the live database.")
PY
