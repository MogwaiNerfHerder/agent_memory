"""
migrate_v0_2_0_knowledge_web.py - Pivot to "knowledge web" model.

The principle: cortado (and other sources) are SOURCES, not SUBSTRATE.
Every fact about an entity — email, role, employer, project membership,
risk, stakeholder role — lives as an edge with citations, not as a
denormalized column. This lets meeting-derived inference supersede stale
source data while preserving full provenance.

Changes from v0.1.0:
  - source_meeting: drop summary, action_items, decisions, attendees, transcript_url
                    (cortado serves these fresh; we don't mirror)
  - source_document: drop title, doc_type, mime_type, content_url
                     (Box index serves these; we don't mirror)
  - edge: add object_literal_type (string|email|date|currency|int|url|...)
  - z_glossary: add 'knowledge web' principle entry
  - z_version: bump to v0.2.0

Idempotent: safe to re-run.

Usage:
    python migrate_v0_2_0_knowledge_web.py
    python migrate_v0_2_0_knowledge_web.py --path /path/to/agent_memory.db
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def drop_column_if_exists(conn, table, column):
    if column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        return True
    return False


def add_column_if_missing(conn, table, column, ddl):
    if not column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    return False


def upsert_schema(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema (object_type, object_name, column_name, description) VALUES (?,?,?,?)",
        rows,
    )


def upsert_glossary(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO z_glossary (term, definition, example_usage) VALUES (?,?,?)",
        rows,
    )


def already_at_version(conn, version):
    row = conn.execute(
        "SELECT version FROM z_version ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row and row[0] == version


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

    print(f"Migrating {args.path} -> v0.2.0 (knowledge web pivot)...")

    # --- source_meeting: strip mirrors of cortado fields ---------------------
    sm_dropped = []
    for col in ("summary", "action_items", "decisions", "attendees", "transcript_url"):
        if drop_column_if_exists(conn, "source_meeting", col):
            sm_dropped.append(col)
    if sm_dropped:
        print(f"  source_meeting: dropped {sm_dropped}")
    else:
        print("  source_meeting: already lean")

    # --- source_document: strip mirrors of Box index fields ------------------
    sd_dropped = []
    for col in ("title", "doc_type", "mime_type", "content_url"):
        if drop_column_if_exists(conn, "source_document", col):
            sd_dropped.append(col)
    if sd_dropped:
        print(f"  source_document: dropped {sd_dropped}")
    else:
        print("  source_document: already lean")

    # --- edge: add object_literal_type --------------------------------------
    if add_column_if_missing(conn, "edge", "object_literal_type", "TEXT"):
        print("  edge: added object_literal_type")
    else:
        print("  edge: object_literal_type already present")

    # --- z_schema doc updates ------------------------------------------------
    upsert_schema(conn, [
        ("table", "source_meeting", None,
         "Thin pointer to a meeting record (typically cortadogroup.ai). Fields like attendees / action_items / decisions / summary are NOT mirrored — fetch from cortado on demand. This row exists only as a citation target."),
        ("table", "source_document", None,
         "Thin pointer to a document (typically Box). Title/doc_type/mime_type are NOT mirrored — fetch from box-search index on demand. Citation target only."),
        ("column", "edge", "object_literal_type",
         "Type tag when object is a literal: string | email | date | currency | int | url | phone (extensible)"),
        ("column", "edge", "object_literal",
         "Literal value when object isn't an entity. Pair with object_literal_type for rendering and dedup."),
    ])
    print("  z_schema docs updated")

    # --- philosophy entry in glossary ---------------------------------------
    upsert_glossary(conn, [
        ("knowledge web",
         "Architectural principle: every fact about an entity lives as a (subject, predicate, object) edge with citations — never as a denormalized column on a fixed table. External sources (cortado, Box) seed edges and are cited; meeting-derived inference can supersede stale source data with full provenance preserved.",
         "Barry's job title is an edge (barry --has_role--> 'VP Sales'), not entity.title. Cortado seeds it cited to contact_guid; a transcript saying 'I'm CRO now' supersedes with a meeting citation."),
        ("substrate vs source",
         "Cortado / Box / cortadogroup.ai meeting platform are SOURCES. The agent_memory graph is SUBSTRATE. Sources may be stale or wrong; inference and corroboration update substrate edges, with citations preserving where each fact came from.",
         "If cortado lists Barry as VP Sales but three meetings refer to him as CRO, the graph holds both edges with timestamps; the bot answers from latest corroborated, but can show the discrepancy."),
        ("literal object",
         "An edge can point at either an entity (e.g., another person) or a literal value (string, email, date). Use object_literal + object_literal_type for literals; object_id is NULL in that case.",
         "(barry) --has_email--> object_literal='barry@hig.com', object_literal_type='email'"),
    ])
    print("  z_glossary updated with knowledge-web principle")

    # --- version bump --------------------------------------------------------
    if already_at_version(conn, "v0.2.0"):
        print("  z_version already at v0.2.0; skipping bump")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.2.0', 'Knowledge web pivot: source_meeting/source_document slim to citation pointers; edge gains object_literal_type; z_glossary documents knowledge-web principle')"
        )
        print("  bumped to v0.2.0")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
