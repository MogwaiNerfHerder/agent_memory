"""
migrate_v0_3_1_nullable_citation_source_id.py - Rebuild citation table to make source_id nullable.

The original v0.1.0 schema declared source_id as INTEGER NOT NULL. v0.3.0 added
source_external_id but didn't relax the NOT NULL on source_id, so external
citations (no local FK) couldn't be inserted. SQLite needs a table rebuild
for this; ALTER TABLE can't drop NOT NULL.

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def column_is_not_null(conn, table, column):
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == column:
            return bool(row[3])
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        default=os.path.join(os.path.dirname(__file__), "agent_memory.db"),
    )
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = OFF")  # required for table rebuild

    print(f"Migrating {args.path} -> v0.3.1 (citation.source_id nullable)...")

    if not column_is_not_null(conn, "citation", "source_id"):
        print("  citation.source_id is already nullable; skipping rebuild")
    else:
        # Drop dependent triggers first
        for trig in ("trg_citation_kind_check", "trg_citation_source_required"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")

        conn.executescript(
            """
            CREATE TABLE citation_new (
                citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cited_kind TEXT NOT NULL,
                cited_id INTEGER NOT NULL,
                source_kind TEXT NOT NULL,
                source_id INTEGER,
                source_external_id TEXT,
                source_ts TIMESTAMP,
                quote TEXT,
                offset_start INTEGER,
                offset_end INTEGER,
                extracted_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cited_kind, cited_id, source_kind, source_id, offset_start, offset_end)
            );

            INSERT INTO citation_new (citation_id, cited_kind, cited_id, source_kind, source_id,
                                       source_external_id, source_ts, quote, offset_start, offset_end,
                                       extracted_by, created_at)
            SELECT citation_id, cited_kind, cited_id, source_kind, source_id,
                   source_external_id, source_ts, quote, offset_start, offset_end,
                   extracted_by, created_at
            FROM citation;

            DROP TABLE citation;
            ALTER TABLE citation_new RENAME TO citation;
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_citation_cited ON citation(cited_kind, cited_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_citation_source ON citation(source_kind, source_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_citation_external ON citation(source_kind, source_external_id)")

        # Recreate triggers
        conn.execute(
            """
            CREATE TRIGGER trg_citation_kind_check
            BEFORE INSERT ON citation
            WHEN NEW.cited_kind NOT IN ('memory','edge','entity')
              OR NEW.source_kind NOT IN ('meeting','document','manual','cortado','salesforce','asana','slack')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid citation kind');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_citation_source_required
            BEFORE INSERT ON citation
            WHEN NEW.source_kind != 'manual'
              AND NEW.source_id IS NULL
              AND (NEW.source_external_id IS NULL OR NEW.source_external_id = '')
            BEGIN
                SELECT RAISE(FAIL, 'Non-manual citation requires source_id or source_external_id');
            END
            """
        )
        print("  citation table rebuilt; source_id now nullable; triggers + indexes restored")

    conn.execute("PRAGMA foreign_keys = ON")

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.3.1":
        print("  z_version already at v0.3.1")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.3.1', 'citation.source_id made nullable via table rebuild')"
        )
        print("  bumped to v0.3.1")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
