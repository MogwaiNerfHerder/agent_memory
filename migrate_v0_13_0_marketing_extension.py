"""
migrate_v0_13_0_marketing_extension.py - Additive Marketing Intelligence tables.

Adds a Marketing-specific overlay on top of the existing knowledge graph, per
docs/marketing-intelligence-design.md in the marketing_commercial_intelligence
repo. Nothing existing (client, entity, edge, z_memory, citation, source_meeting,
source_document) is altered — this migration only adds new tables.

New concepts, each as a lookup_<concept> table (per this repo's v0.9.0 convention
of FK-backed vocabularies rather than CHECK triggers):

    lookup_marketing_perspective   - internal | external (mandatory, see below)
    lookup_marketing_speaker_detail - client | prospect | research_participant |
                                      internal_employee | unknown (optional, finer axis)
    lookup_marketing_stream        - commercial_evidence | internal_craft | internal_direction
    lookup_evidence_purpose        - cortado_pov | external_intelligence | proof_and_examples
    lookup_marketing_use_permission - restricted_processing | internal_marketing_use |
                                       external_use_cleared

New tables:
    marketing_topic             - the six intelligence buckets (Demand/Account/Sales/
                                   Talent/Pricing/Customer), hierarchical for future subtopics
    marketing_source_revision   - content-hash-tracked processing state per source_meeting/
                                   source_document, with pending/running/succeeded/held/failed
                                   lifecycle that sync_meetings.py's flat boolean lacks
    marketing_nugget            - one row per Marketing-relevant z_memory row (unique FK).
                                   perspective is NOT NULL: a nugget CANNOT be inserted
                                   without a resolved internal/external facingness value.
                                   This is a hard requirement, enforced at the DB level via
                                   a NOT NULL FK to lookup_marketing_perspective, matching
                                   the same enforcement style already used for
                                   z_memory.sensitivity.
    marketing_nugget_topic      - bucket assignment join table. A nugget must have at least
                                   one row here (the intelligence-type requirement). SQLite
                                   cannot express this cross-table cardinality as a column
                                   constraint (no deferred constraints) - it is enforced by
                                   the application's single-transaction write (see
                                   marketing_store.py in marketing_commercial_intelligence),
                                   not by this schema alone. A periodic integrity check
                                   query is provided in verify_no_orphan_nuggets() below for
                                   tests/CI to call; do not mistake this for the NOT NULL
                                   guarantee that `perspective` has.
    marketing_citation_location - extends the existing citation table with real character
                                   offsets and a SHA-256 digest per populated insight field
                                   (citation.offset_start/offset_end exist but are never
                                   populated by extract_meeting.py/extract_document.py today)
    marketing_collection_item   - approval staging per named collection
    marketing_access_grant      - named-audience access grants, scoped by source client
    marketing_direction         - internal-direction state (proposed/confirmed/conflicting/superseded)

Idempotent. Never touches the live db in place when used for testing — run against a
copy (see marketing_commercial_intelligence's test fixtures).
"""

import argparse
import os
import sqlite3
import sys


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def create_lookup(conn, name, rows):
    """Create a lookup_<concept> table if missing and seed its values (idempotent)."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {name} (
            value TEXT PRIMARY KEY,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'approved' REFERENCES lookup_predicate_status(value),
            superseded_by TEXT REFERENCES {name}(value),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for value, description in rows:
        conn.execute(
            f"INSERT OR IGNORE INTO {name} (value, description, status) VALUES (?, ?, 'approved')",
            (value, description),
        )


LOOKUPS = {
    "lookup_marketing_perspective": [
        ("internal", "The speaker(s) whose words this nugget cites are Cortado staff"),
        ("external", "The speaker(s) whose words this nugget cites are not Cortado staff"),
    ],
    "lookup_marketing_speaker_detail": [
        ("client",               "Speaker is a contact at an active/former Cortado client"),
        ("prospect",              "Speaker is a contact at a prospective client"),
        ("research_participant",   "Speaker is an interview/research subject, not a buyer"),
        ("internal_employee",       "Speaker is Cortado staff"),
        ("unknown",                   "Not yet resolved; never guessed"),
    ],
    "lookup_marketing_stream": [
        ("commercial_evidence", "Buyer/client/prospect evidence usable in content or messaging"),
        ("internal_craft",      "Cortado's own expertise/methodology, not sourced from a buyer"),
        ("internal_direction",  "Internal strategic direction/decision, not buyer-facing evidence"),
    ],
    "lookup_evidence_purpose": [
        ("cortado_pov",           "Supports Cortado's own point of view on a bucket"),
        ("external_intelligence", "Market/analyst/competitor research"),
        ("proof_and_examples",    "Case study or concrete proof point"),
    ],
    "lookup_marketing_use_permission": [
        ("restricted_processing",  "May be processed/stored but not shown to any Marketing audience yet"),
        ("internal_marketing_use", "Cleared for internal Marketing use only"),
        ("external_use_cleared",   "Cleared for use in external content, per-claim review still required"),
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.13.0 (marketing extension)...")

    # ------------------------------------------------------------------
    # Lookup tables first (parent tables FK to these)
    # ------------------------------------------------------------------
    for name, rows in LOOKUPS.items():
        create_lookup(conn, name, rows)
        print(f"  {name}: {len(rows)} values seeded")
    conn.commit()

    # ------------------------------------------------------------------
    # marketing_topic - the six buckets, seeded
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_topic"):
        conn.execute("""
            CREATE TABLE marketing_topic (
                topic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key       TEXT NOT NULL UNIQUE,
                label           TEXT NOT NULL,
                parent_topic_id INTEGER REFERENCES marketing_topic(topic_id),
                box_origin      TEXT,
                state           TEXT NOT NULL DEFAULT 'active',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("  marketing_topic: created")
    else:
        print("  marketing_topic: already present")

    six_buckets = [
        ("demand",   "Demand"),
        ("account",  "Account"),
        ("sales",    "Sales"),
        ("talent",   "Talent"),
        ("pricing",  "Pricing"),
        ("customer", "Customer"),
    ]
    for key, label in six_buckets:
        conn.execute(
            "INSERT OR IGNORE INTO marketing_topic (topic_key, label) VALUES (?, ?)",
            (key, label),
        )
    conn.commit()
    print("  marketing_topic: six top-level buckets seeded")

    # ------------------------------------------------------------------
    # marketing_source_revision
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_source_revision"):
        conn.execute("""
            CREATE TABLE marketing_source_revision (
                revision_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source_meeting_id  INTEGER REFERENCES source_meeting(source_meeting_id),
                source_document_id INTEGER REFERENCES source_document(source_document_id),
                content_sha256     TEXT NOT NULL,
                metadata_sha256    TEXT NOT NULL,
                parser_version     TEXT NOT NULL,
                extractor_version  TEXT NOT NULL,
                processing_state   TEXT NOT NULL DEFAULT 'pending',
                attempt_count      INTEGER NOT NULL DEFAULT 0,
                lease_expires_at   TIMESTAMP,
                held_reason        TEXT,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK ((source_meeting_id IS NOT NULL) <> (source_document_id IS NOT NULL)),
                CHECK (processing_state IN ('pending','running','succeeded','held','failed')),
                UNIQUE (source_meeting_id, source_document_id, content_sha256, extractor_version)
            )
        """)
        conn.execute("CREATE INDEX idx_mkt_revision_state ON marketing_source_revision(processing_state)")
        print("  marketing_source_revision: created")
    else:
        print("  marketing_source_revision: already present")

    # ------------------------------------------------------------------
    # marketing_nugget - perspective is the mandatory, DB-enforced field
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_nugget"):
        conn.execute("""
            CREATE TABLE marketing_nugget (
                nugget_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id                  INTEGER NOT NULL UNIQUE REFERENCES z_memory(memory_id),
                revision_id                INTEGER NOT NULL REFERENCES marketing_source_revision(revision_id),
                content_hash                TEXT NOT NULL,
                stream                      TEXT NOT NULL REFERENCES lookup_marketing_stream(value),
                evidence_purpose            TEXT REFERENCES lookup_evidence_purpose(value),
                perspective                 TEXT NOT NULL REFERENCES lookup_marketing_perspective(value),
                speaker_perspective_detail  TEXT REFERENCES lookup_marketing_speaker_detail(value),
                speaker_role                TEXT,
                use_permission              TEXT NOT NULL DEFAULT 'restricted_processing'
                                                REFERENCES lookup_marketing_use_permission(value),
                review_state                TEXT NOT NULL DEFAULT 'pending' REFERENCES lookup_review_status(value),
                problem_text        TEXT,
                situation_text      TEXT,
                desired_result_text TEXT,
                solution_text       TEXT,
                impact_text         TEXT,
                confidence               REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                extractor                TEXT NOT NULL,
                superseded_by_nugget_id  INTEGER REFERENCES marketing_nugget(nugget_id),
                created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (revision_id, content_hash)
            )
        """)
        conn.execute("CREATE INDEX idx_mkt_nugget_revision ON marketing_nugget(revision_id)")
        conn.execute("CREATE INDEX idx_mkt_nugget_review ON marketing_nugget(review_state)")
        conn.execute("CREATE INDEX idx_mkt_nugget_perspective ON marketing_nugget(perspective)")
        print("  marketing_nugget: created (perspective NOT NULL - mandatory)")
    else:
        print("  marketing_nugget: already present")

    # ------------------------------------------------------------------
    # marketing_nugget_topic - bucket assignment (cardinality enforced by
    # the application's single-transaction write; see verify_no_orphan_nuggets)
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_nugget_topic"):
        conn.execute("""
            CREATE TABLE marketing_nugget_topic (
                nugget_id           INTEGER NOT NULL REFERENCES marketing_nugget(nugget_id) ON DELETE CASCADE,
                topic_id            INTEGER NOT NULL REFERENCES marketing_topic(topic_id),
                relevance_rationale TEXT NOT NULL,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (nugget_id, topic_id)
            )
        """)
        print("  marketing_nugget_topic: created")
    else:
        print("  marketing_nugget_topic: already present")

    # ------------------------------------------------------------------
    # marketing_citation_location - extends citation with exact offsets/digest
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_citation_location"):
        conn.execute("""
            CREATE TABLE marketing_citation_location (
                citation_location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                citation_id           INTEGER NOT NULL REFERENCES citation(citation_id) ON DELETE CASCADE,
                nugget_id             INTEGER NOT NULL REFERENCES marketing_nugget(nugget_id) ON DELETE CASCADE,
                field_name            TEXT NOT NULL,
                start_offset          INTEGER NOT NULL CHECK (start_offset >= 0),
                end_offset            INTEGER NOT NULL CHECK (end_offset > start_offset),
                excerpt               TEXT NOT NULL,
                excerpt_sha256        TEXT NOT NULL,
                recording_start_ms    INTEGER,
                recording_end_ms      INTEGER,
                page_locator          TEXT,
                CHECK (field_name IN ('problem','situation','desired_result','solution','impact')),
                CHECK (recording_end_ms IS NULL OR recording_start_ms IS NULL
                       OR recording_end_ms >= recording_start_ms)
            )
        """)
        conn.execute("CREATE INDEX idx_mkt_citation_nugget ON marketing_citation_location(nugget_id)")
        print("  marketing_citation_location: created")
    else:
        print("  marketing_citation_location: already present")

    # ------------------------------------------------------------------
    # marketing_collection_item
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_collection_item"):
        conn.execute("""
            CREATE TABLE marketing_collection_item (
                collection_key  TEXT NOT NULL,
                nugget_id       INTEGER NOT NULL REFERENCES marketing_nugget(nugget_id),
                approval_state  TEXT NOT NULL DEFAULT 'pending' REFERENCES lookup_review_status(value),
                approver        TEXT,
                decision_at     TIMESTAMP,
                intended_use    TEXT,
                PRIMARY KEY (collection_key, nugget_id)
            )
        """)
        print("  marketing_collection_item: created")
    else:
        print("  marketing_collection_item: already present")

    # ------------------------------------------------------------------
    # marketing_access_grant
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_access_grant"):
        conn.execute("""
            CREATE TABLE marketing_access_grant (
                grant_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                audience        TEXT NOT NULL,
                collection_key  TEXT NOT NULL,
                nugget_id       INTEGER REFERENCES marketing_nugget(nugget_id),
                source_client_id INTEGER REFERENCES client(client_id),
                use_permission  TEXT NOT NULL REFERENCES lookup_marketing_use_permission(value),
                granted_by      TEXT NOT NULL,
                granted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked_at      TIMESTAMP,
                revoked_reason  TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_mkt_grant_audience ON marketing_access_grant(audience, collection_key)")
        print("  marketing_access_grant: created")
    else:
        print("  marketing_access_grant: already present")

    # ------------------------------------------------------------------
    # marketing_direction
    # ------------------------------------------------------------------
    if not table_exists(conn, "marketing_direction"):
        conn.execute("""
            CREATE TABLE marketing_direction (
                direction_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id               INTEGER NOT NULL REFERENCES z_memory(memory_id),
                state                   TEXT NOT NULL,
                effective_date          TEXT,
                confirming_authority    TEXT,
                supersedes_direction_id INTEGER REFERENCES marketing_direction(direction_id),
                created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (state IN ('proposed','confirmed','conflicting','superseded'))
            )
        """)
        print("  marketing_direction: created")
    else:
        print("  marketing_direction: already present")

    conn.commit()

    # ------------------------------------------------------------------
    # Documentation
    # ------------------------------------------------------------------
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)",
        [
            ("table", "marketing_topic", None, "The six Marketing intelligence buckets (Demand/Account/Sales/Talent/Pricing/Customer), hierarchical."),
            ("table", "marketing_source_revision", None, "Content-hash-tracked processing state per source_meeting/source_document for the Marketing pipeline."),
            ("table", "marketing_nugget", None, "One row per Marketing-relevant z_memory row. perspective (internal/external) is NOT NULL - mandatory, DB-enforced."),
            ("column", "marketing_nugget", "perspective", "Mandatory facingness: internal (Cortado speaker) or external (not Cortado). Cannot be NULL; a nugget without a resolved perspective must not be inserted."),
            ("table", "marketing_nugget_topic", None, "Bucket assignment join table. A nugget requires >=1 row here; enforced by the application's single-transaction write, not a DB constraint (SQLite has no deferred cross-table check)."),
            ("table", "marketing_citation_location", None, "Extends citation with exact character offsets and a SHA-256 digest per populated insight field."),
            ("table", "marketing_collection_item", None, "Approval staging for a named Marketing collection."),
            ("table", "marketing_access_grant", None, "Named-audience access grants, scoped by source client and use permission."),
            ("table", "marketing_direction", None, "Internal-direction state: proposed/confirmed/conflicting/superseded."),
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO z_glossary VALUES (?,?,?)",
        ("marketing nugget",
         "An atomic, individually-cited piece of Marketing-relevant knowledge, one per z_memory row. "
         "Every nugget must carry both an intelligence-type bucket (marketing_nugget_topic, >=1 row) "
         "and a mandatory internal/external perspective (marketing_nugget.perspective, NOT NULL) - "
         "neither is optional.",
         "A nugget tagged perspective='external', topic='demand' captures something a prospect said "
         "about unmet demand; it cannot be stored without both fields set."),
    )
    conn.commit()

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if row and row[0] == "v0.13.0":
        print("  z_version already at v0.13.0")
    else:
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES "
            "('v0.13.0', 'Add Marketing Intelligence extension: marketing_topic, "
            "marketing_source_revision, marketing_nugget (perspective NOT NULL), "
            "marketing_nugget_topic, marketing_citation_location, marketing_collection_item, "
            "marketing_access_grant, marketing_direction, plus 5 lookup tables')"
        )
        print("  bumped to v0.13.0")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
