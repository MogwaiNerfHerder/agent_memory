"""
migrate_v0_14_0_cross_client_identity.py - Link the same real person across
multiple clients' isolated entity graphs.

Problem this closes: `entity` is scoped per client_id and deduped within that
scope only (UNIQUE(client_id, type, canonical_name)) -- by design, so one
client's graph doesn't casually leak another client's specifics. But a real
person can legitimately appear across many clients' graphs: Cortado staff
(a consultant staffed on 10 engagements) and cross-portfolio contacts (a PE
firm partner who sits across several of their portfolio companies). Without
this, retroactively attributing shared-client data correctly means either (a)
leaving that person's data stuck under a shared/fake bucket forever, or (b)
cloning them into a disconnected, unlinked copy per client -- losing the fact
that it's the same Dan Perry / Jessica Pearson every time.

Fix: entities stay genuinely per-client (query isolation is preserved -- a
query scoped to one client's entities still only sees that client's own row),
but each per-client row can optionally point at one shared global_identity
row. A cross-client query (an internal Cortado use case, e.g. "what has
Jessica touched across all her engagements") joins through global_identity;
any single client's own isolated queries never need to.

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

    print(f"Migrating {args.path} -> v0.14.0 (cross-client identity linking)...")

    if not table_exists(conn, "global_identity"):
        conn.execute("""
            CREATE TABLE global_identity (
                global_identity_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_kind       TEXT NOT NULL,
                canonical_name      TEXT NOT NULL,
                cortado_staff_email TEXT,
                notes               TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (identity_kind IN ('cortado_staff', 'cross_portfolio_contact')),
                UNIQUE (identity_kind, canonical_name)
            )
        """)
        print("  global_identity: created")
    else:
        print("  global_identity: already present")

    if not column_exists(conn, "entity", "global_identity_id"):
        conn.execute(
            "ALTER TABLE entity ADD COLUMN global_identity_id INTEGER REFERENCES global_identity(global_identity_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_global_identity ON entity(global_identity_id)")
        print("  entity.global_identity_id: added")
    else:
        print("  entity.global_identity_id: already present")

    conn.commit()

    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("table", "global_identity", None,
             "One row per real person who legitimately spans multiple clients' entity "
             "graphs: Cortado staff (identity_kind='cortado_staff') or a cross-portfolio "
             "contact, e.g. a PE firm partner (identity_kind='cross_portfolio_contact'). "
             "NOT used for genuine client-side people -- those stay entirely client-scoped, "
             "same as before this migration."),
            ("column", "entity", "global_identity_id",
             "Nullable. Set only when this entity is known to be the same real person as "
             "other clients' entity rows (see global_identity). A query scoped to one "
             "client's entities never needs to join through this; it exists for internal "
             "cross-client queries only."),
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("cross-client identity",
         "A real person (Cortado staff, or a cross-portfolio contact) who appears in more "
         "than one client's isolated entity graph. Each client still gets its own entity "
         "row (query isolation preserved) but all of them point at one shared "
         "global_identity row, so 'is this the same Jessica' is answerable without merging "
         "the clients' graphs together.",
         "Jessica Pearson, staffed across 5 clients, has 5 entity rows (one per client), "
         "all sharing one global_identity_id -- not 5 disconnected people who happen to "
         "share a name, and not one entity awkwardly shared across 5 clients' graphs."),
    )
    conn.commit()

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.14.0":
        print("  z_version already at v0.14.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES "
            "('v0.14.0', 'Add global_identity + entity.global_identity_id: link the same "
            "real person (Cortado staff, cross-portfolio contacts) across multiple clients '"
            "'isolated entity graphs without merging the graphs themselves')"
        )
        print("  bumped to v0.14.0")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
