"""
migrate_v0_11_0_comprehensive_schema_docs.py - Thorough z_schema documentation pass.

Walks every table in the database and ensures z_schema has a description for
the table itself and every column. Existing rows are upserted (description
overwritten with the latest copy) so future re-runs converge to the same
canonical text.

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


# ---------------------------------------------------------------------------
# Documentation registry. Keyed by (object_type, object_name, column_name).
# Tables: ('table', table_name, None, description)
# Columns: ('column', table_name, column_name, description)
# ---------------------------------------------------------------------------

DOCS = []


def T(table, description):
    DOCS.append(("table", table, None, description))


def C(table, column, description):
    DOCS.append(("column", table, column, description))


# =====================================================================
# BASE LIBRARY (unchanged from agent_memory original)
# =====================================================================

T("z_version", "Database schema version history. Each migration appends a row.")
C("z_version", "id", "Primary key")
C("z_version", "version", "Semantic version string, e.g. v0.11.0")
C("z_version", "description", "What changed in this version")
C("z_version", "created_at", "When this version was recorded")

T("z_schema", "Self-documentation: descriptions for tables, views, columns. The base library expects every object to have an entry here.")
C("z_schema", "object_type", "table | view | column")
C("z_schema", "object_name", "Name of the table or view")
C("z_schema", "column_name", "Column name when object_type='column'; NULL for table-level rows")
C("z_schema", "description", "Human-readable description")

T("z_glossary", "Domain-term definitions for humans/LLMs. NOT for FK lookups — see lookup_<concept> tables for that.")
C("z_glossary", "term", "The term being defined")
C("z_glossary", "definition", "Plain-language meaning")
C("z_glossary", "example_usage", "Example showing the term in context")

T("z_prompt_catalog", "Reusable LLM prompt templates indexed by trigger phrase.")
C("z_prompt_catalog", "id", "Primary key")
C("z_prompt_catalog", "created_at", "Created timestamp")
C("z_prompt_catalog", "updated_at", "Last modification")
C("z_prompt_catalog", "prompt_nickname", "Short identifier")
C("z_prompt_catalog", "question_pattern", "Trigger phrases that invoke this prompt")
C("z_prompt_catalog", "prompt_template", "Full prompt with placeholders")
C("z_prompt_catalog", "object_type", "Schema object this prompt operates on")
C("z_prompt_catalog", "object_name", "Specific table/view name")
C("z_prompt_catalog", "field_names", "Comma-separated columns referenced")
C("z_prompt_catalog", "target_audience", "Who uses this prompt (LLM, human)")
C("z_prompt_catalog", "example_response", "Reference output")
C("z_prompt_catalog", "is_active", "Soft-disable flag")
C("z_prompt_catalog", "version", "Prompt version tag")

T("z_script_catalog", "Registered access primitives — Python functions stored as text and exec'd by the script_loader. The single source of truth for db access patterns.")
C("z_script_catalog", "id", "Primary key")
C("z_script_catalog", "created_at", "Created timestamp")
C("z_script_catalog", "updated_at", "Last modification")
C("z_script_catalog", "script_name", "Unique function name (matches the def in script_body)")
C("z_script_catalog", "description", "What the script does")
C("z_script_catalog", "language", "python | sql")
C("z_script_catalog", "script_body", "The function source code (or SQL statement)")
C("z_script_catalog", "applies_to", "Comma-separated table names this script reads/writes")
C("z_script_catalog", "version_target", "Schema version this script was authored against")
C("z_script_catalog", "is_active", "Soft-disable flag")

T("z_script_params", "Parameter signatures for scripts. One row per parameter; ordered by ordinal.")
C("z_script_params", "script_name", "FK to z_script_catalog.script_name")
C("z_script_params", "method_name", "If the script defines a class with methods, the method name; else NULL")
C("z_script_params", "param_name", "Parameter name")
C("z_script_params", "param_type", "Type hint")
C("z_script_params", "default_value", "Default literal as a string; NULL if required")
C("z_script_params", "description", "What the parameter is for")
C("z_script_params", "ordinal", "1-based parameter position")

T("z_script_test", "TDD test cases for scripts. Run via the run_script_tests helper.")
C("z_script_test", "test_id", "Primary key")
C("z_script_test", "created_at", "Created timestamp")
C("z_script_test", "script_name", "FK to z_script_catalog.script_name")
C("z_script_test", "test_name", "Unique within script_name")
C("z_script_test", "description", "What this test asserts")
C("z_script_test", "test_input", "JSON: {'args': [...], 'kwargs': {...}}")
C("z_script_test", "expected_output", "JSON: subset that must match returned dict, or full literal")
C("z_script_test", "expected_error", "Exception class name when testing error path")
C("z_script_test", "setup_sql", "SQL run before the test (creates fixtures)")
C("z_script_test", "teardown_sql", "SQL run after the test (cleans up)")
C("z_script_test", "is_active", "Soft-disable flag")

T("z_memory", "Hot-fact / item-of-note store. Free-text observations extracted from sources, with sentence-transformer embeddings for semantic recall. The centerpiece of the agent_memory library — despite the z_ prefix this is core data, not infrastructure.")
C("z_memory", "memory_id", "Primary key")
C("z_memory", "content", "The note text (1-3 sentences)")
C("z_memory", "summary", "Optional brief summary")
C("z_memory", "memory_type", "fact|preference|lesson|decision|insight|error|security|risk|action_item|theme|recommendation|observation|open_question")
C("z_memory", "source", "meeting|document|user|agent|file|api — the source kind label")
C("z_memory", "source_id", "External id of the source (e.g. cortado meeting guid)")
C("z_memory", "tags", "Comma-separated tags")
C("z_memory", "importance", "1-10 priority; resists decay when high")
C("z_memory", "embedding", "BLOB: numpy float32 from sentence-transformers/all-MiniLM-L6-v2")
C("z_memory", "embedding_model", "Model identifier used to compute the embedding")
C("z_memory", "created_at", "When the row was written")
C("z_memory", "accessed_at", "Last retrieval timestamp")
C("z_memory", "access_count", "Times this row was returned by recall()")
C("z_memory", "expires_at", "Optional TTL")
C("z_memory", "is_active", "Soft-delete flag (True=visible)")
C("z_memory", "deleted_at", "Hard-purge timestamp; set during purge_memories()")
C("z_memory", "client_id", "FK to client.client_id; NULL = unattributed")
C("z_memory", "sensitivity", "FK to lookup_edge_sensitivity: routine|sensitive|hr_grade")


# =====================================================================
# CLIENT-KNOWLEDGE EXTENSION
# =====================================================================

T("client", "Tenant: a paying customer / account whose knowledge graph this row scopes. All entities/edges/memories scope to one client.")
C("client", "client_id", "Primary key")
C("client", "slug", "URL-safe short identifier (e.g. hig_growth_partners)")
C("client", "name", "Display name (e.g. 'H.I.G. Growth Partners, LLC')")
C("client", "box_folder_id", "Root Box folder ID owned by this client (used for attribution heuristics)")
C("client", "cortado_client_id", "External id used by cortadogroup.ai meeting platform")
C("client", "status", "active | paused | archived")
C("client", "sensitivity_default", "Default sensitivity tier for facts ingested for this client")
C("client", "attributes", "JSON: extensible per-client metadata")
C("client", "created_at", "Created timestamp")
C("client", "updated_at", "Last modification (auto-set via trigger)")

T("entity", "Typed node in the per-client knowledge graph (person, company, project, event, product, document, fund, ...). Entities never cross clients — same real-world person in two clients is two entity rows.")
C("entity", "entity_id", "Primary key")
C("entity", "client_id", "FK to client.client_id (CASCADE)")
C("entity", "type", "person|company|project|event|product|document|topic|department|deal|fund (extensible)")
C("entity", "canonical_name", "Display/canonical name. Aliases live in entity_alias.")
C("entity", "attributes", "JSON: type-specific identity attributes (NOT for facts — those go in edges)")
C("entity", "first_seen", "Earliest source timestamp this entity appears in")
C("entity", "last_seen", "Most recent source timestamp")
C("entity", "created_at", "Created timestamp")
C("entity", "updated_at", "Last modification (auto-set via trigger)")

T("entity_alias", "Many-to-one mapping: alternate strings → one entity, scoped per-client. Holds nicknames, initials, sfids, system guids, codenames, and known transcription drifts.")
C("entity_alias", "alias_id", "Primary key")
C("entity_alias", "client_id", "FK to client.client_id (CASCADE)")
C("entity_alias", "entity_id", "FK to entity.entity_id (CASCADE)")
C("entity_alias", "alias_text", "The string that resolves to the entity (case-insensitive lookup)")
C("entity_alias", "alias_kind", "FK to lookup_alias_kind: name|nickname|formal_name|codename|transcription_drift|sfid|cortado_*_guid|box_folder_id|...")
C("entity_alias", "confidence", "FK to lookup_identity_confidence: certain|likely|guessed")
C("entity_alias", "resolved_by", "Provenance label: 'seeder:cortado:v0.3.0', 'extractor:multipass:v0.7.0', 'david_correction', etc.")
C("entity_alias", "created_at", "Created timestamp")

T("predicate", "Controlled vocabulary for edge predicates. Starts empty; predicates earn approval by being proposed during extraction.")
C("predicate", "predicate_id", "Primary key")
C("predicate", "name", "Predicate identifier (snake_case): reports_to, tension_with, prefers, ...")
C("predicate", "inverse_name", "Inverse relation if applicable (reports_to ↔ manages)")
C("predicate", "subject_types", "JSON array of valid entity.type values for the subject side")
C("predicate", "object_types", "JSON array of valid entity.type values for the object side (or 'literal')")
C("predicate", "description", "Plain-language meaning")
C("predicate", "status", "FK to lookup_predicate_status: proposed|approved|deprecated")
C("predicate", "proposed_count", "Number of times an extractor has proposed this predicate")
C("predicate", "cardinality", "FK to lookup_predicate_cardinality: single_valued|multi_valued. Used by the consolidator to detect real discrepancies.")
C("predicate", "created_at", "Created timestamp")
C("predicate", "updated_at", "Last modification (auto-set via trigger)")

T("edge", "Typed relationship: (subject) --predicate--> (object_or_literal). Every edge is supersedable — when a fact changes, INSERT a new edge with supersedes_id pointing at the old one and mark the old as status='superseded'. Never UPDATE; preserves history.")
C("edge", "edge_id", "Primary key")
C("edge", "client_id", "FK to client.client_id (CASCADE)")
C("edge", "subject_id", "FK to entity.entity_id (CASCADE) — the subject of the relationship")
C("edge", "predicate_id", "FK to predicate.predicate_id (RESTRICT)")
C("edge", "object_id", "FK to entity.entity_id (CASCADE) — entity-side object; NULL when object_literal is used")
C("edge", "object_literal", "Literal value when the object isn't an entity (a date, amount, raw string, URL)")
C("edge", "object_literal_type", "string|date|datetime|year|quarter|month|date_range|currency|int|url|phone")
C("edge", "notes", "Free-text elaboration (e.g. 'tense since Q2 budget fight')")
C("edge", "justification", "Verbatim quote from the source supporting this edge — required from extractor")
C("edge", "confidence", "FK to lookup_edge_confidence: stated|implied|factual|pattern|confirmed")
C("edge", "sensitivity", "FK to lookup_edge_sensitivity: routine|sensitive|hr_grade — drives bot retrieval ACL")
C("edge", "status", "FK to lookup_edge_status: active|superseded|retracted")
C("edge", "supersedes_id", "When this edge replaces an older one, FK back to that edge")
C("edge", "first_observed_ts", "Earliest source timestamp among citations")
C("edge", "last_corroborated_ts", "Most recent source timestamp that supports this edge")
C("edge", "created_at", "Created timestamp")
C("edge", "updated_at", "Last modification (auto-set via trigger)")

T("source_meeting", "Thin pointer to a meeting record (typically cortadogroup.ai). Fields like attendees / action_items / decisions / summary are NOT mirrored — fetch from cortado on demand. This row exists primarily as a citation target and to record the meeting's url/occurred_at/attribution.")
C("source_meeting", "source_meeting_id", "Primary key")
C("source_meeting", "client_id", "FK to client.client_id (SET NULL on delete)")
C("source_meeting", "external_id", "ID in the upstream meeting platform (cortado meeting guid)")
C("source_meeting", "title", "Cached meeting title (denormalization helper)")
C("source_meeting", "occurred_at", "When the meeting happened")
C("source_meeting", "raw_metadata", "JSON: full upstream record snapshot for replay")
C("source_meeting", "attribution_confidence", "FK to lookup_identity_confidence: how sure we are of client_id assignment")
C("source_meeting", "attribution_source", "FK to lookup_attribution_source: how client_id was assigned")
C("source_meeting", "fetched_at", "When we last pulled this row from upstream")
C("source_meeting", "created_at", "Created timestamp")
C("source_meeting", "updated_at", "Last modification (auto-set via trigger)")
C("source_meeting", "url", "Canonical clickable URL (e.g. https://cg.cortadogroup.ai/meetings/console/<guid>/)")

T("source_document", "Thin pointer to a document (typically Box). Title/doc_type/mime_type are NOT mirrored — fetch from box-search on demand. Citation target.")
C("source_document", "source_document_id", "Primary key")
C("source_document", "client_id", "FK to client.client_id (SET NULL on delete)")
C("source_document", "external_id", "ID in upstream provider (Box file id)")
C("source_document", "provider", "box | drive | local | other")
C("source_document", "folder_path", "Full folder path at fetch time (often the strongest attribution signal)")
C("source_document", "modified_at", "Source-system modified timestamp")
C("source_document", "raw_metadata", "JSON: full upstream record")
C("source_document", "attribution_confidence", "FK to lookup_identity_confidence")
C("source_document", "attribution_source", "FK to lookup_attribution_source")
C("source_document", "fetched_at", "When we last pulled from upstream")
C("source_document", "created_at", "Created timestamp")
C("source_document", "updated_at", "Last modification (auto-set via trigger)")
C("source_document", "url", "Canonical clickable URL (e.g. https://cortadogroup.app.box.com/file/<id>)")

T("citation", "Polymorphic provenance: every fact (memory or edge) cites at least one source. Either source_id (FK to local source_meeting/source_document) OR source_external_id (external system guid).")
C("citation", "citation_id", "Primary key")
C("citation", "cited_kind", "FK to lookup_citation_kind: memory|edge|entity")
C("citation", "cited_id", "Primary key value within cited_kind table")
C("citation", "source_kind", "FK to lookup_citation_source: meeting|document|manual|cortado|salesforce|asana|slack")
C("citation", "source_id", "FK to source_meeting/source_document.id when source_kind in (meeting, document); NULL otherwise")
C("citation", "source_external_id", "External system identifier (cortado guid, sfid, asana gid, slack ts) when source_id is NULL")
C("citation", "source_ts", "Timestamp of the source moment (e.g. when the meeting occurred)")
C("citation", "quote", "Verbatim snippet from the source supporting the cited fact (10-200 chars typical)")
C("citation", "offset_start", "Optional char offset where quote begins in source content")
C("citation", "offset_end", "Optional char offset where quote ends")
C("citation", "extracted_by", "Worker version label (e.g. 'extractor:multipass:v0.7.0') or human user")
C("citation", "created_at", "Created timestamp")

T("attribution_event", "Append-only audit log: every (fact → client) attribution decision, including re-attributions. Immutable — UPDATEs are blocked by trigger.")
C("attribution_event", "attribution_event_id", "Primary key")
C("attribution_event", "target_kind", "FK to lookup_attribution_kind: memory|source_meeting|source_document|edge|entity")
C("attribution_event", "target_id", "Primary key within target_kind")
C("attribution_event", "client_id", "Client this fact was attributed TO at this event (NULL = un-attributed)")
C("attribution_event", "previous_client_id", "Client this fact was attributed TO before this event (NULL on first attribution)")
C("attribution_event", "confidence", "FK to lookup_identity_confidence: certain|likely|guessed")
C("attribution_event", "source", "FK to lookup_attribution_source: how the decision was made")
C("attribution_event", "attributed_by", "Worker version or human user")
C("attribution_event", "rationale", "Short explanation, especially for manual re-attribution")
C("attribution_event", "created_at", "Created timestamp (immutable; row itself is append-only)")

T("memory_entity", "Join table: links z_memory rows (items of note) to entities they're about. Enforced FK CASCADE both ways. Indexed both directions for efficient 'all notes about X' queries.")
C("memory_entity", "memory_entity_id", "Primary key")
C("memory_entity", "memory_id", "FK to z_memory.memory_id (CASCADE)")
C("memory_entity", "entity_id", "FK to entity.entity_id (CASCADE)")
C("memory_entity", "role", "FK to lookup_memory_role: subject|mentioned|attendee|about|blocker|champion")
C("memory_entity", "created_at", "Created timestamp")

T("drift_hypothesis", "Pending claim: 'observed_token in this source might be a transcription drift of candidate_entity'. Reviewed manually before becoming an entity_alias.")
C("drift_hypothesis", "drift_hypothesis_id", "Primary key")
C("drift_hypothesis", "client_id", "FK to client.client_id (CASCADE)")
C("drift_hypothesis", "observed_token", "The string that appeared in the source (e.g. 'Dedle')")
C("drift_hypothesis", "candidate_entity_id", "FK to entity.entity_id (CASCADE) — what we hypothesize the token is")
C("drift_hypothesis", "rationale", "Why the extractor thinks this is a drift, not a separate entity")
C("drift_hypothesis", "supporting_quote", "Verbatim transcript snippet showing the observed token")
C("drift_hypothesis", "source_kind", "Source kind: meeting|document|manual")
C("drift_hypothesis", "source_external_id", "External guid where the token appeared")
C("drift_hypothesis", "source_ts", "When the source was created")
C("drift_hypothesis", "proposed_by", "Worker version (e.g. 'extractor:multipass:v0.7.0')")
C("drift_hypothesis", "status", "FK to lookup_drift_status: pending|approved|rejected")
C("drift_hypothesis", "reviewed_by", "Who approved/rejected; NULL while pending")
C("drift_hypothesis", "reviewed_at", "When approved/rejected")
C("drift_hypothesis", "created_at", "Created timestamp")


# =====================================================================
# LOOKUP TABLES (v0.9.0)
# =====================================================================

LOOKUP_TABLES = [
    ("lookup_alias_kind",            "Vocabulary for entity_alias.alias_kind"),
    ("lookup_edge_confidence",       "Vocabulary for edge.confidence"),
    ("lookup_edge_sensitivity",      "Vocabulary for edge.sensitivity AND z_memory.sensitivity"),
    ("lookup_edge_status",           "Vocabulary for edge.status (active|superseded|retracted)"),
    ("lookup_citation_kind",         "Vocabulary for citation.cited_kind (memory|edge|entity)"),
    ("lookup_citation_source",       "Vocabulary for citation.source_kind (meeting|document|cortado|...)"),
    ("lookup_attribution_kind",      "Vocabulary for attribution_event.target_kind"),
    ("lookup_attribution_source",    "Vocabulary for attribution_source columns (box_folder|attendee_domain|...)"),
    ("lookup_drift_status",          "Vocabulary for drift_hypothesis.status (pending|approved|rejected)"),
    ("lookup_memory_role",           "Vocabulary for memory_entity.role"),
    ("lookup_predicate_status",      "Vocabulary for predicate.status (proposed|approved|deprecated)"),
    ("lookup_predicate_cardinality", "Vocabulary for predicate.cardinality (single_valued|multi_valued)"),
    ("lookup_identity_confidence",   "Vocabulary for entity_alias.confidence, attribution_event.confidence, source_*.attribution_confidence"),
    ("lookup_review_status",         "Vocabulary for general review-status fields"),
]
for name, desc in LOOKUP_TABLES:
    T(name, desc + ". One row per allowed value. Add a new value = INSERT; deprecate = SET status='deprecated'; supersede = SET superseded_by.")
    C(name, "value", "Allowed value (PRIMARY KEY)")
    C(name, "description", "Plain-language meaning of this value")
    C(name, "status", "FK to lookup_predicate_status: proposed|approved|deprecated")
    C(name, "superseded_by", "If deprecated, points at the replacement value (self-FK)")
    C(name, "created_at", "Created timestamp")


# =====================================================================
# Apply
# =====================================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()
    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.11.0 (comprehensive schema docs)...")

    n_tables = 0
    n_columns = 0
    for object_type, object_name, column_name, description in DOCS:
        conn.execute(
            "INSERT OR REPLACE INTO z_schema (object_type, object_name, column_name, description) VALUES (?,?,?,?)",
            (object_type, object_name, column_name, description),
        )
        if object_type == "table":
            n_tables += 1
        else:
            n_columns += 1
    conn.commit()

    print(f"  upserted {n_tables} table descriptions, {n_columns} column descriptions")

    # Audit: any tables without entries?
    all_tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    documented = {r[0] for r in conn.execute(
        "SELECT object_name FROM z_schema WHERE object_type='table'"
    ).fetchall()}
    missing = [t for t in all_tables if t not in documented]
    if missing:
        print(f"  WARN: {len(missing)} tables still without z_schema row: {missing}")
    else:
        print(f"  ✓ all {len(all_tables)} tables documented")

    # Per-table column audit
    incomplete = []
    for t in all_tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        documented_cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM z_schema WHERE object_type='column' AND object_name=?", (t,)
        ).fetchall()}
        missing_cols = [c for c in cols if c not in documented_cols]
        if missing_cols:
            incomplete.append((t, missing_cols))
    if incomplete:
        print(f"  WARN: {len(incomplete)} tables with undocumented columns:")
        for t, cols in incomplete[:10]:
            print(f"    {t}: {cols}")
        if len(incomplete) > 10:
            print(f"    ... and {len(incomplete) - 10} more")
    else:
        print(f"  ✓ all columns across all {len(all_tables)} tables documented")

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.11.0"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.11.0', 'Comprehensive z_schema documentation pass — every table and every column has a description')"
        )
        print("  bumped to v0.11.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
