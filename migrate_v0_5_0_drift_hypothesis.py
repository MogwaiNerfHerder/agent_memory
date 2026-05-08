"""
migrate_v0_5_0_drift_hypothesis.py - Add drift_hypothesis table for human-reviewed alias merges.

When an extractor sees a token that *might* be a mishearing/typo of an existing entity
(e.g. 'Dedle' → 'D. Dale', 'Shoppable' → 'Shopify'), it should NOT silently write an
alias on the candidate entity — that would risk merging genuinely distinct things
(Shoppable might be a real product, not a mishearing of Shopify).

Instead, hypotheses land here pending human review. Approval promotes to a real
entity_alias row with alias_kind='transcription_drift'. Rejection records that
the two are NOT the same entity (so the same hypothesis won't re-fire next meeting).

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
    parser.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.5.0 (drift_hypothesis)...")

    if not table_exists(conn, "drift_hypothesis"):
        conn.execute(
            """
            CREATE TABLE drift_hypothesis (
                drift_hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES client(client_id) ON DELETE CASCADE,
                observed_token TEXT NOT NULL,
                candidate_entity_id INTEGER NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
                rationale TEXT,
                supporting_quote TEXT,
                source_kind TEXT,
                source_external_id TEXT,
                source_ts TIMESTAMP,
                proposed_by TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by TEXT,
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(client_id, observed_token, candidate_entity_id, source_external_id)
            )
            """
        )
        conn.execute("CREATE INDEX idx_drift_hypothesis_pending ON drift_hypothesis(client_id, status)")
        conn.execute("CREATE INDEX idx_drift_hypothesis_token ON drift_hypothesis(client_id, observed_token)")
        conn.execute(
            """
            CREATE TRIGGER trg_drift_hypothesis_status
            BEFORE INSERT ON drift_hypothesis
            WHEN NEW.status NOT IN ('pending','approved','rejected')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid drift_hypothesis.status');
            END
            """
        )
        print("  drift_hypothesis created with FKs + indexes + status-check trigger")
    else:
        print("  drift_hypothesis already exists; skipping create")

    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("table", "drift_hypothesis", None,
             "Pending claims of the form 'observed_token is a transcription drift of candidate_entity'. Reviewed manually; approval promotes to entity_alias row with alias_kind='transcription_drift'."),
            ("column", "drift_hypothesis", "observed_token", "The string that appeared in the source (e.g., 'Dedle')"),
            ("column", "drift_hypothesis", "candidate_entity_id", "The entity we hypothesize the token refers to (e.g., D. Dale entity)"),
            ("column", "drift_hypothesis", "rationale", "Why the extractor thinks this is a drift, not a separate entity"),
            ("column", "drift_hypothesis", "supporting_quote", "Verbatim transcript snippet showing the observed token"),
            ("column", "drift_hypothesis", "status", "pending | approved | rejected"),
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("drift hypothesis",
         "An LLM-proposed claim that a token from a source (transcript, doc) is a transcription drift / typo / mishearing of a known entity, NOT a separate entity. Lands in drift_hypothesis pending human review; approval promotes to an entity_alias row.",
         "Hypothesis: 'Dedle' (observed in transcript) is a drift of D. Dale (existing entity). Reviewer approves → alias added; reviewer rejects → 'Dedle' stays separate."),
    )

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.5.0":
        print("  z_version already at v0.5.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.5.0', 'Add drift_hypothesis table: extractor-proposed transcription drift claims pending human review before promoting to entity_alias')"
        )
        print("  bumped to v0.5.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
