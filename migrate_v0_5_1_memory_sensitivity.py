"""
migrate_v0_5_1_memory_sensitivity.py - Add sensitivity tier to z_memory.

Notes (z_memory rows) extracted from meetings can carry the same sensitivity
classification as edges (routine | sensitive | hr_grade). Bot retrieval needs
to filter by this tier — without it, HR-grade observations sit at the same
visibility level as routine logistics.

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def trigger_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.5.1 (z_memory.sensitivity)...")

    if not column_exists(conn, "z_memory", "sensitivity"):
        conn.execute("ALTER TABLE z_memory ADD COLUMN sensitivity TEXT DEFAULT 'routine'")
        print("  z_memory: added sensitivity column (default 'routine')")
    else:
        print("  z_memory: sensitivity already present")

    if not trigger_exists(conn, "trg_z_memory_sensitivity_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_z_memory_sensitivity_check
            BEFORE INSERT ON z_memory
            WHEN NEW.sensitivity NOT IN ('routine','sensitive','hr_grade')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid z_memory.sensitivity: must be routine, sensitive, or hr_grade');
            END
            """
        )
        print("  z_memory: sensitivity-check trigger created")
    else:
        print("  z_memory: sensitivity-check trigger already present")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_sensitivity ON z_memory(client_id, sensitivity, importance DESC)")

    conn.execute(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        ("column", "z_memory", "sensitivity",
         "Access tier: routine | sensitive | hr_grade. Bots filter by this. routine for ops/logistics; sensitive for interpersonal/preferences; hr_grade for confidentiality/personnel risk."),
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.5.1":
        print("  z_version already at v0.5.1")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.5.1', 'Add z_memory.sensitivity column with check trigger; tier notes by routine/sensitive/hr_grade')"
        )
        print("  bumped to v0.5.1")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
