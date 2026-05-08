"""
migrate_v0_9_0_lookup_tables.py - Replace hardcoded enum CHECK triggers with FK-backed lookup tables.

For every column that today has a CHECK trigger restricting its values, create a
`lookup_<concept>` table holding the valid values, then rebuild the parent table
to enforce the constraint via FK instead of trigger.

Lookup tables created (per-category):
    lookup_alias_kind, lookup_edge_confidence, lookup_edge_sensitivity,
    lookup_edge_status, lookup_citation_kind, lookup_citation_source,
    lookup_attribution_kind, lookup_attribution_source, lookup_drift_status,
    lookup_memory_role, lookup_predicate_status, lookup_predicate_cardinality,
    lookup_identity_confidence, lookup_review_status

Each lookup row carries: (value PK, description, status, superseded_by, created_at).
status defaults to 'approved'; deprecated values stay in the table with status='deprecated'
and `superseded_by` pointing at the replacement.

Parent tables rebuilt to add FK constraints:
    predicate (status, cardinality)
    edge (status, confidence, sensitivity)
    citation (cited_kind, source_kind)
    attribution_event (target_kind, source, confidence)
    memory_entity (role)
    drift_hypothesis (status)
    z_memory (sensitivity)
    entity_alias (alias_kind, confidence)
    source_meeting (attribution_source, attribution_confidence)
    source_document (attribution_source, attribution_confidence)

Idempotent: re-running on an already-migrated db is a no-op.

NOTE: SQLite needs a table rebuild to add a FK constraint to an existing table.
This migration does that rebuild for each parent. Foreign keys are disabled
during the rebuild, then re-enabled.
"""

import argparse
import os
import sqlite3
import sys


# ---------------------------------------------------------------------------
# Lookup table specifications
# ---------------------------------------------------------------------------

# Each entry: (table_name, [(value, description), ...])
LOOKUPS = {
    "lookup_alias_kind": [
        ("name",                  "Canonical/preferred display name for the entity"),
        ("nickname",              "Casual or affectionate alternate name"),
        ("formal_name",           "Formal full name (e.g. 'Steven Wastie' vs nickname 'Steve')"),
        ("codename",              "Project codename used in confidential contexts"),
        ("initials",              "Short letter form (BM, JD)"),
        ("transcription_drift",   "Mishearing/typo of canonical that arose in source transcription"),
        ("role_descriptor",       "Descriptive label for the role this entity plays (e.g. 'the target', 'their CEO')"),
        ("slug",                  "URL-safe short identifier"),
        ("sfid",                  "Salesforce 18-char ID"),
        ("cortado_account_guid",  "Cortado Platform account GUID"),
        ("cortado_contact_guid",  "Cortado Platform contact GUID"),
        ("cortado_staff_guid",    "Cortado Platform staff GUID"),
        ("cortado_project_guid",  "Cortado Platform project GUID"),
        ("box_folder_id",         "Box folder numeric ID"),
        ("box_file_id",           "Box file numeric ID"),
        ("slack_user_id",         "Slack user identifier (Uxxxxx)"),
        ("asana_gid",             "Asana global identifier"),
    ],
    "lookup_edge_confidence": [
        ("factual",  "Procedural ground truth (X attended, Y was the project)"),
        ("stated",   "Speaker/source stated the fact explicitly"),
        ("implied",  "Clear inference from context, multiple cues"),
        ("pattern",  "Corroborated across 3+ sources/meetings"),
        ("confirmed","Strongly corroborated, 5+ sources"),
    ],
    "lookup_edge_sensitivity": [
        ("routine",  "Professional facts, ops, standard project data"),
        ("sensitive","Interpersonal observations, dispositions, personal context"),
        ("hr_grade", "Personnel risk, confidentiality, conflicts of interest"),
    ],
    "lookup_edge_status": [
        ("active",     "Currently true / authoritative"),
        ("superseded", "Replaced by a newer edge (see supersedes_id chain)"),
        ("retracted",  "Withdrawn — was incorrect, no replacement"),
    ],
    "lookup_citation_kind": [
        ("memory", "Citation supports a z_memory row"),
        ("edge",   "Citation supports an edge row"),
        ("entity", "Citation supports an entity (rare; usually for first-mention provenance)"),
    ],
    "lookup_citation_source": [
        ("meeting",     "Cortado Platform meeting (cited via source_meeting)"),
        ("document",    "Box / other document (cited via source_document)"),
        ("manual",      "Human assertion or seed-script attribution"),
        ("cortado",     "Cortado Platform structured data (account/project/contact records)"),
        ("salesforce",  "Salesforce structured data"),
        ("asana",       "Asana task / board"),
        ("slack",       "Slack message"),
    ],
    "lookup_attribution_kind": [
        ("memory",          "Re-attribution of a z_memory row"),
        ("source_meeting",  "Re-attribution of a meeting source row"),
        ("source_document", "Re-attribution of a document source row"),
        ("edge",            "Re-attribution of an edge"),
        ("entity",          "Re-attribution of an entity (esp. for cross-tenant moves)"),
    ],
    "lookup_attribution_source": [
        ("box_folder",         "Inferred from Box folder path"),
        ("attendee_domain",    "Inferred from meeting attendee email domain"),
        ("platform_tag",       "Tagged in source platform (cortado account, etc.)"),
        ("content_inference",  "LLM inference from content"),
        ("manual",             "Human assertion"),
        ("cortado_account",    "Linked via cortado account guid"),
    ],
    "lookup_drift_status": [
        ("pending",  "Awaiting human review"),
        ("approved", "Promoted to a real transcription_drift alias"),
        ("rejected", "Confirmed NOT a drift"),
    ],
    "lookup_memory_role": [
        ("subject",   "The note is primarily about this entity"),
        ("mentioned", "Entity is mentioned, not the focus"),
        ("attendee",  "Entity attended the event the note describes"),
        ("about",     "Note discusses this entity's behavior / state"),
        ("blocker",   "Entity is a blocker for the action item / decision"),
        ("champion",  "Entity is a champion / advocate of the topic"),
    ],
    "lookup_predicate_status": [
        ("proposed",   "Suggested by extractor; awaiting human approval"),
        ("approved",   "In the official vocabulary"),
        ("deprecated", "No longer used; new edges should use superseded_by"),
    ],
    "lookup_predicate_cardinality": [
        ("single_valued", "At most one active edge per (subject, predicate)"),
        ("multi_valued",  "Many active edges expected (collections)"),
    ],
    "lookup_identity_confidence": [
        ("certain",  "Identity is definitive (system label, explicit statement)"),
        ("likely",   "Strong inference but not authoritative"),
        ("guessed",  "Working hypothesis, low confidence — may be wrong"),
    ],
    "lookup_review_status": [
        ("pending",  "Awaiting human review"),
        ("approved", "Confirmed correct"),
        ("rejected", "Confirmed incorrect"),
    ],
}


def create_lookup(conn, name, rows):
    """Create lookup table if missing, seed values."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {name} (
            value TEXT PRIMARY KEY,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'approved' REFERENCES lookup_predicate_status(value),
            superseded_by TEXT REFERENCES {name}(value),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # If lookup_predicate_status doesn't exist yet (chicken-and-egg for the FK on .status),
    # the initial CREATE may fail. We bootstrap that one without the FK on status first.
    for value, description in rows:
        conn.execute(
            f"INSERT OR IGNORE INTO {name} (value, description, status) VALUES (?, ?, 'approved')",
            (value, description),
        )


# ---------------------------------------------------------------------------
# Parent-table rebuild helpers
# ---------------------------------------------------------------------------

def _column_info(conn, table):
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _index_list(conn, table):
    return [r for r in conn.execute(f"PRAGMA index_list({table})").fetchall() if not r[1].startswith("sqlite_")]


def _index_create_sql(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
    return row[0] if row and row[0] else None


def _trigger_create_sql(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
    return row[0] if row and row[0] else None


def _table_triggers(conn, table):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()]


def already_has_fk(conn, table, column, target_table):
    """Return True if the table already has a FK on `column` -> target_table.value."""
    fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    for fk in fks:
        # fk: id, seq, table, from, to, on_update, on_delete, match
        if fk[2] == target_table and fk[3] == column:
            return True
    return False


def rebuild_table_with_fks(conn, table, fk_specs, drop_check_triggers):
    """
    fk_specs: list of (column, target_lookup_table, on_delete)  e.g. ('status', 'lookup_predicate_status', 'RESTRICT')
    drop_check_triggers: list of trigger names to drop after rebuild
    """
    # Skip if ALL desired FKs already exist
    if all(already_has_fk(conn, table, col, tgt) for col, tgt, *_ in fk_specs):
        print(f"  {table}: all FKs already in place, skipping rebuild")
        for trig in drop_check_triggers:
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        return

    print(f"  {table}: rebuilding to add {len(fk_specs)} FK constraint(s)")

    # 1. Read existing schema using PRAGMA
    cols = _column_info(conn, table)  # rows: cid, name, type, notnull, dflt_value, pk
    indexes = _index_list(conn, table)  # rows: seq, name, unique, origin, partial
    index_create_sqls = [s for s in (_index_create_sql(conn, idx[1]) for idx in indexes) if s]

    triggers = _table_triggers(conn, table)
    keep_trigger_sqls = []
    for trig in triggers:
        if trig in drop_check_triggers:
            continue
        sql = _trigger_create_sql(conn, trig)
        if sql:
            keep_trigger_sqls.append(sql)

    # 2. Build new CREATE TABLE statement.
    # Column definitions reproduce existing nullability / defaults / pk.
    # Existing FKs are gathered too.
    fk_by_col = {col: (tgt, on_del) for col, tgt, on_del in fk_specs}
    existing_fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    new_fks = []  # list of FK clause strings
    for fk in existing_fks:
        from_col, to_col, ref_table = fk[3], fk[4], fk[2]
        on_del = fk[6] or "NO ACTION"
        # Skip if this FK is going to be replaced by our new FK on the same column
        if from_col in fk_by_col:
            continue
        new_fks.append(f"FOREIGN KEY ({from_col}) REFERENCES {ref_table}({to_col}) ON DELETE {on_del}")
    for col, tgt, on_del in fk_specs:
        new_fks.append(f"FOREIGN KEY ({col}) REFERENCES {tgt}(value) ON DELETE {on_del}")

    col_defs = []
    pk_cols = [c[1] for c in cols if c[5]]
    for c in cols:
        cid, name, ctype, notnull, dflt, pk = c
        line = f"{name} {ctype}"
        if notnull:
            line += " NOT NULL"
        if dflt is not None:
            line += f" DEFAULT {dflt}"
        if pk and len(pk_cols) == 1:
            # single-column PK; SQLite needs INTEGER PRIMARY KEY for autoincrement to work
            if ctype.upper() == "INTEGER":
                line += " PRIMARY KEY AUTOINCREMENT"
            else:
                line += " PRIMARY KEY"
        col_defs.append(line)

    if len(pk_cols) > 1:
        col_defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")

    col_defs.extend(new_fks)
    new_table_sql = f"CREATE TABLE _{table}_new (\n  " + ",\n  ".join(col_defs) + "\n)"

    # 3. Execute the rebuild inside foreign_keys=OFF
    col_names = [c[1] for c in cols]
    cols_csv = ", ".join(col_names)
    conn.executescript(f"""
        PRAGMA foreign_keys = OFF;
        {new_table_sql};
        INSERT INTO _{table}_new ({cols_csv}) SELECT {cols_csv} FROM {table};
        DROP TABLE {table};
        ALTER TABLE _{table}_new RENAME TO {table};
    """)

    # 4. Recreate indexes (skip auto-PK indexes which sqlite recreates).
    for sql in index_create_sqls:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # index already exists from PK rebuild

    # 5. Recreate triggers we kept (and DROP CHECK ones)
    for trig in drop_check_triggers:
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    for sql in keep_trigger_sqls:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass

    print(f"  {table}: rebuild complete")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()

    if not os.path.exists(args.path):
        raise SystemExit(f"{args.path} does not exist")

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.9.0 (lookup tables + FK enforcement)...")

    # ------------------------------------------------------------------
    # Phase 1: bootstrap lookup_predicate_status FIRST (others reference it)
    # ------------------------------------------------------------------
    # Workaround the chicken-and-egg: create predicate_status without the
    # status-self-reference, seed it, then create the rest with FKs.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lookup_predicate_status (
            value TEXT PRIMARY KEY,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'approved',
            superseded_by TEXT REFERENCES lookup_predicate_status(value),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for value, description in LOOKUPS["lookup_predicate_status"]:
        conn.execute(
            "INSERT OR IGNORE INTO lookup_predicate_status (value, description) VALUES (?, ?)",
            (value, description),
        )
    print("  lookup_predicate_status: bootstrapped")

    # ------------------------------------------------------------------
    # Phase 2: create remaining lookup tables (each FK's its status -> lookup_predicate_status)
    # ------------------------------------------------------------------
    for name, rows in LOOKUPS.items():
        if name == "lookup_predicate_status":
            continue
        create_lookup(conn, name, rows)
        print(f"  {name}: {len(rows)} values seeded")
    conn.commit()

    # ------------------------------------------------------------------
    # Phase 3: rebuild parent tables with FK constraints to lookup tables
    # ------------------------------------------------------------------
    print("\nRebuilding parent tables to add FK constraints...")

    # predicate
    rebuild_table_with_fks(
        conn, "predicate",
        [("status", "lookup_predicate_status", "RESTRICT"),
         ("cardinality", "lookup_predicate_cardinality", "RESTRICT")],
        drop_check_triggers=["trg_predicate_status_check"],
    )

    # edge
    rebuild_table_with_fks(
        conn, "edge",
        [("status", "lookup_edge_status", "RESTRICT"),
         ("confidence", "lookup_edge_confidence", "RESTRICT"),
         ("sensitivity", "lookup_edge_sensitivity", "RESTRICT")],
        drop_check_triggers=["trg_edge_status_check", "trg_edge_confidence_check"],
    )

    # citation
    rebuild_table_with_fks(
        conn, "citation",
        [("cited_kind", "lookup_citation_kind", "RESTRICT"),
         ("source_kind", "lookup_citation_source", "RESTRICT")],
        drop_check_triggers=["trg_citation_kind_check"],
    )

    # attribution_event
    rebuild_table_with_fks(
        conn, "attribution_event",
        [("target_kind", "lookup_attribution_kind", "RESTRICT"),
         ("source", "lookup_attribution_source", "RESTRICT"),
         ("confidence", "lookup_identity_confidence", "RESTRICT")],
        drop_check_triggers=["trg_attribution_kind_check"],
    )

    # memory_entity
    rebuild_table_with_fks(
        conn, "memory_entity",
        [("role", "lookup_memory_role", "RESTRICT")],
        drop_check_triggers=["trg_memory_entity_role_check"],
    )

    # drift_hypothesis
    rebuild_table_with_fks(
        conn, "drift_hypothesis",
        [("status", "lookup_drift_status", "RESTRICT")],
        drop_check_triggers=["trg_drift_hypothesis_status"],
    )

    # z_memory  (sensitivity)
    rebuild_table_with_fks(
        conn, "z_memory",
        [("sensitivity", "lookup_edge_sensitivity", "RESTRICT")],
        drop_check_triggers=["trg_z_memory_sensitivity_check"],
    )

    # entity_alias  (alias_kind, confidence)
    rebuild_table_with_fks(
        conn, "entity_alias",
        [("alias_kind", "lookup_alias_kind", "RESTRICT"),
         ("confidence", "lookup_identity_confidence", "RESTRICT")],
        drop_check_triggers=[],
    )

    # source_meeting  (attribution_source, attribution_confidence)
    rebuild_table_with_fks(
        conn, "source_meeting",
        [("attribution_source", "lookup_attribution_source", "RESTRICT"),
         ("attribution_confidence", "lookup_identity_confidence", "RESTRICT")],
        drop_check_triggers=[],
    )

    # source_document
    rebuild_table_with_fks(
        conn, "source_document",
        [("attribution_source", "lookup_attribution_source", "RESTRICT"),
         ("attribution_confidence", "lookup_identity_confidence", "RESTRICT")],
        drop_check_triggers=[],
    )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()

    # ------------------------------------------------------------------
    # Phase 4: documentation
    # ------------------------------------------------------------------
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("table", lookup, None,
             f"Vocabulary table for the {lookup.replace('lookup_', '')} concept. "
             "Adding a new value = INSERT a row. Deprecation = SET status='deprecated' (and optionally superseded_by).")
            for lookup in LOOKUPS
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("lookup table",
         "lookup_<concept> tables hold the valid values for a categorical column. "
         "Parent tables FK their column to lookup_<concept>(value). Adding a new value = "
         "INSERT into the lookup table. No CHECK trigger gymnastics, no migrations to add values.",
         "lookup_alias_kind contains 'name', 'nickname', 'transcription_drift', etc. "
         "entity_alias.alias_kind FKs to it."),
    )
    conn.commit()

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.9.0"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.9.0', 'Replace hardcoded enum CHECK triggers with lookup_<concept> tables and FK constraints; add 14 lookup tables seeded with current values; rebuild 10 parent tables to use FK enforcement')"
        )
        print("\nbumped to v0.9.0")
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
