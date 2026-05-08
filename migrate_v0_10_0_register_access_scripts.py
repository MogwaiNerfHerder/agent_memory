"""
migrate_v0_10_0_register_access_scripts.py - Register access primitives in z_script_catalog.

The base library's promise: anyone opening the database can discover what
access patterns exist by reading z_script_catalog. We had drifted — most
of our access logic lived inline in Python files. This migration moves the
core read/write primitives into the catalog with params + tests.

Registered (v0.10.0):

  Write primitives:
    write_memory_for_client     — z_memory row with sentence-transformers embedding
    upsert_edge_dedup           — INSERT or return existing edge (knowledge-web friendly)
    link_memory_entity          — memory_entity row with role
    cite_edge_meeting           — citation: edge -> meeting
    cite_memory_meeting         — citation: memory -> meeting
    cite_edge_external          — citation: edge -> external (cortado/box/sf/asana)
    supersede_edge              — mark edge superseded; new edge points back

  Read primitives:
    find_entity_by_alias        — alias-aware, case-insensitive entity lookup
    notes_for_entity            — z_memory rows linked to entity (filtered by sensitivity, type)
    edges_for_entity            — active edges where entity is subject or object
    pending_drifts              — drift_hypothesis rows awaiting review
    edges_with_discrepancies    — single-valued predicates with parallel actives

Idempotent.
"""

import argparse
import json
import os
import sqlite3
import sys


# ---------------------------------------------------------------------------
# Script bodies (Python source). Each defines a top-level function whose name
# matches the script_name registered in z_script_catalog. The base library's
# loader does `exec(body, globals())` and looks up the function by name.
# ---------------------------------------------------------------------------

SCRIPTS = {

# ---- Write primitives ------------------------------------------------------

"write_memory_for_client": (
    "Insert a z_memory row with sentence-transformers embedding. Returns memory_id.",
    "z_memory",
r'''
def write_memory_for_client(conn, client_id, content, memory_type, source="meeting",
                              source_id=None, importance=5, tags=None, summary=None,
                              sensitivity="routine"):
    import numpy as np
    from sentence_transformers import SentenceTransformer
    embedder = SentenceTransformer("all-MiniLM-L6-v2")
    embedding = embedder.encode(content).astype(np.float32).tobytes()
    cur = conn.execute(
        """INSERT INTO z_memory
            (content, summary, memory_type, source, source_id, importance, tags,
             embedding, embedding_model, client_id, sensitivity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'all-MiniLM-L6-v2', ?, ?)""",
        (content, summary, memory_type, source, source_id, importance, tags,
         embedding, client_id, sensitivity),
    )
    conn.commit()
    return {"memory_id": cur.lastrowid}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
        ("content", "str", None, "Note text (1-3 sentences)"),
        ("memory_type", "str", None, "decision|action_item|risk|observation|theme|recommendation|insight|lesson|open_question"),
        ("source", "str", "'meeting'", "Source kind label (meeting|document|api|user|agent)"),
        ("source_id", "Optional[str]", "None", "External id of the source (e.g. cortado meeting guid)"),
        ("importance", "int", "5", "1-10"),
        ("tags", "Optional[str]", "None", "Comma-separated tags"),
        ("summary", "Optional[str]", "None", "Brief one-line summary"),
        ("sensitivity", "str", "'routine'", "routine|sensitive|hr_grade"),
    ],
),

"upsert_edge_dedup": (
    "Insert an active edge; if one already exists with same (client, subject, predicate, object), return it.",
    "edge",
r'''
def upsert_edge_dedup(conn, client_id, subject_id, predicate_id, object_id=None,
                      object_literal=None, object_literal_type=None,
                      notes=None, justification=None,
                      confidence="stated", sensitivity="routine"):
    if object_id is not None:
        existing = conn.execute(
            "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_id=? AND status='active'",
            (client_id, subject_id, predicate_id, object_id),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT edge_id FROM edge WHERE client_id=? AND subject_id=? AND predicate_id=? AND object_literal=? AND status='active'",
            (client_id, subject_id, predicate_id, object_literal),
        ).fetchone()
    if existing:
        return {"edge_id": existing[0], "created": False}
    cur = conn.execute(
        """INSERT INTO edge (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
                              notes, justification, confidence, sensitivity, status,
                              first_observed_ts, last_corroborated_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (client_id, subject_id, predicate_id, object_id, object_literal, object_literal_type,
         notes, justification, confidence, sensitivity),
    )
    conn.commit()
    return {"edge_id": cur.lastrowid, "created": True}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
        ("subject_id", "int", None, "Subject entity_id"),
        ("predicate_id", "int", None, "FK to predicate"),
        ("object_id", "Optional[int]", "None", "Object entity_id (for entity-to-entity edges)"),
        ("object_literal", "Optional[str]", "None", "Literal value (for entity-to-literal edges)"),
        ("object_literal_type", "Optional[str]", "None", "string|date|datetime|year|quarter|currency|url|..."),
        ("notes", "Optional[str]", "None", "Free-text elaboration"),
        ("justification", "Optional[str]", "None", "Verbatim quote supporting this edge"),
        ("confidence", "str", "'stated'", "stated|implied|factual|pattern|confirmed"),
        ("sensitivity", "str", "'routine'", "routine|sensitive|hr_grade"),
    ],
),

"link_memory_entity": (
    "Link a z_memory row to an entity with a role. Idempotent.",
    "memory_entity",
r'''
def link_memory_entity(conn, memory_id, entity_id, role="mentioned"):
    conn.execute(
        "INSERT OR IGNORE INTO memory_entity (memory_id, entity_id, role) VALUES (?, ?, ?)",
        (memory_id, entity_id, role),
    )
    conn.commit()
    return {"memory_id": memory_id, "entity_id": entity_id, "role": role}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("memory_id", "int", None, "z_memory row id"),
        ("entity_id", "int", None, "entity row id"),
        ("role", "str", "'mentioned'", "subject|mentioned|attendee|about|blocker|champion"),
    ],
),

"cite_edge_meeting": (
    "Add a citation: this edge is supported by this meeting (with optional verbatim quote).",
    "citation",
r'''
def cite_edge_meeting(conn, edge_id, source_meeting_id, source_external_id, source_ts=None,
                       quote=None, extracted_by="extractor:multipass:v0.7.0"):
    existing = conn.execute(
        "SELECT citation_id FROM citation WHERE cited_kind='edge' AND cited_id=? AND source_kind='meeting' AND source_id=?",
        (edge_id, source_meeting_id),
    ).fetchone()
    if existing:
        return {"citation_id": existing[0], "created": False}
    cur = conn.execute(
        """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, source_ts, quote, extracted_by)
           VALUES ('edge', ?, 'meeting', ?, ?, ?, ?, ?)""",
        (edge_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by),
    )
    conn.commit()
    return {"citation_id": cur.lastrowid, "created": True}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("edge_id", "int", None, "Edge being cited"),
        ("source_meeting_id", "int", None, "FK to source_meeting"),
        ("source_external_id", "str", None, "Cortado meeting guid"),
        ("source_ts", "Optional[str]", "None", "ISO timestamp of the source moment"),
        ("quote", "Optional[str]", "None", "Verbatim transcript snippet"),
        ("extracted_by", "str", "'extractor:multipass:v0.7.0'", "Worker version or human user"),
    ],
),

"cite_memory_meeting": (
    "Add a citation: this z_memory row is supported by this meeting (with optional verbatim quote).",
    "citation",
r'''
def cite_memory_meeting(conn, memory_id, source_meeting_id, source_external_id, source_ts=None,
                         quote=None, extracted_by="extractor:multipass:v0.7.0"):
    existing = conn.execute(
        "SELECT citation_id FROM citation WHERE cited_kind='memory' AND cited_id=? AND source_kind='meeting' AND source_id=?",
        (memory_id, source_meeting_id),
    ).fetchone()
    if existing:
        return {"citation_id": existing[0], "created": False}
    cur = conn.execute(
        """INSERT INTO citation (cited_kind, cited_id, source_kind, source_id, source_external_id, source_ts, quote, extracted_by)
           VALUES ('memory', ?, 'meeting', ?, ?, ?, ?, ?)""",
        (memory_id, source_meeting_id, source_external_id, source_ts, quote, extracted_by),
    )
    conn.commit()
    return {"citation_id": cur.lastrowid, "created": True}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("memory_id", "int", None, "z_memory row being cited"),
        ("source_meeting_id", "int", None, "FK to source_meeting"),
        ("source_external_id", "str", None, "Cortado meeting guid"),
        ("source_ts", "Optional[str]", "None", "ISO timestamp"),
        ("quote", "Optional[str]", "None", "Verbatim transcript snippet"),
        ("extracted_by", "str", "'extractor:multipass:v0.7.0'", "Worker version or human user"),
    ],
),

"cite_edge_external": (
    "Add a citation linking an edge to an external system (cortado/salesforce/asana/slack/box) by external_id.",
    "citation",
r'''
def cite_edge_external(conn, edge_id, source_kind, source_external_id, quote=None,
                        extracted_by="seeder:v0.1.0"):
    existing = conn.execute(
        "SELECT citation_id FROM citation WHERE cited_kind='edge' AND cited_id=? AND source_kind=? AND source_external_id=?",
        (edge_id, source_kind, source_external_id),
    ).fetchone()
    if existing:
        return {"citation_id": existing[0], "created": False}
    cur = conn.execute(
        """INSERT INTO citation
            (cited_kind, cited_id, source_kind, source_id, source_external_id, quote, extracted_by)
           VALUES ('edge', ?, ?, NULL, ?, ?, ?)""",
        (edge_id, source_kind, source_external_id, quote, extracted_by),
    )
    conn.commit()
    return {"citation_id": cur.lastrowid, "created": True}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("edge_id", "int", None, "Edge being cited"),
        ("source_kind", "str", None, "cortado|salesforce|asana|slack|manual"),
        ("source_external_id", "str", None, "External system identifier (cortado guid, sfid, asana gid, slack ts)"),
        ("quote", "Optional[str]", "None", "Optional excerpt"),
        ("extracted_by", "str", "'seeder:v0.1.0'", "Worker version or human user"),
    ],
),

"supersede_edge": (
    "Mark an edge as superseded; new edge points back via supersedes_id.",
    "edge",
r'''
def supersede_edge(conn, old_edge_id, new_edge_id):
    conn.execute("UPDATE edge SET status='superseded' WHERE edge_id=?", (old_edge_id,))
    conn.execute("UPDATE edge SET supersedes_id=? WHERE edge_id=?", (old_edge_id, new_edge_id))
    conn.commit()
    return {"old_edge_id": old_edge_id, "new_edge_id": new_edge_id}
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("old_edge_id", "int", None, "Edge to mark superseded"),
        ("new_edge_id", "int", None, "Edge that replaces it"),
    ],
),

# ---- Read primitives -------------------------------------------------------

"find_entity_by_alias": (
    "Case-insensitive lookup by canonical_name OR any alias_text (substring match). Returns list of matches.",
    "entity",
r'''
def find_entity_by_alias(conn, client_id, query, types=None):
    sql = """
    SELECT DISTINCT e.entity_id, e.type, e.canonical_name
      FROM entity e
 LEFT JOIN entity_alias a ON a.entity_id=e.entity_id
     WHERE e.client_id=?
       AND (e.canonical_name LIKE ? COLLATE NOCASE OR a.alias_text LIKE ? COLLATE NOCASE)
    """
    params = [client_id, f"%{query}%", f"%{query}%"]
    if types:
        sql += " AND e.type IN (" + ",".join(["?"]*len(types)) + ")"
        params.extend(types)
    sql += " ORDER BY (CASE WHEN e.canonical_name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END), e.canonical_name"
    params.append(f"%{query}%")
    return [{"entity_id": r[0], "type": r[1], "canonical_name": r[2]} for r in conn.execute(sql, params).fetchall()]
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
        ("query", "str", None, "String to match against canonical_name or any alias_text"),
        ("types", "Optional[List[str]]", "None", "Restrict to entity types (person|company|project|event|product|document|topic|department|deal|fund)"),
    ],
),

"notes_for_entity": (
    "z_memory rows linked to an entity via memory_entity, filtered by role/type/sensitivity.",
    "z_memory",
r'''
def notes_for_entity(conn, client_id, entity_id, max_sensitivity="sensitive",
                      memory_type=None, role=None, limit=50):
    sens_rank = {"routine": 0, "sensitive": 1, "hr_grade": 2}
    cap = sens_rank.get(max_sensitivity, 1)
    valid = [s for s, r in sens_rank.items() if r <= cap]
    placeholders = ",".join(["?"] * len(valid))
    sql = f"""
    SELECT DISTINCT m.memory_id, m.memory_type, m.importance, m.sensitivity, m.content,
           m.created_at, me.role
      FROM memory_entity me
      JOIN z_memory m ON m.memory_id=me.memory_id
     WHERE me.entity_id=? AND m.client_id=? AND m.is_active=1 AND m.deleted_at IS NULL
       AND m.sensitivity IN ({placeholders})
    """
    params = [entity_id, client_id] + valid
    if memory_type:
        sql += " AND m.memory_type=?"; params.append(memory_type)
    if role:
        sql += " AND me.role=?"; params.append(role)
    sql += " ORDER BY m.importance DESC, m.created_at DESC LIMIT ?"
    params.append(limit)
    return [{"memory_id": r[0], "memory_type": r[1], "importance": r[2],
              "sensitivity": r[3], "content": r[4], "created_at": r[5], "role": r[6]}
             for r in conn.execute(sql, params).fetchall()]
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
        ("entity_id", "int", None, "Entity to fetch notes for"),
        ("max_sensitivity", "str", "'sensitive'", "Highest tier to include: routine|sensitive|hr_grade"),
        ("memory_type", "Optional[str]", "None", "Filter by note type (decision|action_item|risk|...)"),
        ("role", "Optional[str]", "None", "Filter by memory_entity.role (subject|mentioned|...)"),
        ("limit", "int", "50", "Max rows"),
    ],
),

"edges_for_entity": (
    "Active edges where the entity is subject OR object. Sensitivity-filtered.",
    "edge",
r'''
def edges_for_entity(conn, client_id, entity_id, max_sensitivity="sensitive"):
    sens_rank = {"routine": 0, "sensitive": 1, "hr_grade": 2}
    cap = sens_rank.get(max_sensitivity, 1)
    valid = [s for s, r in sens_rank.items() if r <= cap]
    placeholders = ",".join(["?"] * len(valid))
    sql = f"""
    SELECT e.edge_id, e.subject_id, p.name AS predicate, p.cardinality,
           e.object_id, e.object_literal, e.object_literal_type,
           e.confidence, e.sensitivity, e.notes, e.justification,
           subj.canonical_name AS subj_name, subj.type AS subj_type,
           obj.canonical_name AS obj_name, obj.type AS obj_type
      FROM edge e
      JOIN predicate p ON p.predicate_id=e.predicate_id
      JOIN entity subj ON subj.entity_id=e.subject_id
 LEFT JOIN entity obj ON obj.entity_id=e.object_id
     WHERE e.client_id=? AND e.status='active'
       AND (e.subject_id=? OR e.object_id=?)
       AND e.sensitivity IN ({placeholders})
     ORDER BY p.name, e.confidence
    """
    params = [client_id, entity_id, entity_id] + valid
    return [dict(zip(
        ("edge_id","subject_id","predicate","cardinality","object_id","object_literal",
         "object_literal_type","confidence","sensitivity","notes","justification",
         "subj_name","subj_type","obj_name","obj_type"),
        r)) for r in conn.execute(sql, params).fetchall()]
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
        ("entity_id", "int", None, "Entity to fetch edges for"),
        ("max_sensitivity", "str", "'sensitive'", "Highest sensitivity tier to include"),
    ],
),

"pending_drifts": (
    "drift_hypothesis rows awaiting human review (status='pending') for a client.",
    "drift_hypothesis",
r'''
def pending_drifts(conn, client_id):
    rows = conn.execute(
        """SELECT dh.drift_hypothesis_id, dh.observed_token, e.entity_id, e.canonical_name, e.type,
                  dh.rationale, dh.supporting_quote, dh.source_kind, dh.source_external_id,
                  dh.proposed_by, dh.created_at
             FROM drift_hypothesis dh JOIN entity e ON e.entity_id = dh.candidate_entity_id
            WHERE dh.client_id=? AND dh.status='pending'
         ORDER BY dh.created_at""",
        (client_id,),
    ).fetchall()
    return [dict(zip(
        ("drift_hypothesis_id","observed_token","candidate_entity_id","candidate_name",
         "candidate_type","rationale","supporting_quote","source_kind","source_external_id",
         "proposed_by","created_at"),
        r)) for r in rows]
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
    ],
),

"edges_with_discrepancies": (
    "Single-valued predicates with > 1 active edges per (subject, predicate) — possible discrepancies.",
    "edge",
r'''
def edges_with_discrepancies(conn, client_id):
    rows = conn.execute(
        """SELECT subj.canonical_name AS subj_name, p.name AS predicate, COUNT(*) AS n_edges
             FROM edge e
             JOIN predicate p ON p.predicate_id=e.predicate_id
             JOIN entity subj ON subj.entity_id=e.subject_id
            WHERE e.client_id=? AND e.status='active'
              AND p.cardinality='single_valued'
         GROUP BY e.subject_id, e.predicate_id
           HAVING COUNT(*) > 1
         ORDER BY n_edges DESC""",
        (client_id,),
    ).fetchall()
    return [{"subject": r[0], "predicate": r[1], "n_edges": r[2]} for r in rows]
''',
    [
        ("conn", "sqlite3.Connection", None, "SQLite connection"),
        ("client_id", "int", None, "Tenant id"),
    ],
),

}


# ---------------------------------------------------------------------------
# Tests for each script
# ---------------------------------------------------------------------------

TESTS = [
    # script_name, test_name, description, test_input (json), expected_output (json), expected_error, setup_sql, teardown_sql
    (
        "find_entity_by_alias", "test_resolve_existing",
        "Looking up an existing entity by canonical name returns it",
        json.dumps({"args": [9001, "Test Person"]}),
        None, None,
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v10', 'Test Client');"
        "INSERT OR IGNORE INTO entity (entity_id, client_id, type, canonical_name) VALUES (99001, 9001, 'person', 'Test Person');",
        "DELETE FROM entity WHERE entity_id=99001; DELETE FROM client WHERE client_id=9001;",
    ),
    (
        "find_entity_by_alias", "test_no_match_returns_empty",
        "Unknown query returns empty list",
        json.dumps({"args": [9001, "NoSuchEntity_zzzz"]}),
        json.dumps([]),
        None,
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v10', 'Test Client');",
        "DELETE FROM client WHERE client_id=9001;",
    ),
    (
        "edges_with_discrepancies", "test_runs",
        "Smoke test: function returns a list (may be empty)",
        json.dumps({"args": [9001]}),
        None, None,
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v10', 'Test Client');",
        "DELETE FROM client WHERE client_id=9001;",
    ),
    (
        "pending_drifts", "test_empty_for_clean_client",
        "Returns empty list for a client with no drift hypotheses",
        json.dumps({"args": [9001]}),
        json.dumps([]),
        None,
        "INSERT OR IGNORE INTO client (client_id, slug, name) VALUES (9001, 'test_v10', 'Test Client');",
        "DELETE FROM client WHERE client_id=9001;",
    ),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()

    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.10.0 (register access scripts)...")

    n_scripts = 0
    n_params = 0
    for name, (description, applies_to, body, params) in SCRIPTS.items():
        body = body.lstrip("\n")
        conn.execute("""
            INSERT INTO z_script_catalog
                (script_name, description, language, script_body, applies_to, version_target, is_active)
            VALUES (?, ?, 'python', ?, ?, 'v0.10.0', 1)
            ON CONFLICT(script_name) DO UPDATE SET
                description=excluded.description,
                script_body=excluded.script_body,
                applies_to=excluded.applies_to,
                version_target=excluded.version_target,
                updated_at=CURRENT_TIMESTAMP
        """, (name, description, body, applies_to))
        # rewrite params
        conn.execute("DELETE FROM z_script_params WHERE script_name=?", (name,))
        for idx, (pname, ptype, default, pdesc) in enumerate(params, start=1):
            conn.execute(
                """INSERT INTO z_script_params
                    (script_name, method_name, param_name, param_type, default_value, description, ordinal)
                   VALUES (?, NULL, ?, ?, ?, ?, ?)""",
                (name, pname, ptype, default, pdesc, idx),
            )
            n_params += 1
        n_scripts += 1
    print(f"  registered {n_scripts} scripts ({n_params} params)")

    # Tests
    n_tests = 0
    for sname, tname, tdesc, tinput, tout, terror, setup, teardown in TESTS:
        conn.execute("""
            INSERT OR IGNORE INTO z_script_test
              (script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (sname, tname, tdesc, tinput, tout, terror, setup, teardown))
        n_tests += 1
    print(f"  registered {n_tests} tests")

    conn.commit()

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.10.0"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.10.0', 'Register core read/write access primitives in z_script_catalog with params and tests; bots can now discover db access patterns from the catalog')"
        )
        print("  bumped to v0.10.0")
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
