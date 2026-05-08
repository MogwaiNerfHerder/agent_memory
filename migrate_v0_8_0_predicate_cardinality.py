"""
migrate_v0_8_0_predicate_cardinality.py - Add cardinality to predicate vocabulary.

Some predicates are SINGLE-VALUED (one active edge per subject — disagreements
are genuine discrepancies):
    has_role, reports_to, employed_by, has_event_type, event_status,
    occurred_at, scheduled_for, occurred_around, started_at, ended_at,
    assigned_to, commissioned_by, target_of_diligence, project_status,
    engages_cortado_for, has_email, has_phone, recurring_pattern

Others are MULTI-VALUED (many active edges OK — multiple people attend, multiple
platforms served, etc.):
    attended_by, staffed_on, stakeholder_on, engagement_role, serves_platform,
    has_box_folder, has_asana_board, has_workstream, has_deliverable,
    has_milestone, has_risk, has_event, member_of_project, mentions,
    precedes, follows, parent_of, part_of, comparable_to, peer_of,
    discovered_at, manages, on_team_with, supports, blocks

Adds:
    predicate.cardinality TEXT  -- 'single_valued' | 'multi_valued' | NULL (= unknown)

Idempotent. Sets known classifications by name.
"""

import argparse
import os
import sqlite3
import sys


SINGLE_VALUED = [
    "has_role", "reports_to", "employed_by", "has_event_type", "event_status",
    "occurred_at", "scheduled_for", "occurred_around", "started_at", "ended_at",
    "assigned_to", "commissioned_by", "target_of_diligence", "project_status",
    "engages_cortado_for", "has_email", "has_phone", "recurring_pattern",
    "scheduled_start", "scheduled_end", "stakeholder_disposition",
    "stakeholder_influence",
]

MULTI_VALUED = [
    "attended_by", "staffed_on", "stakeholder_on", "engagement_role",
    "serves_platform", "has_box_folder", "has_asana_board", "has_workstream",
    "has_deliverable", "has_milestone", "has_risk", "has_event",
    "member_of_project", "mentions", "precedes", "follows", "parent_of",
    "part_of", "comparable_to", "peer_of", "discovered_at", "manages",
    "on_team_with", "supports", "blocks", "concerns", "event_about",
    "uses_vdr_platform", "banker_for", "produced_for", "commissioned_by_external",
]


def column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()

    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.8.0 (predicate cardinality)...")

    if not column_exists(conn, "predicate", "cardinality"):
        conn.execute("ALTER TABLE predicate ADD COLUMN cardinality TEXT")
        print("  predicate: added cardinality column")
    else:
        print("  predicate: cardinality already present")

    n_single = n_multi = 0
    for name in SINGLE_VALUED:
        n = conn.execute("UPDATE predicate SET cardinality='single_valued' WHERE name=?", (name,)).rowcount
        n_single += n
    for name in MULTI_VALUED:
        n = conn.execute("UPDATE predicate SET cardinality='multi_valued' WHERE name=?", (name,)).rowcount
        n_multi += n
    print(f"  classified: {n_single} single-valued, {n_multi} multi-valued")

    conn.execute(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        ("column", "predicate", "cardinality",
         "single_valued: at most one active edge per (subject, predicate). Multiple active edges => discrepancy. "
         "multi_valued: many active edges expected (attendees, platforms, members). NULL: unknown — consolidator treats as multi by default."),
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("predicate cardinality",
         "single_valued vs multi_valued. Single-valued predicates can have only one active edge per (subject, predicate) — multiple parallel actives are discrepancies. Multi-valued predicates are collections (attended_by, staffed_on) and many actives are normal.",
         "scheduled_for is single-valued; if two active edges both name dates, one should supersede. attended_by is multi-valued; many attendees is normal."),
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.8.0":
        print("  z_version already at v0.8.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.8.0', 'Add predicate.cardinality (single_valued | multi_valued); seed known classifications')"
        )
        print("  bumped to v0.8.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
