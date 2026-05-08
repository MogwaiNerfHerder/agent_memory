"""
migrate_v0_7_0_source_urls.py - Add canonical URL columns to source_meeting and source_document.

Citations should be actionable. Today they carry an external_id (cortado guid,
box_file_id) but no URL — a human reading a citation can't click through.

Adds:
  source_meeting.url   TEXT  -- canonical link to the meeting in cortadogroup.ai
  source_document.url  TEXT  -- canonical link to the Box file

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.7.0 (source URLs)...")

    if not column_exists(conn, "source_meeting", "url"):
        conn.execute("ALTER TABLE source_meeting ADD COLUMN url TEXT")
        print("  source_meeting: added url column")
    else:
        print("  source_meeting: url already present")

    if not column_exists(conn, "source_document", "url"):
        conn.execute("ALTER TABLE source_document ADD COLUMN url TEXT")
        print("  source_document: added url column")
    else:
        print("  source_document: url already present")

    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("column", "source_meeting", "url",
             "Canonical clickable URL for the meeting (e.g. https://cg.cortadogroup.ai/meetings/<guid>). NULL if pattern not yet known."),
            ("column", "source_document", "url",
             "Canonical clickable URL for the document (e.g. https://cortadogroup.app.box.com/file/<box_file_id>)."),
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("source url",
         "Each source_meeting and source_document row carries a `url` field — a clickable link a human can follow back to the original record. Citations expose this URL when rendering, so a bot answer like 'we discussed X in this meeting [url]' lands as an actionable link.",
         "source_meeting.url='https://cg.cortadogroup.ai/meetings/113ccc9b-...'; source_document.url='https://cortadogroup.app.box.com/file/378594788635'"),
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.7.0":
        print("  z_version already at v0.7.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.7.0', 'Add url columns to source_meeting and source_document for clickable citation traceability')"
        )
        print("  bumped to v0.7.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
