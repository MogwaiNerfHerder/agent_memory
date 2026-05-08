"""
migrate_v0_3_0_external_citations.py - Allow citations to reference external systems
(cortado records, salesforce records, asana, etc.) by guid instead of local FK.

Changes:
  - citation.source_external_id TEXT (new)
  - citation.source_id is now nullable in spirit (still INTEGER, just allowed NULL when source_external_id is used)
  - trg_citation_kind_check: replace; allow source_kind in (meeting, document, manual, cortado, salesforce, asana, slack)
  - z_glossary: 'external citation' principle entry

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def trigger_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    return row is not None


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
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.3.0 (external citations)...")

    # --- citation.source_external_id ----------------------------------------
    if not column_exists(conn, "citation", "source_external_id"):
        conn.execute("ALTER TABLE citation ADD COLUMN source_external_id TEXT")
        print("  citation: added source_external_id")
    else:
        print("  citation: source_external_id already present")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citation_external ON citation(source_kind, source_external_id)"
    )

    # --- replace source_kind trigger -----------------------------------------
    if trigger_exists(conn, "trg_citation_kind_check"):
        conn.execute("DROP TRIGGER trg_citation_kind_check")
    conn.execute(
        """
        CREATE TRIGGER trg_citation_kind_check
        BEFORE INSERT ON citation
        WHEN NEW.cited_kind NOT IN ('memory','edge','entity')
          OR NEW.source_kind NOT IN ('meeting','document','manual','cortado','salesforce','asana','slack')
        BEGIN
            SELECT RAISE(FAIL, 'Invalid citation kind: cited_kind in (memory,edge,entity); source_kind in (meeting,document,manual,cortado,salesforce,asana,slack)');
        END
        """
    )
    print("  citation: trigger updated (source_kind expanded)")

    # require either source_id or source_external_id (not both NULL) unless manual
    if not trigger_exists(conn, "trg_citation_source_required"):
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
        print("  citation: source_required trigger added")
    else:
        print("  citation: source_required trigger already present")

    # --- z_schema doc updates ------------------------------------------------
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("column", "citation", "source_external_id",
             "External system identifier (cortado guid, salesforce id, asana gid, slack ts) when source_kind references an external system rather than a local source_meeting/source_document row."),
            ("column", "citation", "source_kind",
             "meeting | document | manual | cortado | salesforce | asana | slack"),
        ],
    )
    print("  z_schema docs updated")

    # --- glossary -----------------------------------------------------------
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("external citation",
         "Citation to a record in an external system (cortado, salesforce, asana, slack) by external guid rather than local FK. Used when the source isn't a transcript or document we mirror locally — we don't mirror cortado contact records, we cite them.",
         "(barry) --employed_by--> (hig) cited as source_kind='cortado', source_external_id='<contact_guid>'"),
    )
    print("  z_glossary updated")

    # --- version bump --------------------------------------------------------
    row = conn.execute(
        "SELECT version FROM z_version ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row[0] == "v0.3.0":
        print("  z_version already at v0.3.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.3.0', 'External citations: citation.source_external_id; source_kind expanded to include cortado, salesforce, asana, slack')"
        )
        print("  bumped to v0.3.0")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
