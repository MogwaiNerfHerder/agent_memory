"""
create_client_knowledge_schema.py - Additive schema for multi-tenant client knowledge.

Extends an existing agent_memory.db with the tables needed to back per-client
knowledge bots (Box docs + cortadogroup.ai meetings → entities, edges, citations).

Adds:
    - client                : the tenant
    - entity                : typed nodes (person, company, project, ...)
    - entity_alias          : many strings -> one entity (per client)
    - predicate             : controlled vocabulary (status: proposed/approved/deprecated)
    - edge                  : (subject, predicate, object) with provenance + supersession
    - source_meeting        : pointer to cortadogroup.ai meeting records
    - source_document       : pointer to Box documents
    - citation              : polymorphic many-to-many between facts and sources
    - attribution_event     : append-only log of (fact -> client) decisions
    - z_memory.client_id    : added column + index (NULL allowed for legacy rows)

Idempotent: safe to re-run on an existing db.

Usage:
    python create_client_knowledge_schema.py
    python create_client_knowledge_schema.py --path /path/to/agent_memory.db
"""

import argparse
import os
import sqlite3
import sys


def column_exists(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def trigger_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upsert_schema(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO z_schema (object_type, object_name, column_name, description) VALUES (?,?,?,?)",
        rows,
    )


def upsert_glossary(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO z_glossary (term, definition, example_usage) VALUES (?,?,?)",
        rows,
    )


def upsert_script(conn, name, description, body, applies_to, version_target="v0.1.0"):
    conn.execute(
        """INSERT INTO z_script_catalog
            (script_name, description, language, script_body, applies_to, version_target, is_active)
           VALUES (?, ?, 'python', ?, ?, ?, 1)
           ON CONFLICT(script_name) DO UPDATE SET
                description=excluded.description,
                script_body=excluded.script_body,
                applies_to=excluded.applies_to,
                version_target=excluded.version_target,
                updated_at=CURRENT_TIMESTAMP""",
        (name, description, body, applies_to, version_target),
    )


def upsert_script_params(conn, script_name, params):
    conn.execute("DELETE FROM z_script_params WHERE script_name=?", (script_name,))
    rows = [
        (script_name, None, p[0], p[1], p[2], p[3], idx + 1)
        for idx, p in enumerate(params)
    ]
    conn.executemany(
        "INSERT INTO z_script_params (script_name, method_name, param_name, param_type, default_value, description, ordinal) VALUES (?,?,?,?,?,?,?)",
        rows,
    )


def add_updated_at_trigger(conn, table, pk_col):
    name = f"trg_{table}_updated_at"
    if trigger_exists(conn, name):
        return
    conn.execute(
        f"""
        CREATE TRIGGER {name}
        AFTER UPDATE ON {table}
        FOR EACH ROW
        BEGIN
            UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE {pk_col} = NEW.{pk_col};
        END
        """
    )


# =============================================================================
# SCHEMA
# =============================================================================

def create_schema(conn):
    # --- client ---------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client (
            client_id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            box_folder_id TEXT,
            cortado_client_id TEXT,
            status TEXT DEFAULT 'active',
            sensitivity_default TEXT DEFAULT 'routine',
            attributes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    add_updated_at_trigger(conn, "client", "client_id")

    # --- entity ---------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES client(client_id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            attributes TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_id, type, canonical_name)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_client_type ON entity(client_id, type)")
    add_updated_at_trigger(conn, "entity", "entity_id")

    # --- entity_alias ---------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_alias (
            alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES client(client_id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
            alias_text TEXT NOT NULL,
            alias_kind TEXT,
            confidence TEXT DEFAULT 'likely',
            resolved_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_id, alias_text, entity_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_alias_lookup ON entity_alias(client_id, alias_text)"
    )

    # --- predicate (controlled vocab, starts empty) ---------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS predicate (
            predicate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            inverse_name TEXT,
            subject_types TEXT,
            object_types TEXT,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            proposed_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    add_updated_at_trigger(conn, "predicate", "predicate_id")

    if not trigger_exists(conn, "trg_predicate_status_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_predicate_status_check
            BEFORE INSERT ON predicate
            WHEN NEW.status NOT IN ('proposed','approved','deprecated')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid predicate.status: must be proposed, approved, or deprecated');
            END
            """
        )

    # --- edge -----------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge (
            edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES client(client_id) ON DELETE CASCADE,
            subject_id INTEGER NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
            predicate_id INTEGER NOT NULL REFERENCES predicate(predicate_id) ON DELETE RESTRICT,
            object_id INTEGER REFERENCES entity(entity_id) ON DELETE CASCADE,
            object_literal TEXT,
            notes TEXT,
            justification TEXT,
            confidence TEXT DEFAULT 'stated',
            sensitivity TEXT DEFAULT 'routine',
            status TEXT NOT NULL DEFAULT 'active',
            supersedes_id INTEGER REFERENCES edge(edge_id),
            first_observed_ts TIMESTAMP,
            last_corroborated_ts TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edge_client_subject ON edge(client_id, subject_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edge_client_object ON edge(client_id, object_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edge_predicate ON edge(predicate_id, status)"
    )
    add_updated_at_trigger(conn, "edge", "edge_id")

    if not trigger_exists(conn, "trg_edge_status_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_edge_status_check
            BEFORE INSERT ON edge
            WHEN NEW.status NOT IN ('active','superseded','retracted')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid edge.status: must be active, superseded, or retracted');
            END
            """
        )

    if not trigger_exists(conn, "trg_edge_confidence_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_edge_confidence_check
            BEFORE INSERT ON edge
            WHEN NEW.confidence NOT IN ('stated','implied','factual','pattern','confirmed')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid edge.confidence: must be stated, implied, factual, pattern, or confirmed');
            END
            """
        )

    # --- source_meeting -------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_meeting (
            source_meeting_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES client(client_id) ON DELETE SET NULL,
            external_id TEXT NOT NULL UNIQUE,
            title TEXT,
            occurred_at TIMESTAMP,
            attendees TEXT,
            summary TEXT,
            action_items TEXT,
            decisions TEXT,
            transcript_url TEXT,
            raw_metadata TEXT,
            attribution_confidence TEXT DEFAULT 'guessed',
            attribution_source TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_meeting_client ON source_meeting(client_id, occurred_at DESC)"
    )
    add_updated_at_trigger(conn, "source_meeting", "source_meeting_id")

    # --- source_document ------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_document (
            source_document_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES client(client_id) ON DELETE SET NULL,
            external_id TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT 'box',
            title TEXT,
            doc_type TEXT,
            folder_path TEXT,
            mime_type TEXT,
            modified_at TIMESTAMP,
            content_url TEXT,
            raw_metadata TEXT,
            attribution_confidence TEXT DEFAULT 'guessed',
            attribution_source TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_document_client ON source_document(client_id, modified_at DESC)"
    )
    add_updated_at_trigger(conn, "source_document", "source_document_id")

    # --- citation (polymorphic) ----------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS citation (
            citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cited_kind TEXT NOT NULL,
            cited_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            source_ts TIMESTAMP,
            quote TEXT,
            offset_start INTEGER,
            offset_end INTEGER,
            extracted_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cited_kind, cited_id, source_kind, source_id, offset_start, offset_end)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citation_cited ON citation(cited_kind, cited_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citation_source ON citation(source_kind, source_id)"
    )

    if not trigger_exists(conn, "trg_citation_kind_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_citation_kind_check
            BEFORE INSERT ON citation
            WHEN NEW.cited_kind NOT IN ('memory','edge','entity')
              OR NEW.source_kind NOT IN ('meeting','document','manual')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid citation kind: cited_kind in (memory,edge,entity); source_kind in (meeting,document,manual)');
            END
            """
        )

    # --- attribution_event (append-only) -------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attribution_event (
            attribution_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_kind TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            client_id INTEGER REFERENCES client(client_id) ON DELETE SET NULL,
            previous_client_id INTEGER REFERENCES client(client_id) ON DELETE SET NULL,
            confidence TEXT NOT NULL DEFAULT 'guessed',
            source TEXT NOT NULL,
            attributed_by TEXT,
            rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attribution_target ON attribution_event(target_kind, target_id, created_at DESC)"
    )

    if not trigger_exists(conn, "trg_attribution_kind_check"):
        conn.execute(
            """
            CREATE TRIGGER trg_attribution_kind_check
            BEFORE INSERT ON attribution_event
            WHEN NEW.target_kind NOT IN ('memory','source_meeting','source_document','edge','entity')
            BEGIN
                SELECT RAISE(FAIL, 'Invalid attribution_event.target_kind');
            END
            """
        )

    if not trigger_exists(conn, "trg_attribution_immutable"):
        conn.execute(
            """
            CREATE TRIGGER trg_attribution_immutable
            BEFORE UPDATE ON attribution_event
            BEGIN
                SELECT RAISE(FAIL, 'attribution_event is append-only');
            END
            """
        )

    # --- z_memory.client_id ---------------------------------------------------
    if not column_exists(conn, "z_memory", "client_id"):
        conn.execute("ALTER TABLE z_memory ADD COLUMN client_id INTEGER REFERENCES client(client_id) ON DELETE SET NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_client ON z_memory(client_id, is_active, importance DESC)")


# =============================================================================
# DOCUMENTATION
# =============================================================================

SCHEMA_DOCS = [
    # client
    ("table", "client", None, "Tenant: a customer/account whose knowledge graph this row scopes to"),
    ("column", "client", "slug", "URL-safe short identifier (e.g., hig_growth_partners)"),
    ("column", "client", "name", "Display name"),
    ("column", "client", "box_folder_id", "Root Box folder ID owned by this client (used for attribution)"),
    ("column", "client", "cortado_client_id", "External id used by cortadogroup.ai meeting platform"),
    ("column", "client", "status", "active | paused | archived"),
    ("column", "client", "sensitivity_default", "Default sensitivity tier for facts ingested for this client"),
    ("column", "client", "attributes", "JSON: extensible per-client metadata"),

    # entity
    ("table", "entity", None, "Typed node in the per-client knowledge graph (person, company, project, ...)"),
    ("column", "entity", "client_id", "FK: which client this entity belongs to (entities are NOT shared across clients)"),
    ("column", "entity", "type", "person | company | project | product | topic | department | deal (extensible)"),
    ("column", "entity", "canonical_name", "Display/canonical name. Aliases live in entity_alias."),
    ("column", "entity", "attributes", "JSON: type-specific fields (email, role, etc.)"),
    ("column", "entity", "first_seen", "Earliest source timestamp this entity appears in"),
    ("column", "entity", "last_seen", "Most recent source timestamp"),

    # entity_alias
    ("table", "entity_alias", None, "Many alternate strings -> one entity, scoped per client (BM != BM across clients)"),
    ("column", "entity_alias", "alias_text", "The string seen in source material"),
    ("column", "entity_alias", "alias_kind", "name | initials | email | handle | nickname"),
    ("column", "entity_alias", "confidence", "certain | likely | guessed (resolution confidence)"),
    ("column", "entity_alias", "resolved_by", "human:<user> | worker:<version> | manual"),

    # predicate
    ("table", "predicate", None, "Controlled vocabulary for edge predicates. Starts empty; predicates earn approval."),
    ("column", "predicate", "name", "Predicate identifier (snake_case): reports_to, tension_with, prefers"),
    ("column", "predicate", "inverse_name", "Inverse relation if applicable (reports_to <-> manages)"),
    ("column", "predicate", "subject_types", "JSON array of valid entity.type for subject"),
    ("column", "predicate", "object_types", "JSON array of valid entity.type for object (or 'literal')"),
    ("column", "predicate", "status", "proposed | approved | deprecated"),
    ("column", "predicate", "proposed_count", "How many times this predicate has been proposed by extractors"),

    # edge
    ("table", "edge", None, "Typed relationship: (subject) --predicate--> (object). Versioned via supersedes_id."),
    ("column", "edge", "subject_id", "FK to entity"),
    ("column", "edge", "predicate_id", "FK to predicate (controlled vocab)"),
    ("column", "edge", "object_id", "FK to entity (NULL if object_literal used)"),
    ("column", "edge", "object_literal", "Literal value when object isn't an entity (a date, amount, raw string)"),
    ("column", "edge", "notes", "Free-text elaboration (e.g., 'tense since Q2 budget fight')"),
    ("column", "edge", "justification", "Why we believe this — short reasoning trace"),
    ("column", "edge", "confidence", "stated | implied | factual | pattern | confirmed"),
    ("column", "edge", "sensitivity", "routine | sensitive | hr_grade (drives ACL on bot retrieval)"),
    ("column", "edge", "status", "active | superseded | retracted"),
    ("column", "edge", "supersedes_id", "If this edge replaced an older one, FK to that edge"),
    ("column", "edge", "first_observed_ts", "Earliest source ts among citations"),
    ("column", "edge", "last_corroborated_ts", "Most recent source ts that supports this edge"),

    # source_meeting
    ("table", "source_meeting", None, "Pointer to a meeting record (typically in cortadogroup.ai)"),
    ("column", "source_meeting", "external_id", "ID in the upstream meeting platform"),
    ("column", "source_meeting", "attendees", "JSON: structured attendee list pulled from platform"),
    ("column", "source_meeting", "action_items", "JSON: pre-extracted action items (don't re-extract)"),
    ("column", "source_meeting", "decisions", "JSON: pre-extracted decisions"),
    ("column", "source_meeting", "raw_metadata", "JSON: full upstream record for replay"),
    ("column", "source_meeting", "attribution_confidence", "certain | likely | guessed (how sure about client_id)"),
    ("column", "source_meeting", "attribution_source", "box_folder | attendee_domain | platform_tag | content_inference | manual"),

    # source_document
    ("table", "source_document", None, "Pointer to a document (Box default; provider extensible)"),
    ("column", "source_document", "external_id", "ID in upstream provider (Box file id)"),
    ("column", "source_document", "provider", "box | drive | local | other"),
    ("column", "source_document", "folder_path", "Full folder path at fetch time (often the strongest attribution signal)"),
    ("column", "source_document", "raw_metadata", "JSON: full upstream record"),

    # citation
    ("table", "citation", None, "Polymorphic provenance: every fact (memory or edge) cites at least one source"),
    ("column", "citation", "cited_kind", "memory | edge | entity"),
    ("column", "citation", "cited_id", "ID within cited_kind"),
    ("column", "citation", "source_kind", "meeting | document | manual"),
    ("column", "citation", "source_id", "FK to source_meeting / source_document (or NULL for manual)"),
    ("column", "citation", "quote", "Verbatim snippet supporting the cited fact, if available"),
    ("column", "citation", "offset_start", "Char offset in source content (optional)"),
    ("column", "citation", "offset_end", "Char offset in source content (optional)"),
    ("column", "citation", "extracted_by", "worker:<version> | human:<user>"),

    # attribution_event
    ("table", "attribution_event", None, "Append-only log: every (fact -> client) attribution decision, including re-attributions"),
    ("column", "attribution_event", "target_kind", "memory | source_meeting | source_document | edge | entity"),
    ("column", "attribution_event", "previous_client_id", "Client this fact was attributed to before this event (NULL on first)"),
    ("column", "attribution_event", "confidence", "certain | likely | guessed"),
    ("column", "attribution_event", "source", "box_folder | attendee_domain | platform_tag | content_inference | manual"),
    ("column", "attribution_event", "attributed_by", "worker:<version> | human:<user>"),
    ("column", "attribution_event", "rationale", "Short note explaining the decision (esp. for manual re-attribution)"),

    # z_memory.client_id
    ("column", "z_memory", "client_id", "FK to client. NULL = unattributed/ambiguous; bots filter on this."),
]


GLOSSARY = [
    ("client", "A tenant in the knowledge platform; all entities/edges/memories scope to one client.",
     "client.slug='hig_growth_partners'"),
    ("entity", "A typed node in a client's knowledge graph (person, company, project, ...).",
     "Barry Marsh is a person entity in the Hig client graph"),
    ("alias", "An alternate string that resolves to a known entity within a single client.",
     "'BM' is an alias for entity 'Barry Marsh' in Hig; in another client 'BM' might mean something else"),
    ("predicate", "A typed relationship between two entities (or entity and literal). Controlled vocabulary.",
     "reports_to, tension_with, champions, prefers"),
    ("edge", "An instance of a predicate connecting two entities, with provenance and confidence.",
     "(Barry) --tension_with--> (Tommy), confidence=pattern, citations=[mtg_2026-02-14, mtg_2026-04-02]"),
    ("citation", "A pointer from a fact (memory/edge/entity) to the source that supports it.",
     "Edge 17 cites meeting 412 with quote 'I really don't trust Tommy's numbers anymore'"),
    ("attribution", "The decision that a given fact belongs to a given client. Versioned via attribution_event.",
     "Document 991 was attributed to Hig (likely, by attendee_domain), later re-attributed to Acme (certain, by manual)"),
    ("hot fact", "A z_memory row written from a single source (per-meeting or per-doc); single-observation only.",
     "smart_remember_local writes hot facts"),
    ("cold fact", "An edge written by the consolidator after corroborating across multiple hot facts.",
     "Confidence='pattern' edges are cold facts"),
    ("sensitivity", "Access tier: routine, sensitive, hr_grade. Drives bot retrieval ACL.",
     "Interpersonal observations default to 'sensitive'; HR-grade items get extra controls"),
]


# =============================================================================
# SCRIPTS (registered in z_script_catalog so agents can discover them)
# =============================================================================

RESOLVE_OR_CREATE_ENTITY = r'''
def resolve_or_create_entity(conn, client_id: int, alias_text: str, entity_type: str = None,
                              canonical_name: str = None, alias_kind: str = None,
                              min_alias_confidence: str = "likely", resolved_by: str = "worker:v0.1.0"):
    """Look up an alias within a client; create entity + alias if not found.

    Resolution is per-client (a string never crosses clients). If the alias is short
    (<=3 chars) and not already known, returns {"resolved": False, "reason": "ambiguous_short_token"}
    so the caller can park it for review rather than minting a node.
    """
    cur = conn.cursor()
    row = cur.execute(
        """SELECT a.entity_id, a.confidence, e.canonical_name, e.type
             FROM entity_alias a JOIN entity e ON e.entity_id = a.entity_id
            WHERE a.client_id=? AND a.alias_text=? COLLATE NOCASE
            ORDER BY (CASE a.confidence WHEN 'certain' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END)
            LIMIT 1""",
        (client_id, alias_text),
    ).fetchone()
    if row:
        return {"resolved": True, "entity_id": row[0], "confidence": row[1],
                "canonical_name": row[2], "type": row[3], "created": False}

    if len(alias_text.strip()) <= 3:
        return {"resolved": False, "reason": "ambiguous_short_token", "alias_text": alias_text}

    if not entity_type:
        return {"resolved": False, "reason": "missing_entity_type", "alias_text": alias_text}

    canonical = canonical_name or alias_text.strip()
    cur.execute(
        """INSERT INTO entity (client_id, type, canonical_name, first_seen, last_seen)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT(client_id, type, canonical_name) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",
        (client_id, entity_type, canonical),
    )
    entity_id = cur.execute(
        "SELECT entity_id FROM entity WHERE client_id=? AND type=? AND canonical_name=?",
        (client_id, entity_type, canonical),
    ).fetchone()[0]

    cur.execute(
        """INSERT OR IGNORE INTO entity_alias (client_id, entity_id, alias_text, alias_kind, confidence, resolved_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, entity_id, alias_text, alias_kind, min_alias_confidence, resolved_by),
    )
    conn.commit()
    return {"resolved": True, "entity_id": entity_id, "confidence": min_alias_confidence,
            "canonical_name": canonical, "type": entity_type, "created": True}
'''


PROPOSE_PREDICATE = r'''
def propose_predicate(conn, name: str, description: str = None, subject_types=None,
                       object_types=None, inverse_name: str = None):
    """Register or bump a proposed predicate. Approved predicates are unchanged."""
    import json
    cur = conn.cursor()
    row = cur.execute("SELECT predicate_id, status, proposed_count FROM predicate WHERE name=?", (name,)).fetchone()
    if row:
        if row[1] == 'approved':
            return {"predicate_id": row[0], "status": "approved", "proposed_count": row[2]}
        cur.execute("UPDATE predicate SET proposed_count = proposed_count + 1 WHERE predicate_id=?", (row[0],))
        conn.commit()
        return {"predicate_id": row[0], "status": row[1], "proposed_count": row[2] + 1}

    cur.execute(
        """INSERT INTO predicate (name, description, subject_types, object_types, inverse_name, status, proposed_count)
           VALUES (?, ?, ?, ?, ?, 'proposed', 1)""",
        (name, description,
         json.dumps(subject_types) if subject_types else None,
         json.dumps(object_types) if object_types else None,
         inverse_name),
    )
    conn.commit()
    return {"predicate_id": cur.lastrowid, "status": "proposed", "proposed_count": 1, "created": True}
'''


ATTRIBUTE_FACT = r'''
def attribute_fact(conn, target_kind: str, target_id: int, client_id, source: str,
                    confidence: str = "guessed", attributed_by: str = "worker:v0.1.0",
                    rationale: str = None):
    """Attribute (or re-attribute) a fact to a client. Append-only audit via attribution_event.

    Updates the target row's client_id (where applicable) and writes an attribution_event.
    Re-attribution is supported: pass a different client_id; previous_client_id is captured.
    """
    cur = conn.cursor()
    table_map = {
        "memory": ("z_memory", "memory_id"),
        "source_meeting": ("source_meeting", "source_meeting_id"),
        "source_document": ("source_document", "source_document_id"),
        "edge": ("edge", "edge_id"),
        "entity": ("entity", "entity_id"),
    }
    if target_kind not in table_map:
        raise ValueError(f"Invalid target_kind: {target_kind}")
    table, pk = table_map[target_kind]

    prev_row = cur.execute(f"SELECT client_id FROM {table} WHERE {pk}=?", (target_id,)).fetchone()
    if prev_row is None:
        raise ValueError(f"{target_kind} id={target_id} not found")
    previous_client_id = prev_row[0]

    cur.execute(f"UPDATE {table} SET client_id=? WHERE {pk}=?", (client_id, target_id))
    cur.execute(
        """INSERT INTO attribution_event
            (target_kind, target_id, client_id, previous_client_id, confidence, source, attributed_by, rationale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (target_kind, target_id, client_id, previous_client_id, confidence, source, attributed_by, rationale),
    )
    conn.commit()
    return {"target_kind": target_kind, "target_id": target_id, "client_id": client_id,
            "previous_client_id": previous_client_id, "rewritten": previous_client_id != client_id}
'''


CITE = r'''
def cite(conn, cited_kind: str, cited_id: int, source_kind: str, source_id,
          source_ts: str = None, quote: str = None,
          offset_start: int = None, offset_end: int = None,
          extracted_by: str = "worker:v0.1.0"):
    """Record provenance: link a fact to a source with optional quote/offsets."""
    cur = conn.cursor()
    cur.execute(
        """INSERT OR IGNORE INTO citation
            (cited_kind, cited_id, source_kind, source_id, source_ts, quote, offset_start, offset_end, extracted_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cited_kind, cited_id, source_kind, source_id, source_ts, quote, offset_start, offset_end, extracted_by),
    )
    conn.commit()
    return {"citation_id": cur.lastrowid, "cited_kind": cited_kind, "cited_id": cited_id,
            "source_kind": source_kind, "source_id": source_id}
'''


def install_scripts(conn):
    upsert_script(
        conn,
        "resolve_or_create_entity",
        "Resolve an alias to an entity within a client; create new entity+alias if not found. Refuses to mint nodes from short ambiguous tokens.",
        RESOLVE_OR_CREATE_ENTITY,
        applies_to="entity,entity_alias",
    )
    upsert_script_params(
        conn,
        "resolve_or_create_entity",
        [
            ("conn", "sqlite3.Connection", None, "SQLite connection"),
            ("client_id", "int", None, "Client this resolution is scoped to"),
            ("alias_text", "str", None, "The string to resolve"),
            ("entity_type", "str", "None", "Required if creating; ignored if alias resolves"),
            ("canonical_name", "str", "None", "If creating, the canonical name (defaults to alias_text)"),
            ("alias_kind", "str", "None", "name | initials | email | handle | nickname"),
            ("min_alias_confidence", "str", "'likely'", "Confidence to record on a new alias row"),
            ("resolved_by", "str", "'worker:v0.1.0'", "Who/what made the call"),
        ],
    )

    upsert_script(
        conn,
        "propose_predicate",
        "Register or increment a proposed predicate. No-op for already-approved predicates.",
        PROPOSE_PREDICATE,
        applies_to="predicate",
    )
    upsert_script_params(
        conn,
        "propose_predicate",
        [
            ("conn", "sqlite3.Connection", None, "SQLite connection"),
            ("name", "str", None, "Predicate name (snake_case)"),
            ("description", "str", "None", "What this predicate means"),
            ("subject_types", "List[str]", "None", "Valid entity types for subject"),
            ("object_types", "List[str]", "None", "Valid entity types for object (or ['literal'])"),
            ("inverse_name", "str", "None", "Name of the inverse predicate, if any"),
        ],
    )

    upsert_script(
        conn,
        "attribute_fact",
        "Attribute (or re-attribute) a fact to a client. Updates target row and writes append-only attribution_event.",
        ATTRIBUTE_FACT,
        applies_to="z_memory,source_meeting,source_document,edge,entity",
    )
    upsert_script_params(
        conn,
        "attribute_fact",
        [
            ("conn", "sqlite3.Connection", None, "SQLite connection"),
            ("target_kind", "str", None, "memory | source_meeting | source_document | edge | entity"),
            ("target_id", "int", None, "Primary key of the target row"),
            ("client_id", "Optional[int]", None, "Client to attribute to (None = un-attribute)"),
            ("source", "str", None, "box_folder | attendee_domain | platform_tag | content_inference | manual"),
            ("confidence", "str", "'guessed'", "certain | likely | guessed"),
            ("attributed_by", "str", "'worker:v0.1.0'", "Who/what made the call"),
            ("rationale", "str", "None", "Optional explanation, especially for re-attribution"),
        ],
    )

    upsert_script(
        conn,
        "cite",
        "Link a fact (memory/edge/entity) to a source (meeting/document/manual) with optional quote.",
        CITE,
        applies_to="citation",
    )
    upsert_script_params(
        conn,
        "cite",
        [
            ("conn", "sqlite3.Connection", None, "SQLite connection"),
            ("cited_kind", "str", None, "memory | edge | entity"),
            ("cited_id", "int", None, "Primary key within cited_kind"),
            ("source_kind", "str", None, "meeting | document | manual"),
            ("source_id", "Optional[int]", None, "FK to source_meeting / source_document (NULL for manual)"),
            ("source_ts", "str", "None", "ISO timestamp of the source moment"),
            ("quote", "str", "None", "Verbatim snippet from the source"),
            ("offset_start", "int", "None", "Char offset where quote begins in source content"),
            ("offset_end", "int", "None", "Char offset where quote ends"),
            ("extracted_by", "str", "'worker:v0.1.0'", "worker version or human user"),
        ],
    )


# =============================================================================
# TESTS (registered in z_script_test)
# =============================================================================

def install_tests(conn):
    import json as _json
    setup = (
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_client', 'Test Client');"
    )
    teardown = (
        "DELETE FROM citation WHERE cited_kind='entity';"
        "DELETE FROM attribution_event WHERE target_kind='entity';"
        "DELETE FROM entity_alias WHERE client_id=9001;"
        "DELETE FROM entity WHERE client_id=9001;"
        "DELETE FROM client WHERE client_id=9001;"
        "DELETE FROM predicate WHERE name LIKE 'test_%';"
    )
    tests = [
        (
            "resolve_or_create_entity",
            "test_short_token_refused",
            "Short ambiguous tokens are not auto-minted into entities",
            _json.dumps({"args": [9001, "BM"], "kwargs": {"entity_type": "person"}}),
            _json.dumps({"resolved": False, "reason": "ambiguous_short_token"}),
            None,
            setup,
            teardown,
        ),
        (
            "resolve_or_create_entity",
            "test_creates_new_entity",
            "Long alias with type creates new entity + alias",
            _json.dumps({"args": [9001, "Barry Marsh"], "kwargs": {"entity_type": "person"}}),
            _json.dumps({"resolved": True, "created": True, "type": "person"}),
            None,
            setup,
            teardown,
        ),
        (
            "propose_predicate",
            "test_first_proposal",
            "First-time proposal lands as status=proposed with count=1",
            _json.dumps({"args": ["test_tension_with"]}),
            _json.dumps({"status": "proposed", "proposed_count": 1, "created": True}),
            None,
            None,
            "DELETE FROM predicate WHERE name='test_tension_with'",
        ),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO z_script_test (script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql) VALUES (?,?,?,?,?,?,?,?)",
        tests,
    )


# =============================================================================
# DRIVER
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        default=os.path.join(os.path.dirname(__file__), "agent_memory.db"),
        help="Path to existing agent_memory.db",
    )
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} does not exist. Run create_agent_memory_db.py first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.path)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Extending {args.path}...")

    create_schema(conn)
    print("  Tables + indexes + triggers created (idempotent)")

    upsert_schema(conn, SCHEMA_DOCS)
    print("  z_schema documentation upserted")

    upsert_glossary(conn, GLOSSARY)
    print("  z_glossary terms upserted")

    install_scripts(conn)
    print("  Scripts registered in z_script_catalog")

    install_tests(conn)
    print("  Tests registered in z_script_test")

    conn.execute(
        "INSERT INTO z_version (version, description) VALUES ('v0.1.0', 'Add multi-tenant client knowledge schema: client, entity, entity_alias, predicate, edge, source_meeting, source_document, citation, attribution_event; z_memory.client_id')"
    )
    conn.commit()
    conn.close()
    print("\nDone. Version bumped to v0.1.0.")


if __name__ == "__main__":
    main()
