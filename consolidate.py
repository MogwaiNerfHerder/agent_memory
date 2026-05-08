"""
consolidate.py - Cold-path graph consolidator.

Walks meeting-cited edges and notes for a client; promotes corroborated facts;
surfaces discrepancies and recurring observations.

v1 scope:
  1. Edge confidence promotion:
       active edge w/ >= 3 distinct meeting citations  &&  confidence in (stated, implied)
            -> SET confidence='pattern', last_corroborated_ts=NOW
       active edge w/ >= 5 distinct meeting citations  &&  confidence='pattern'
            -> SET confidence='confirmed'
       (factual edges are not auto-promoted; they're already procedural ground truth.)

  2. Discrepancies: for each (subject_id, predicate_id) with multiple ACTIVE edges
     where the object differs (object_id or object_literal), report the cluster
     so a human can decide whether to supersede.

  3. Pattern notes: for each entity, list memory_types with >= 3 notes linked via
     memory_entity (role in {subject, mentioned}). Highlights recurring themes.

Usage:
    consolidate.py --client-slug hig_growth_partners            # report only
    consolidate.py --client-slug hig_growth_partners --apply    # also run promotions
"""

import argparse
import os
import sqlite3
import sys


PROMOTION_THRESHOLD_PATTERN = 3
PROMOTION_THRESHOLD_CONFIRMED = 5


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client-slug", required=True)
    p.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    p.add_argument("--apply", action="store_true",
                    help="Apply confidence promotions (default: report only).")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(os.path.expanduser(args.db))
    conn.execute("PRAGMA foreign_keys = ON")

    row = conn.execute("SELECT client_id, name FROM client WHERE slug=?", (args.client_slug,)).fetchone()
    if not row:
        raise SystemExit(f"No client with slug '{args.client_slug}'")
    client_id, client_name = row
    print(f"=== Consolidating {client_name} (client_id={client_id}) ===\n")

    # -------------------------------------------------------------------------
    # 1. Edge confidence promotion
    # -------------------------------------------------------------------------
    promotions = conn.execute("""
        SELECT e.edge_id, e.confidence,
               p.name AS predicate,
               subj.canonical_name AS subj_name,
               COALESCE(obj.canonical_name, e.object_literal) AS obj_disp,
               COUNT(DISTINCT c.source_external_id) AS n_meetings
          FROM edge e
          JOIN predicate p ON p.predicate_id = e.predicate_id
          JOIN entity subj ON subj.entity_id = e.subject_id
     LEFT JOIN entity obj ON obj.entity_id = e.object_id
          JOIN citation c ON c.cited_kind='edge' AND c.cited_id = e.edge_id
                          AND c.source_kind='meeting'
         WHERE e.client_id=? AND e.status='active'
           AND e.confidence IN ('stated','implied','pattern')
      GROUP BY e.edge_id
        HAVING n_meetings >= ?
      ORDER BY n_meetings DESC, e.edge_id
    """, (client_id, PROMOTION_THRESHOLD_PATTERN)).fetchall()

    print(f"## Edge confidence promotions ({len(promotions)} candidates)")
    if not promotions:
        print("  (none)")
    else:
        n_to_pattern = 0
        n_to_confirmed = 0
        for edge_id, current, pred, subj, obj, n_mtgs in promotions:
            if current in ("stated", "implied") and n_mtgs >= PROMOTION_THRESHOLD_PATTERN:
                new_conf = "confirmed" if n_mtgs >= PROMOTION_THRESHOLD_CONFIRMED else "pattern"
                arrow = f"{current} -> {new_conf}"
                print(f"  edge {edge_id:>5}  {n_mtgs:>2} mtgs  {arrow:<22}  {subj} --{pred}--> {obj}")
                if args.apply:
                    conn.execute(
                        "UPDATE edge SET confidence=?, last_corroborated_ts=CURRENT_TIMESTAMP WHERE edge_id=?",
                        (new_conf, edge_id),
                    )
                if new_conf == "confirmed":
                    n_to_confirmed += 1
                else:
                    n_to_pattern += 1
            elif current == "pattern" and n_mtgs >= PROMOTION_THRESHOLD_CONFIRMED:
                print(f"  edge {edge_id:>5}  {n_mtgs:>2} mtgs  pattern -> confirmed     {subj} --{pred}--> {obj}")
                if args.apply:
                    conn.execute(
                        "UPDATE edge SET confidence='confirmed', last_corroborated_ts=CURRENT_TIMESTAMP WHERE edge_id=?",
                        (edge_id,),
                    )
                n_to_confirmed += 1
        if args.apply:
            conn.commit()
            print(f"\n  applied: {n_to_pattern} -> pattern,  {n_to_confirmed} -> confirmed")
        else:
            print(f"\n  (dry-run; would promote {n_to_pattern} to pattern, {n_to_confirmed} to confirmed — pass --apply)")

    # -------------------------------------------------------------------------
    # 2. Discrepancies — same (subject, predicate) with multiple active objects.
    # Only flag SINGLE-VALUED predicates; multi-valued predicates legitimately
    # carry many parallel actives (attended_by, staffed_on, serves_platform, ...).
    # -------------------------------------------------------------------------
    discrepancies = conn.execute("""
        SELECT subj.canonical_name AS subj_name, p.name AS predicate, COUNT(*) AS n_edges
          FROM edge e
          JOIN predicate p ON p.predicate_id = e.predicate_id
          JOIN entity subj ON subj.entity_id = e.subject_id
         WHERE e.client_id=? AND e.status='active'
           AND p.cardinality = 'single_valued'
      GROUP BY e.subject_id, e.predicate_id
        HAVING n_edges > 1
      ORDER BY n_edges DESC, subj_name, predicate
    """, (client_id,)).fetchall()
    print(f"\n## Discrepancies — single-valued predicates with multiple active edges ({len(discrepancies)} clusters)")
    for subj, pred, n in discrepancies:
        # get the parallel edges
        rows = conn.execute("""
            SELECT e.edge_id, COALESCE(obj.canonical_name, e.object_literal) AS obj_disp,
                   e.confidence, e.sensitivity,
                   (SELECT COUNT(DISTINCT source_external_id) FROM citation
                     WHERE cited_kind='edge' AND cited_id=e.edge_id AND source_kind='meeting') AS n_mtgs,
                   (SELECT GROUP_CONCAT(source_kind, ',') FROM citation
                     WHERE cited_kind='edge' AND cited_id=e.edge_id) AS sources
              FROM edge e
         LEFT JOIN entity obj ON obj.entity_id = e.object_id
              JOIN entity subj ON subj.entity_id = e.subject_id
              JOIN predicate p ON p.predicate_id = e.predicate_id
             WHERE e.client_id=? AND e.status='active'
               AND subj.canonical_name=? AND p.name=?
          ORDER BY n_mtgs DESC, e.edge_id
        """, (client_id, subj, pred)).fetchall()
        print(f"\n  {subj} --{pred}-->  ({n} parallel edges)")
        for edge_id, obj_disp, conf, sens, n_mtgs, sources in rows:
            sources_str = sources or ""
            sens_str = "" if sens == "routine" else f"[{sens}] "
            print(f"    edge {edge_id:>5}  {sens_str}{conf:<10}  obj={obj_disp!r:<40}  meetings={n_mtgs}  sources={sources_str[:60]}")

    # -------------------------------------------------------------------------
    # 3. Pattern notes — entities with recurring themed observations
    # -------------------------------------------------------------------------
    print(f"\n## Pattern notes — entities with >= 3 notes of same memory_type")
    rows = conn.execute("""
        SELECT e.entity_id, e.canonical_name, e.type, m.memory_type, COUNT(DISTINCT m.memory_id) AS n_notes
          FROM memory_entity me
          JOIN entity e ON e.entity_id = me.entity_id
          JOIN z_memory m ON m.memory_id = me.memory_id
         WHERE m.client_id=? AND m.is_active=1 AND m.deleted_at IS NULL
      GROUP BY e.entity_id, m.memory_type
        HAVING n_notes >= 3
      ORDER BY n_notes DESC, e.canonical_name, m.memory_type
    """, (client_id,)).fetchall()
    if not rows:
        print("  (no entities with >=3 notes of any memory_type)")
    else:
        for eid, name, etype, mtype, n in rows:
            print(f"  [{etype:8}] {name:<45}  {mtype:<14}  {n} notes")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"SUMMARY (mode: {'APPLY' if args.apply else 'REPORT-ONLY'})")
    print(f"  Promotion candidates:    {len(promotions)}")
    print(f"  Discrepancy clusters:    {len(discrepancies)}")
    print(f"  Entities w/ pattern notes: {len(rows)}")


if __name__ == "__main__":
    main()
