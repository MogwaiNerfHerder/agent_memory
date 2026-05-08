"""
migrate_v0_10_1_register_merge_entities.py - Register merge_entities() in z_script_catalog.

Folds the existing merge_entities.py logic into the script catalog so any
agent / process can discover and invoke entity merges via the catalog
loader, not just our CLI tool.

Idempotent.
"""

import argparse
import json
import os
import sqlite3
import sys


SCRIPT_NAME = "merge_entities"
SCRIPT_DESC = "Fold a duplicate entity into a canonical (keeper). Moves aliases, edges, memory_entity links, drift hypotheses, attribution events; dedupes; deletes the duplicate."
SCRIPT_APPLIES = "entity,entity_alias,edge,memory_entity,drift_hypothesis,attribution_event"

SCRIPT_BODY = r'''
def merge_entities(conn, keeper_id, duplicate_id, dry_run=False):
    """
    Fold `duplicate_id` into `keeper_id`. Both must belong to the same client.
    Returns a stats dict.
    """
    if keeper_id == duplicate_id:
        raise ValueError("keeper and duplicate must differ")
    keeper = conn.execute(
        "SELECT entity_id, type, canonical_name, client_id FROM entity WHERE entity_id=?", (keeper_id,)
    ).fetchone()
    dup = conn.execute(
        "SELECT entity_id, type, canonical_name, client_id FROM entity WHERE entity_id=?", (duplicate_id,)
    ).fetchone()
    if not keeper:
        raise ValueError(f"keeper entity_id={keeper_id} not found")
    if not dup:
        raise ValueError(f"duplicate entity_id={duplicate_id} not found")
    if keeper[3] != dup[3]:
        raise ValueError(f"entities are in different clients ({keeper[3]} vs {dup[3]})")

    stats = {
        "keeper_id": keeper_id, "duplicate_id": duplicate_id, "dry_run": dry_run,
        "aliases_moved": 0, "aliases_skipped": 0,
        "edges_resubjected": 0, "edges_reobjected": 0,
        "edges_dropped_self_loop": 0, "edges_dropped_duplicate": 0,
        "citations_migrated": 0,
        "memory_entity_moved": 0, "memory_entity_skipped": 0,
        "drift_hypothesis_moved": 0,
        "attribution_event_moved": 0,
    }

    # 1. Aliases
    for r in conn.execute(
        "SELECT alias_id, alias_text, alias_kind, confidence FROM entity_alias WHERE entity_id=?",
        (dup[0],),
    ).fetchall():
        existing = conn.execute(
            "SELECT alias_id FROM entity_alias WHERE entity_id=? AND alias_text=?",
            (keeper[0], r[1]),
        ).fetchone()
        if existing:
            stats["aliases_skipped"] += 1
        else:
            if not dry_run:
                conn.execute("UPDATE entity_alias SET entity_id=? WHERE alias_id=?", (keeper[0], r[0]))
            stats["aliases_moved"] += 1

    # 2. Edges where dup is subject — re-target subject_id; check self-loops + dedupe
    for r in conn.execute(
        "SELECT edge_id, predicate_id, object_id, object_literal FROM edge WHERE subject_id=? AND status='active'",
        (dup[0],),
    ).fetchall():
        edge_id, pid, obj_id, obj_lit = r
        new_obj_id = keeper[0] if obj_id == dup[0] else obj_id
        if new_obj_id is not None and new_obj_id == keeper[0]:
            stats["edges_dropped_self_loop"] += 1
            if not dry_run:
                conn.execute("DELETE FROM citation WHERE cited_kind='edge' AND cited_id=?", (edge_id,))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            continue
        if obj_id is not None:
            twin = conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active' AND edge_id<>?",
                (keeper[3], keeper[0], pid, new_obj_id, edge_id),
            ).fetchone()
        else:
            twin = conn.execute(
                "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_literal IS ? AND status='active' AND edge_id<>?",
                (keeper[3], keeper[0], pid, obj_lit, edge_id),
            ).fetchone()
        if twin:
            twin_id = twin[0]
            if not dry_run:
                conn.execute("UPDATE citation SET cited_id=? WHERE cited_kind='edge' AND cited_id=?", (twin_id, edge_id))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            stats["edges_dropped_duplicate"] += 1
            stats["citations_migrated"] += 1
        else:
            if not dry_run:
                conn.execute("UPDATE edge SET subject_id=?, object_id=? WHERE edge_id=?", (keeper[0], new_obj_id, edge_id))
            stats["edges_resubjected"] += 1

    # 3. Edges where dup is object (and subject != dup, handled above)
    for r in conn.execute(
        "SELECT edge_id, subject_id, predicate_id, object_literal FROM edge WHERE object_id=? AND subject_id<>? AND status='active'",
        (dup[0], dup[0]),
    ).fetchall():
        edge_id, subj_id, pid, obj_lit = r
        if subj_id == keeper[0]:
            stats["edges_dropped_self_loop"] += 1
            if not dry_run:
                conn.execute("DELETE FROM citation WHERE cited_kind='edge' AND cited_id=?", (edge_id,))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            continue
        twin = conn.execute(
            "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active' AND edge_id<>?",
            (keeper[3], subj_id, pid, keeper[0], edge_id),
        ).fetchone()
        if twin:
            twin_id = twin[0]
            if not dry_run:
                conn.execute("UPDATE citation SET cited_id=? WHERE cited_kind='edge' AND cited_id=?", (twin_id, edge_id))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            stats["edges_dropped_duplicate"] += 1
            stats["citations_migrated"] += 1
        else:
            if not dry_run:
                conn.execute("UPDATE edge SET object_id=? WHERE edge_id=?", (keeper[0], edge_id))
            stats["edges_reobjected"] += 1

    # 4. memory_entity
    for r in conn.execute("SELECT memory_entity_id, memory_id, role FROM memory_entity WHERE entity_id=?", (dup[0],)).fetchall():
        me_id, mid, role = r
        existing = conn.execute(
            "SELECT memory_entity_id FROM memory_entity WHERE memory_id=? AND entity_id=? AND role=?",
            (mid, keeper[0], role),
        ).fetchone()
        if existing:
            stats["memory_entity_skipped"] += 1
            if not dry_run:
                conn.execute("DELETE FROM memory_entity WHERE memory_entity_id=?", (me_id,))
        else:
            if not dry_run:
                conn.execute("UPDATE memory_entity SET entity_id=? WHERE memory_entity_id=?", (keeper[0], me_id))
            stats["memory_entity_moved"] += 1

    # 5. drift_hypothesis
    n = conn.execute("SELECT COUNT(*) FROM drift_hypothesis WHERE candidate_entity_id=?", (dup[0],)).fetchone()[0]
    if n and not dry_run:
        conn.execute("UPDATE drift_hypothesis SET candidate_entity_id=? WHERE candidate_entity_id=?", (keeper[0], dup[0]))
    stats["drift_hypothesis_moved"] = n

    # 6. attribution_event
    n = conn.execute("SELECT COUNT(*) FROM attribution_event WHERE target_kind='entity' AND target_id=?", (dup[0],)).fetchone()[0]
    if n and not dry_run:
        conn.execute("UPDATE attribution_event SET target_id=? WHERE target_kind='entity' AND target_id=?", (keeper[0], dup[0]))
    stats["attribution_event_moved"] = n

    # 7. Delete duplicate
    if not dry_run:
        conn.execute("DELETE FROM entity WHERE entity_id=?", (dup[0],))
        conn.execute(
            """INSERT INTO attribution_event (target_kind, target_id, client_id, previous_client_id,
                                                confidence, source, attributed_by, rationale)
               VALUES ('entity', ?, ?, ?, 'certain', 'manual', ?, ?)""",
            (keeper[0], keeper[3], keeper[3], "merge_entities",
             "Merged duplicate entity_id=" + str(dup[0]) + " (" + repr(dup[2]) + ") into keeper entity_id=" + str(keeper[0]) + " (" + repr(keeper[2]) + ")"),
        )
        conn.commit()

    return stats
'''


PARAMS = [
    ("conn", "sqlite3.Connection", None, "SQLite connection"),
    ("keeper_id", "int", None, "Entity ID to keep (canonical)"),
    ("duplicate_id", "int", None, "Entity ID to merge into keeper and delete"),
    ("dry_run", "bool", "False", "If True, return stats without modifying"),
]


TESTS = [
    (
        "merge_entities", "test_dry_run_no_changes",
        "Dry run returns stats with dry_run=True; no changes applied",
        json.dumps({"args": [99001, 99002], "kwargs": {"dry_run": True}}),
        json.dumps({"dry_run": True, "keeper_id": 99001, "duplicate_id": 99002}),
        None,
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v101', 'Test Client');"
        "INSERT OR IGNORE INTO entity (entity_id, client_id, type, canonical_name) VALUES (99001, 9001, 'person', 'Keeper');"
        "INSERT OR IGNORE INTO entity (entity_id, client_id, type, canonical_name) VALUES (99002, 9001, 'person', 'Dup');",
        "DELETE FROM entity WHERE entity_id IN (99001, 99002); DELETE FROM client WHERE client_id=9001;",
    ),
    (
        "merge_entities", "test_refuses_cross_client",
        "Refuses to merge entities from different clients",
        json.dumps({"args": [99003, 99004]}),
        None,
        "ValueError",
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v101_a', 'TestA');"
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9002, 'test_v101_b', 'TestB');"
        "INSERT OR IGNORE INTO entity (entity_id, client_id, type, canonical_name) VALUES (99003, 9001, 'person', 'A');"
        "INSERT OR IGNORE INTO entity (entity_id, client_id, type, canonical_name) VALUES (99004, 9002, 'person', 'B');",
        "DELETE FROM entity WHERE entity_id IN (99003, 99004); DELETE FROM client WHERE client_id IN (9001, 9002);",
    ),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()
    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.10.1 (register merge_entities)...")

    conn.execute("""
        INSERT INTO z_script_catalog
            (script_name, description, language, script_body, applies_to, version_target, is_active)
        VALUES (?, ?, 'python', ?, ?, 'v0.10.1', 1)
        ON CONFLICT(script_name) DO UPDATE SET
            description=excluded.description,
            script_body=excluded.script_body,
            applies_to=excluded.applies_to,
            version_target=excluded.version_target,
            updated_at=CURRENT_TIMESTAMP
    """, (SCRIPT_NAME, SCRIPT_DESC, SCRIPT_BODY.lstrip("\n"), SCRIPT_APPLIES))
    conn.execute("DELETE FROM z_script_params WHERE script_name=?", (SCRIPT_NAME,))
    for idx, (pname, ptype, default, pdesc) in enumerate(PARAMS, start=1):
        conn.execute(
            """INSERT INTO z_script_params (script_name, method_name, param_name, param_type, default_value, description, ordinal)
               VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (SCRIPT_NAME, pname, ptype, default, pdesc, idx),
        )
    print(f"  registered {SCRIPT_NAME} ({len(PARAMS)} params)")

    for sname, tname, tdesc, tinput, tout, terror, setup, teardown in TESTS:
        conn.execute("""
            INSERT OR IGNORE INTO z_script_test
              (script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (sname, tname, tdesc, tinput, tout, terror, setup, teardown))
    print(f"  registered {len(TESTS)} tests")

    conn.commit()

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.10.1"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.10.1', 'Register merge_entities() in z_script_catalog')"
        )
        print("  bumped to v0.10.1")
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
