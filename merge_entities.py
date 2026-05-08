"""
merge_entities.py - Fold a duplicate entity into a canonical entity.

When two entities represent the same real-world object (different source systems,
naming variants, accidental duplicates), this tool moves the duplicate's references
onto the canonical, then deletes the duplicate.

Operations performed (in order, in a single transaction):
  1. Move all entity_alias rows from duplicate to canonical (skipping conflicts).
  2. Re-target all edges:
       - subject_id = duplicate -> canonical
       - object_id = duplicate -> canonical
       - drop edges that would become self-loops post-merge (subject == object)
       - dedupe: if the resulting (subject, predicate, object_id_or_literal) already
         exists with status='active', drop the duplicate's edge but migrate its
         citations onto the canonical's surviving edge.
  3. Re-target memory_entity rows (entity_id duplicate -> canonical), dedupe.
  4. Re-target drift_hypothesis rows.
  5. Re-target attribution_event rows.
  6. Delete the duplicate entity.

Usage:
    merge_entities.py --keeper 34 --duplicate 212
    merge_entities.py --keeper 34 --duplicate 212 --dry-run
"""

import argparse
import os
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keeper", type=int, required=True, help="Entity ID to KEEP")
    p.add_argument("--duplicate", type=int, required=True, help="Entity ID to MERGE INTO keeper and delete")
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.keeper == args.duplicate:
        raise SystemExit("keeper and duplicate must be different ids")

    conn = sqlite3.connect(os.path.expanduser(args.db))
    conn.execute("PRAGMA foreign_keys = ON")

    keeper = conn.execute("SELECT entity_id, type, canonical_name, client_id FROM entity WHERE entity_id=?", (args.keeper,)).fetchone()
    dup = conn.execute("SELECT entity_id, type, canonical_name, client_id FROM entity WHERE entity_id=?", (args.duplicate,)).fetchone()
    if not keeper:
        raise SystemExit(f"keeper entity_id={args.keeper} not found")
    if not dup:
        raise SystemExit(f"duplicate entity_id={args.duplicate} not found")
    if keeper[3] != dup[3]:
        raise SystemExit(f"refusing: entities are in different clients ({keeper[3]} vs {dup[3]})")

    print(f"keeper:    [{keeper[1]}] {keeper[2]} (id={keeper[0]})")
    print(f"duplicate: [{dup[1]}] {dup[2]} (id={dup[0]})")
    print(f"client_id: {keeper[3]}")
    print()

    stats = {
        "aliases_moved": 0, "aliases_skipped": 0,
        "edges_resubjected": 0, "edges_reobjected": 0,
        "edges_dropped_self_loop": 0, "edges_dropped_duplicate": 0,
        "citations_migrated": 0,
        "memory_entity_moved": 0, "memory_entity_skipped": 0,
        "drift_hypothesis_moved": 0,
        "attribution_event_moved": 0,
    }

    # ---- 1. Aliases ----
    for r in conn.execute(
        "SELECT alias_id, alias_text, alias_kind, confidence, resolved_by FROM entity_alias WHERE entity_id=?",
        (dup[0],),
    ).fetchall():
        existing = conn.execute(
            "SELECT alias_id FROM entity_alias WHERE entity_id=? AND alias_text=?",
            (keeper[0], r[1]),
        ).fetchone()
        if existing:
            stats["aliases_skipped"] += 1
        else:
            if not args.dry_run:
                conn.execute("UPDATE entity_alias SET entity_id=? WHERE alias_id=?", (keeper[0], r[0]))
            stats["aliases_moved"] += 1

    # ---- 2. Edges ----
    # Subject side: edges where dup is subject. Re-target to keeper. Check self-loops + dedupe.
    for r in conn.execute(
        "SELECT edge_id, predicate_id, object_id, object_literal FROM edge WHERE subject_id=? AND status='active'",
        (dup[0],),
    ).fetchall():
        edge_id, pid, obj_id, obj_lit = r
        new_obj_id = keeper[0] if obj_id == dup[0] else obj_id
        # Self-loop check
        if new_obj_id is not None and new_obj_id == keeper[0]:
            stats["edges_dropped_self_loop"] += 1
            if not args.dry_run:
                conn.execute("DELETE FROM citation WHERE cited_kind='edge' AND cited_id=?", (edge_id,))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            continue
        # Dedupe: does keeper already have an active edge with same (predicate, object)?
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
            if not args.dry_run:
                conn.execute("UPDATE citation SET cited_id=? WHERE cited_kind='edge' AND cited_id=?", (twin_id, edge_id))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            stats["edges_dropped_duplicate"] += 1
            stats["citations_migrated"] += 1
        else:
            if not args.dry_run:
                conn.execute("UPDATE edge SET subject_id=?, object_id=? WHERE edge_id=?", (keeper[0], new_obj_id, edge_id))
            stats["edges_resubjected"] += 1

    # Object side: edges where dup is object (and subject != dup, that case handled above).
    for r in conn.execute(
        "SELECT edge_id, subject_id, predicate_id, object_literal FROM edge WHERE object_id=? AND subject_id<>? AND status='active'",
        (dup[0], dup[0]),
    ).fetchall():
        edge_id, subj_id, pid, obj_lit = r
        if subj_id == keeper[0]:  # self-loop after merge
            stats["edges_dropped_self_loop"] += 1
            if not args.dry_run:
                conn.execute("DELETE FROM citation WHERE cited_kind='edge' AND cited_id=?", (edge_id,))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            continue
        twin = conn.execute(
            "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active' AND edge_id<>?",
            (keeper[3], subj_id, pid, keeper[0], edge_id),
        ).fetchone()
        if twin:
            twin_id = twin[0]
            if not args.dry_run:
                conn.execute("UPDATE citation SET cited_id=? WHERE cited_kind='edge' AND cited_id=?", (twin_id, edge_id))
                conn.execute("DELETE FROM edge WHERE edge_id=?", (edge_id,))
            stats["edges_dropped_duplicate"] += 1
            stats["citations_migrated"] += 1
        else:
            if not args.dry_run:
                conn.execute("UPDATE edge SET object_id=? WHERE edge_id=?", (keeper[0], edge_id))
            stats["edges_reobjected"] += 1

    # ---- 3. memory_entity ----
    for r in conn.execute("SELECT memory_entity_id, memory_id, role FROM memory_entity WHERE entity_id=?", (dup[0],)).fetchall():
        me_id, mid, role = r
        existing = conn.execute(
            "SELECT memory_entity_id FROM memory_entity WHERE memory_id=? AND entity_id=? AND role=?",
            (mid, keeper[0], role),
        ).fetchone()
        if existing:
            stats["memory_entity_skipped"] += 1
            if not args.dry_run:
                conn.execute("DELETE FROM memory_entity WHERE memory_entity_id=?", (me_id,))
        else:
            if not args.dry_run:
                conn.execute("UPDATE memory_entity SET entity_id=? WHERE memory_entity_id=?", (keeper[0], me_id))
            stats["memory_entity_moved"] += 1

    # ---- 4. drift_hypothesis ----
    n = conn.execute("SELECT COUNT(*) FROM drift_hypothesis WHERE candidate_entity_id=?", (dup[0],)).fetchone()[0]
    if n and not args.dry_run:
        conn.execute("UPDATE drift_hypothesis SET candidate_entity_id=? WHERE candidate_entity_id=?", (keeper[0], dup[0]))
    stats["drift_hypothesis_moved"] = n

    # ---- 5. attribution_event ----
    n = conn.execute("SELECT COUNT(*) FROM attribution_event WHERE target_kind='entity' AND target_id=?", (dup[0],)).fetchone()[0]
    if n and not args.dry_run:
        conn.execute("UPDATE attribution_event SET target_id=? WHERE target_kind='entity' AND target_id=?", (keeper[0], dup[0]))
    stats["attribution_event_moved"] = n

    # ---- 6. Delete duplicate ----
    if not args.dry_run:
        # The duplicate's own aliases / edges / memory_entity links should now be empty
        # (moved or deleted above). Delete the entity row; FK CASCADE handles any leftovers.
        conn.execute("DELETE FROM entity WHERE entity_id=?", (dup[0],))
        # Record the merge in attribution_event for audit
        conn.execute(
            """INSERT INTO attribution_event (target_kind, target_id, client_id, previous_client_id,
                                                confidence, source, attributed_by, rationale)
               VALUES ('entity', ?, ?, ?, 'certain', 'manual', ?, ?)""",
            (keeper[0], keeper[3], keeper[3], "merge_entities.py",
             f"Merged duplicate entity_id={dup[0]} ({dup[2]!r}) into keeper entity_id={keeper[0]} ({keeper[2]!r})"),
        )
        conn.commit()

    print("Merge stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    print("DONE." if not args.dry_run else "DRY RUN — no changes made.")


if __name__ == "__main__":
    main()
