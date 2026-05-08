"""
migrate_v0_4_0_memory_entity.py - Add memory_entity join table.

Items of note (z_memory rows extracted from meetings/docs) need to be queryable
as a graph: "all observations about Hans Sherman", "all decisions touching
Project Violet". Polymorphic citation alone doesn't index efficiently for that.

memory_entity gives us:
  - Enforced FKs to both z_memory and entity (cascade delete)
  - Indexed lookup both directions
  - A 'role' tag so we can distinguish subject vs mentioned vs attendee

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


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

    print(f"Migrating {args.path} -> v0.4.0 (memory_entity join)...")

    if not table_exists(conn, "memory_entity"):
        conn.execute(
            """
            CREATE TABLE memory_entity (
                memory_entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL REFERENCES z_memory(memory_id) ON DELETE CASCADE,
                entity_id INTEGER NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'mentioned',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(memory_id, entity_id, role)
            )
            """
        )
        conn.execute("CREATE INDEX idx_memory_entity_memory ON memory_entity(memory_id)")
        conn.execute("CREATE INDEX idx_memory_entity_entity ON memory_entity(entity_id, role)")

        conn.execute(
            """
            CREATE TRIGGER trg_memory_entity_role_check
            BEFORE INSERT ON memory_entity
            WHEN NEW.role NOT IN ('subject','mentioned','attendee','about','blocker','champion')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid memory_entity.role');
            END
            """
        )
        print("  memory_entity created with FKs + indexes + role-check trigger")
    else:
        print("  memory_entity already exists; skipping create")

    # Schema docs
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("table", "memory_entity", None,
             "Join: links z_memory rows (items of note) to entities they're about. Enforced FK cascade. Indexed both ways for efficient 'all notes about X' queries."),
            ("column", "memory_entity", "memory_id", "FK to z_memory.memory_id (cascade)"),
            ("column", "memory_entity", "entity_id", "FK to entity.entity_id (cascade)"),
            ("column", "memory_entity", "role",
             "subject (the note is primarily about this entity) | mentioned | attendee | about | blocker | champion"),
        ],
    )

    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("item of note",
         "A free-text observation extracted from a source (meeting, document) that doesn't fit cleanly as a (subject, predicate, object) edge — decisions, action items, themes, recommendations, risks, open questions, soft observations. Stored as z_memory rows with embeddings; linked to relevant entities via memory_entity.",
         "'Hans raised channel coverage as a major red flag' is a memory row of type='observation' linked to (Hans, role=subject) and (Project Violet, role=mentioned)."),
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.4.0":
        print("  z_version already at v0.4.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.4.0', 'Add memory_entity join (FK-enforced) so items of note are queryable as a graph')"
        )
        print("  bumped to v0.4.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
