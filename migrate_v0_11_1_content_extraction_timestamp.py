"""
migrate_v0_11_1_content_extraction_timestamp.py - Track when source content was last extracted.

Adds:
  source_document.content_extracted_at TIMESTAMP
  source_meeting.content_extracted_at  TIMESTAMP

Use case: a worker can compare modified_at (upstream) against content_extracted_at
(when we last processed) to find sources that need re-extraction.

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()
    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.11.1 (content_extracted_at)...")

    for tbl in ("source_document", "source_meeting"):
        if not column_exists(conn, tbl, "content_extracted_at"):
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN content_extracted_at TIMESTAMP")
            print(f"  {tbl}: added content_extracted_at")
        else:
            print(f"  {tbl}: content_extracted_at already present")

    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("column", "source_document", "content_extracted_at",
             "When extract_document.py last processed this file's content. NULL = never extracted. Compare against modified_at to detect stale extractions."),
            ("column", "source_meeting", "content_extracted_at",
             "When extract_meeting.py last processed this meeting's transcript. NULL = never extracted. Compare against occurred_at + a buffer to detect re-runs needed."),
        ],
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.11.1"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.11.1', 'Add content_extracted_at on source_document and source_meeting; detect stale extractions vs upstream modified_at')"
        )
        print("  bumped to v0.11.1")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
