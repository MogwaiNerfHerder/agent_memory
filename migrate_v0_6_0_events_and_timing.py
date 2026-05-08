"""
migrate_v0_6_0_events_and_timing.py - Approve event/timing predicate vocabulary.

No schema changes (events are entities of type='event', already supported).
This migration seeds and approves the timing predicate vocabulary so the
extractor knows what to use:

  has_event_type          : event -> literal (meeting|action_item|decision|...)
  scheduled_for           : event -> date (planned future, supersedable)
  occurred_at             : event -> datetime (confirmed past, precise)
  occurred_around         : event -> literal (fuzzy past: '2024-Q3', 'last year')
  started_at / ended_at   : event/project -> date (bounded periods)
  discovered_at           : event -> datetime (when WE learned of it, vs when it happened)
  recurring_pattern       : event -> literal (e.g., 'weekly Wednesday')
  event_status            : event -> literal (scheduled|occurred|cancelled|missed|in_progress)
  assigned_to             : event -> person (action_item ownership)
  precedes / follows      : event -> event (dependency chain)

Idempotent.
"""

import argparse
import json
import os
import sqlite3
import sys


PREDICATES = [
    # name, description, subject_types, object_types, inverse_name
    ("has_event_type",     "Event sub-type (meeting|action_item|decision|deliverable|deadline|termination|milestone|party|product_launch|deal_close|departure|activity)",
     ["event"], ["literal"], None),
    ("scheduled_for",      "Event is planned for a specific (future or past, may shift) date. Supersedable when dates move.",
     ["event","project"], ["literal"], None),
    ("occurred_at",        "Event confirmed to have happened at a precise datetime. Past tense, high precision.",
     ["event"], ["literal"], None),
    ("occurred_around",    "Event happened approximately (fuzzy past). Object_literal_type: year|quarter|month|date_range.",
     ["event"], ["literal"], None),
    ("started_at",         "Bounded period start. For projects, engagements, employments, etc.",
     ["event","project","person","company"], ["literal"], None),
    ("ended_at",           "Bounded period end (already happened).",
     ["event","project","person","company"], ["literal"], None),
    ("discovered_at",      "When the graph first learned of this event/fact (≠ when the event happened).",
     ["event"], ["literal"], None),
    ("recurring_pattern",  "Event recurs on a pattern (e.g., 'weekly Wednesday', 'monthly first Tuesday').",
     ["event"], ["literal"], None),
    ("event_status",       "scheduled | occurred | cancelled | missed | in_progress",
     ["event"], ["literal"], None),
    ("assigned_to",        "Action-item event is owned by a person.",
     ["event"], ["person"], "owner_of"),
    ("precedes",           "Event precedes another event (this -> next).",
     ["event"], ["event"], "follows"),
    ("follows",            "Event follows another event (this <- previous).",
     ["event"], ["event"], "precedes"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.6.0 (event/timing predicates approved)...")

    for name, desc, subj, obj, inv in PREDICATES:
        existing = conn.execute("SELECT predicate_id, status FROM predicate WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE predicate SET status='approved', description=COALESCE(description, ?),
                       subject_types=COALESCE(subject_types, ?), object_types=COALESCE(object_types, ?),
                       inverse_name=COALESCE(inverse_name, ?)
                     WHERE predicate_id=?""",
                (desc, json.dumps(subj), json.dumps(obj), inv, existing[0]),
            )
            print(f"  {name}: existed (status was {existing[1]}) -> approved")
        else:
            conn.execute(
                """INSERT INTO predicate (name, description, subject_types, object_types, inverse_name,
                                            status, proposed_count)
                   VALUES (?, ?, ?, ?, ?, 'approved', 0)""",
                (name, desc, json.dumps(subj), json.dumps(obj), inv),
            )
            print(f"  {name}: created and approved")

    # Glossary
    conn.executemany(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        [
            ("event",
             "First-class entity (entity.type='event') representing something that happens in time — meetings, action items, decisions, deal closings, departures, milestones. Carries timing via dedicated predicates (scheduled_for, occurred_at, etc.); attendance/ownership via attended_by, assigned_to.",
             "(ic_readout_event) --has_event_type--> 'meeting'; --scheduled_for--> '2026-05-12'; --attended_by--> Hans"),
            ("timing predicate",
             "Specific predicate that captures the kind of temporal relationship: scheduled_for (future plan), occurred_at (precise past), occurred_around (fuzzy past), started_at/ended_at (period bounds), discovered_at (when we learned), recurring_pattern (periodic).",
             "(deal_close_event) --occurred_around--> '2024-Q3' [year]; --discovered_at--> '2026-04-28T18:30Z' [datetime]"),
        ],
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.6.0":
        print("  z_version already at v0.6.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.6.0', 'Approve event/timing predicate vocabulary: has_event_type, scheduled_for, occurred_at, occurred_around, started_at, ended_at, discovered_at, recurring_pattern, event_status, assigned_to, precedes, follows. Events are entities of type=event.')"
        )
        print("  bumped to v0.6.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
