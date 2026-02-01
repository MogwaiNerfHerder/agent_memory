"""
Create agent_memory.db - portable agent memory system.

A self-documenting, self-testing semantic memory database for AI agents.
Run this script to create a fresh database.

Usage:
    python create_agent_memory_db.py
    python create_agent_memory_db.py --path /custom/path/agent_memory.db
"""

import sqlite3
import os
import sys

# Default path is same directory as script
db_path = os.path.join(os.path.dirname(__file__), 'agent_memory.db')

# Allow custom path via command line
if len(sys.argv) > 1:
    if sys.argv[1] == '--path' and len(sys.argv) > 2:
        db_path = sys.argv[2]
    else:
        db_path = sys.argv[1]

# Remove existing
if os.path.exists(db_path):
    os.remove(db_path)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print(f"Creating {db_path}...")

# =============================================================================
# SYSTEM TABLES
# =============================================================================

# z_version - version tracking
cursor.execute('''
CREATE TABLE z_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# z_schema - self-documentation
cursor.execute('''
CREATE TABLE z_schema (
    object_type TEXT NOT NULL,
    object_name TEXT NOT NULL,
    column_name TEXT,
    description TEXT NOT NULL,
    UNIQUE (object_type, object_name, column_name)
)
''')

cursor.execute('''
CREATE TRIGGER trg_z_schema_reject_invalid_object_type
BEFORE INSERT ON z_schema
WHEN NEW.object_type NOT IN ('table', 'view', 'column')
BEGIN
  SELECT RAISE(FAIL, 'Invalid object_type: must be one of table, view, or column.');
END
''')

cursor.execute('''
CREATE TRIGGER trg_z_schema_column_null_for_objects
BEFORE INSERT ON z_schema
WHEN NEW.object_type IN ('table', 'view') AND NEW.column_name IS NOT NULL
BEGIN
  SELECT RAISE(FAIL, 'Invalid column_name: must be NULL for object_type table or view.');
END
''')

# z_glossary - term definitions
cursor.execute('''
CREATE TABLE z_glossary (
    term TEXT PRIMARY KEY,
    definition TEXT,
    example_usage TEXT
)
''')

# z_prompt_catalog - reusable prompts
cursor.execute('''
CREATE TABLE z_prompt_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_nickname TEXT,
    question_pattern TEXT NOT NULL,
    prompt_template TEXT NOT NULL,
    object_type TEXT,
    object_name TEXT,
    field_names TEXT,
    target_audience TEXT,
    example_response TEXT,
    is_active BOOLEAN DEFAULT 1,
    version TEXT DEFAULT 'v1.0',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
)
''')

# z_script_catalog - reusable scripts
cursor.execute('''
CREATE TABLE z_script_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_name TEXT NOT NULL UNIQUE,
    description TEXT,
    language TEXT DEFAULT 'python',
    script_body TEXT NOT NULL,
    applies_to TEXT,
    version_target TEXT DEFAULT 'v1.0.0',
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
)
''')

# z_script_params - script parameter signatures
cursor.execute('''
CREATE TABLE z_script_params (
    script_name TEXT NOT NULL REFERENCES z_script_catalog(script_name) ON DELETE CASCADE,
    method_name TEXT,
    param_name TEXT NOT NULL,
    param_type TEXT,
    default_value TEXT,
    description TEXT,
    ordinal INTEGER DEFAULT 0,
    UNIQUE(script_name, method_name, param_name)
)
''')
cursor.execute('CREATE INDEX idx_script_params_script ON z_script_params(script_name)')

# z_script_test - TDD test cases
cursor.execute('''
CREATE TABLE z_script_test (
    test_id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_name TEXT NOT NULL REFERENCES z_script_catalog(script_name) ON DELETE CASCADE,
    test_name TEXT NOT NULL,
    description TEXT,
    test_input TEXT,
    expected_output TEXT,
    expected_error TEXT,
    setup_sql TEXT,
    teardown_sql TEXT,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(script_name, test_name)
)
''')
cursor.execute('CREATE INDEX idx_script_test_script ON z_script_test(script_name)')

# z_memory - vector memory store
cursor.execute('''
CREATE TABLE z_memory (
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    summary TEXT,
    memory_type TEXT NOT NULL,
    source TEXT,
    source_id TEXT,
    tags TEXT,
    importance INTEGER DEFAULT 5,
    embedding BLOB,
    embedding_model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    accessed_at TIMESTAMP,
    access_count INTEGER DEFAULT 0,
    expires_at TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
)
''')
cursor.execute('CREATE INDEX idx_memory_type ON z_memory(memory_type)')
cursor.execute('CREATE INDEX idx_memory_active ON z_memory(is_active, importance DESC)')

print("  System tables created")

# =============================================================================
# DOMAIN TABLES (generic examples)
# =============================================================================

# user - who the agent interacts with
cursor.execute('''
CREATE TABLE user (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    role TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
)
''')

# conversation - interaction sessions
cursor.execute('''
CREATE TABLE conversation (
    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES user(user_id),
    title TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP
)
''')

# message - individual exchanges
cursor.execute('''
CREATE TABLE message (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversation(conversation_id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# document - reference materials
cursor.execute('''
CREATE TABLE document (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    doc_type TEXT,
    source_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
)
''')

# task - things to track
cursor.execute('''
CREATE TABLE task (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES user(user_id),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    due_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    completed_at TIMESTAMP
)
''')

print("  Domain tables created")

# =============================================================================
# TIMESTAMP TRIGGERS
# =============================================================================

# Insert triggers for all domain tables
for table, created_col in [('user', 'created_at'), ('conversation', 'started_at'),
                            ('message', 'created_at'), ('document', 'created_at'),
                            ('task', 'created_at')]:
    cursor.execute(f'''
        CREATE TRIGGER trg_{table}_insert
        AFTER INSERT ON {table}
        FOR EACH ROW
        WHEN NEW.{created_col} IS NULL
        BEGIN
            UPDATE {table} SET {created_col} = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid;
        END
    ''')

# Update triggers for tables with updated_at
for table, pk_col in [('user', 'user_id'), ('document', 'document_id'), ('task', 'task_id')]:
    cursor.execute(f'''
        CREATE TRIGGER trg_{table}_update
        AFTER UPDATE ON {table}
        FOR EACH ROW
        BEGIN
            UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE {pk_col} = NEW.{pk_col};
        END
    ''')

print("  Timestamp triggers created")

# =============================================================================
# SCHEMA DOCUMENTATION
# =============================================================================

schema_docs = [
    # z_version
    ('table', 'z_version', None, 'Database version history'),
    ('column', 'z_version', 'version', 'Semantic version (vMAJOR.MINOR.PATCH)'),
    ('column', 'z_version', 'description', 'What changed in this version'),
    ('column', 'z_version', 'created_at', 'When version was recorded'),

    # z_schema
    ('table', 'z_schema', None, 'Self-documentation for all database objects'),
    ('column', 'z_schema', 'object_type', 'Type: table, view, or column'),
    ('column', 'z_schema', 'object_name', 'Name of table/view'),
    ('column', 'z_schema', 'column_name', 'Column name (NULL for table/view level docs)'),
    ('column', 'z_schema', 'description', 'Human-readable description'),

    # z_glossary
    ('table', 'z_glossary', None, 'Domain term definitions'),
    ('column', 'z_glossary', 'term', 'The term being defined'),
    ('column', 'z_glossary', 'definition', 'What the term means'),
    ('column', 'z_glossary', 'example_usage', 'Example of term in context'),

    # z_prompt_catalog
    ('table', 'z_prompt_catalog', None, 'Reusable prompt templates for LLM agents'),
    ('column', 'z_prompt_catalog', 'prompt_nickname', 'Short identifier for the prompt'),
    ('column', 'z_prompt_catalog', 'question_pattern', 'Trigger phrases that invoke this prompt'),
    ('column', 'z_prompt_catalog', 'prompt_template', 'The full prompt template with placeholders'),
    ('column', 'z_prompt_catalog', 'target_audience', 'Who uses this prompt (LLM, human, etc.)'),

    # z_script_catalog
    ('table', 'z_script_catalog', None, 'Reusable executable scripts'),
    ('column', 'z_script_catalog', 'script_name', 'Unique script identifier'),
    ('column', 'z_script_catalog', 'description', 'What the script does'),
    ('column', 'z_script_catalog', 'language', 'Programming language (python, sql)'),
    ('column', 'z_script_catalog', 'script_body', 'The executable code'),
    ('column', 'z_script_catalog', 'applies_to', 'Table/object this script operates on'),

    # z_script_params
    ('table', 'z_script_params', None, 'Parameter signatures for scripts'),
    ('column', 'z_script_params', 'script_name', 'FK to z_script_catalog.script_name'),
    ('column', 'z_script_params', 'method_name', 'Method name (NULL for standalone functions)'),
    ('column', 'z_script_params', 'param_name', 'Parameter name'),
    ('column', 'z_script_params', 'param_type', 'Type hint'),
    ('column', 'z_script_params', 'default_value', 'Default value (NULL if required)'),
    ('column', 'z_script_params', 'ordinal', 'Parameter position (1-based)'),

    # z_script_test
    ('table', 'z_script_test', None, 'TDD test cases for scripts'),
    ('column', 'z_script_test', 'script_name', 'FK to z_script_catalog.script_name'),
    ('column', 'z_script_test', 'test_name', 'Unique test identifier within script'),
    ('column', 'z_script_test', 'test_input', 'JSON: {"args": [...], "kwargs": {...}}'),
    ('column', 'z_script_test', 'expected_output', 'JSON: expected return value'),
    ('column', 'z_script_test', 'expected_error', 'Exception class if testing error path'),

    # z_memory
    ('table', 'z_memory', None, 'Universal vector memory store for agent recall'),
    ('column', 'z_memory', 'memory_id', 'Primary key'),
    ('column', 'z_memory', 'content', 'The actual memory text'),
    ('column', 'z_memory', 'summary', 'Brief summary of content'),
    ('column', 'z_memory', 'memory_type', 'Category: fact, preference, lesson, insight, decision, error'),
    ('column', 'z_memory', 'source', 'Origin: user, agent, file, api'),
    ('column', 'z_memory', 'source_id', 'Optional FK to source record'),
    ('column', 'z_memory', 'tags', 'Comma-separated or JSON tags'),
    ('column', 'z_memory', 'importance', 'Priority 1-10 (higher = resists decay)'),
    ('column', 'z_memory', 'embedding', 'Vector as BLOB (numpy float32)'),
    ('column', 'z_memory', 'embedding_model', 'Model used for embedding'),
    ('column', 'z_memory', 'access_count', 'Retrieval count (reinforcement signal)'),
    ('column', 'z_memory', 'expires_at', 'Optional TTL'),
    ('column', 'z_memory', 'is_active', 'Soft delete flag'),

    # user
    ('table', 'user', None, 'People the agent interacts with'),
    ('column', 'user', 'user_id', 'Primary key'),
    ('column', 'user', 'name', 'Display name'),
    ('column', 'user', 'email', 'Contact email'),
    ('column', 'user', 'role', 'Role or title'),
    ('column', 'user', 'created_at', 'When user was added'),
    ('column', 'user', 'updated_at', 'Last modification (auto-set)'),

    # conversation
    ('table', 'conversation', None, 'Interaction sessions with users'),
    ('column', 'conversation', 'conversation_id', 'Primary key'),
    ('column', 'conversation', 'user_id', 'FK to user'),
    ('column', 'conversation', 'title', 'Conversation topic/summary'),
    ('column', 'conversation', 'started_at', 'When conversation began'),
    ('column', 'conversation', 'ended_at', 'When conversation ended'),

    # message
    ('table', 'message', None, 'Individual exchanges within conversations'),
    ('column', 'message', 'message_id', 'Primary key'),
    ('column', 'message', 'conversation_id', 'FK to conversation'),
    ('column', 'message', 'role', 'Speaker: user, assistant, system'),
    ('column', 'message', 'content', 'Message text'),
    ('column', 'message', 'created_at', 'When message was sent'),

    # document
    ('table', 'document', None, 'Reference materials and notes'),
    ('column', 'document', 'document_id', 'Primary key'),
    ('column', 'document', 'title', 'Document title'),
    ('column', 'document', 'content', 'Document content or summary'),
    ('column', 'document', 'doc_type', 'Type: note, file, url, snippet'),
    ('column', 'document', 'source_path', 'Original file path or URL'),
    ('column', 'document', 'created_at', 'When document was added'),
    ('column', 'document', 'updated_at', 'Last modification (auto-set)'),

    # task
    ('table', 'task', None, 'Tracked tasks and to-dos'),
    ('column', 'task', 'task_id', 'Primary key'),
    ('column', 'task', 'user_id', 'FK to user (owner)'),
    ('column', 'task', 'title', 'Task title'),
    ('column', 'task', 'description', 'Task details'),
    ('column', 'task', 'status', 'Status: pending, in_progress, done'),
    ('column', 'task', 'priority', 'Priority 1-10'),
    ('column', 'task', 'due_date', 'Due date (ISO format)'),
    ('column', 'task', 'created_at', 'When task was created'),
    ('column', 'task', 'updated_at', 'Last modification (auto-set)'),
    ('column', 'task', 'completed_at', 'When task was completed'),
]

cursor.executemany('INSERT INTO z_schema VALUES (?,?,?,?)', schema_docs)
print("  Schema documented")

# =============================================================================
# GLOSSARY
# =============================================================================

glossary = [
    ('memory', 'A stored piece of information with semantic embedding for recall', 'Remember that the user prefers concise responses'),
    ('importance', 'Priority score 1-10 determining decay resistance and retrieval order', 'Security facts get importance 9-10'),
    ('decay', 'Process of soft-deleting old, low-importance, rarely-accessed memories', 'Run decay_memories weekly to clean up noise'),
    ('recall', 'Semantic search to find relevant memories', 'Recall what the user said about formatting'),
    ('reinforce', 'Boost importance of a memory to prevent decay', 'Reinforce memories the user confirms as correct'),
    ('embedding', 'Vector representation of text for semantic similarity', 'Uses sentence-transformers all-MiniLM-L6-v2'),
    ('bootstrap', 'Initial prompt that teaches agents how to use the database', 'Read the bootstrap prompt first'),
]

cursor.executemany('INSERT INTO z_glossary VALUES (?,?,?)', glossary)
print("  Glossary populated")

# =============================================================================
# SCRIPTS
# =============================================================================

bump_version_script = '''
def bump_version(conn, level: str, notes: str = ""):
    import re

    assert level in {"MAJOR", "MINOR", "PATCH"}, "Invalid bump level"

    cur = conn.cursor()
    cur.execute("SELECT version FROM z_version")
    versions = [row[0] for row in cur.fetchall() if row[0]]

    def parse(v):
        m = re.match(r"^v(\\d+)\\.(\\d+)\\.(\\d+)$", v)
        return tuple(map(int, m.groups())) if m else None

    parsed_versions = [parse(v) for v in versions if parse(v)]
    latest = max(parsed_versions, default=(0, 0, 0))

    if level == "MAJOR":
        new_version = (latest[0] + 1, 0, 0)
    elif level == "MINOR":
        new_version = (latest[0], latest[1] + 1, 0)
    else:
        new_version = (latest[0], latest[1], latest[2] + 1)

    version = f"v{new_version[0]}.{new_version[1]}.{new_version[2]}"
    cur.execute("INSERT INTO z_version (version, description) VALUES (?, ?)", (version, notes))
    conn.commit()

    escaped_notes = notes.replace("'", "''")
    return f"INSERT INTO z_version (version, description) VALUES ('{version}', '{escaped_notes}');"
'''

smart_remember_script = r'''
def smart_remember_local(
    conn,
    content: str,
    memory_type: str,
    source: str = None,
    source_id: str = None,
    tags: str = None,
    summary: str = None,
    expires_at: str = None,
    min_importance: int = 3
):
    """Store a memory with heuristic-based importance assessment. No API needed."""
    import re
    import numpy as np
    from sentence_transformers import SentenceTransformer

    importance = 5
    signals = []
    content_lower = content.lower()

    # Boost signals
    if any(word in content_lower for word in ["ceo", "cto", "vp", "director", "president", "founder", "owner"]):
        importance += 2
        signals.append("executive_role")
    if any(word in content_lower for word in ["important", "critical", "remember this", "don't forget", "key", "crucial"]):
        importance += 2
        signals.append("explicit_importance")
    if any(word in content_lower for word in ["always", "never", "must", "require", "need"]):
        importance += 1
        signals.append("strong_preference")
    if any(word in content_lower for word in ["password", "credential", "secret", "api key", "token"]):
        importance += 3
        signals.append("security_sensitive")
    if any(word in content_lower for word in ["deadline", "due date", "urgent"]):
        importance += 1
        signals.append("time_sensitive")
    if any(word in content_lower for word in ["prefer", "like", "want", "hate", "dislike"]):
        importance += 1
        signals.append("preference")
    if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", content):
        importance += 1
        signals.append("named_entity")
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", content):
        importance += 1
        signals.append("specific_date")
    if re.search(r"\$[\d,]+", content):
        importance += 1
        signals.append("financial")

    # Reduce signals
    if len(content) < 20:
        importance -= 1
        signals.append("too_short")
    if any(word in content_lower for word in ["maybe", "perhaps", "might", "possibly"]):
        importance -= 1
        signals.append("uncertain")
    if any(word in content_lower for word in ["weather", "lunch", "coffee", "traffic"]):
        importance -= 2
        signals.append("small_talk")

    # Type boosts
    type_boosts = {"fact": 1, "preference": 1, "lesson": 2, "error": 1, "decision": 2, "security": 3}
    if memory_type in type_boosts:
        importance += type_boosts[memory_type]
        signals.append(f"type_boost_{memory_type}")

    importance = max(1, min(10, importance))
    rationale = f"Heuristic score based on: {', '.join(signals)}" if signals else "No special signals detected"

    if importance < min_importance:
        return {"stored": False, "importance": importance, "rationale": rationale, "signals": signals,
                "reason": f"Below min_importance threshold ({importance} < {min_importance})"}

    embedder = SentenceTransformer("all-MiniLM-L6-v2")
    embedding = embedder.encode(content).astype(np.float32)

    cur = conn.cursor()
    cur.execute("""INSERT INTO z_memory (content, summary, memory_type, source, source_id, importance, tags, embedding, embedding_model, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content, summary or rationale[:100], memory_type, source, source_id, importance, tags, embedding.tobytes(), "all-MiniLM-L6-v2", expires_at))
    memory_id = cur.lastrowid
    conn.commit()

    return {"stored": True, "memory_id": memory_id, "importance": importance, "rationale": rationale, "signals": signals}
'''

decay_script = r'''
def decay_memories(conn, days_old: int = 30, importance_threshold: int = 3, min_access: int = 3, dry_run: bool = True):
    """Soft-delete old, low-importance, rarely-accessed memories."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT memory_id, content, memory_type, importance, access_count, julianday('now') - julianday(created_at) as age_days
        FROM z_memory WHERE is_active = 1 AND importance <= ? AND access_count < ? AND created_at < datetime('now', '-' || ? || ' days')
        ORDER BY importance ASC, access_count ASC, created_at ASC
    """, (importance_threshold, min_access, days_old))
    candidates = cursor.fetchall()

    if not candidates:
        return {"decayed_count": 0, "decayed_ids": [], "criteria": {"days_old": days_old, "importance_threshold": importance_threshold, "min_access": min_access}, "dry_run": dry_run}

    candidate_ids = [row[0] for row in candidates]
    if not dry_run:
        cursor.executemany("UPDATE z_memory SET is_active = 0 WHERE memory_id = ?", [(mid,) for mid in candidate_ids])
        conn.commit()

    return {"decayed_count": len(candidates), "decayed_ids": candidate_ids,
            "candidates": [{"memory_id": row[0], "content": row[1][:50], "type": row[2], "importance": row[3], "access_count": row[4], "age_days": round(row[5], 1)} for row in candidates],
            "criteria": {"days_old": days_old, "importance_threshold": importance_threshold, "min_access": min_access}, "dry_run": dry_run}
'''

purge_script = r'''
def purge_memories(conn, days_inactive: int = 90, dry_run: bool = True):
    """Permanently delete memories that have been inactive for N days."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT memory_id, content, memory_type, created_at FROM z_memory
        WHERE is_active = 0 AND ((accessed_at IS NOT NULL AND accessed_at < datetime('now', '-' || ? || ' days'))
              OR (accessed_at IS NULL AND created_at < datetime('now', '-' || ? || ' days')))
    """, (days_inactive, days_inactive))
    candidates = cursor.fetchall()
    candidate_ids = [row[0] for row in candidates]

    if not dry_run and candidate_ids:
        cursor.executemany("DELETE FROM z_memory WHERE memory_id = ?", [(mid,) for mid in candidate_ids])
        conn.commit()

    return {"purged_count": len(candidates), "purged_ids": candidate_ids,
            "candidates": [{"memory_id": row[0], "content": row[1][:50], "type": row[2]} for row in candidates],
            "days_inactive": days_inactive, "dry_run": dry_run}
'''

test_runner_script = r'''
def run_script_tests(conn, script_name: str = None, verbose: bool = True):
    """Run tests from z_script_test for one or all scripts."""
    import json
    cursor = conn.cursor()

    if script_name:
        cursor.execute("SELECT test_id, script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql FROM z_script_test WHERE script_name = ? AND is_active = 1 ORDER BY test_id", (script_name,))
    else:
        cursor.execute("SELECT test_id, script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql FROM z_script_test WHERE is_active = 1 ORDER BY script_name, test_id")

    tests = cursor.fetchall()
    results = {"passed": 0, "failed": 0, "skipped": 0, "details": []}

    for test in tests:
        test_id, sname, tname, desc, tinput, expected, expected_err, setup, teardown = test
        result = {"script": sname, "test": tname, "status": None, "message": ""}

        try:
            if setup: conn.executescript(setup)
            cursor.execute("SELECT script_body FROM z_script_catalog WHERE script_name = ?", (sname,))
            row = cursor.fetchone()
            if not row:
                result["status"] = "skipped"; result["message"] = "Script not found"; results["skipped"] += 1; continue

            exec(row[0], globals())
            inputs = json.loads(tinput) if tinput else {"args": [], "kwargs": {}}
            func = globals().get(sname) or globals().get(sname.replace('_smart', ''))
            if not func:
                result["status"] = "skipped"; result["message"] = "Function not found"; results["skipped"] += 1; continue

            try:
                actual = func(conn, *inputs.get("args", []), **inputs.get("kwargs", {}))
                if expected_err:
                    result["status"] = "failed"; result["message"] = f"Expected {expected_err} but got result"; results["failed"] += 1
                elif expected:
                    expected_val = json.loads(expected)
                    matches = all(actual.get(k) == v for k, v in expected_val.items()) if isinstance(expected_val, dict) and isinstance(actual, dict) else actual == expected_val
                    if matches: result["status"] = "passed"; results["passed"] += 1
                    else: result["status"] = "failed"; result["message"] = f"Expected {expected_val}, got {actual}"; results["failed"] += 1
                else: result["status"] = "passed"; results["passed"] += 1
            except Exception as e:
                if expected_err and type(e).__name__ == expected_err: result["status"] = "passed"; results["passed"] += 1
                else: result["status"] = "failed"; result["message"] = f"{type(e).__name__}: {e}"; results["failed"] += 1
        finally:
            if teardown:
                try: conn.executescript(teardown)
                except: pass

        results["details"].append(result)
        if verbose:
            status = "PASS" if result["status"] == "passed" else "FAIL" if result["status"] == "failed" else "SKIP"
            print(f"{status} {sname}::{tname}")
            if result["message"]: print(f"     {result['message']}")

    if verbose: print(f"\\n{results['passed']} passed, {results['failed']} failed, {results['skipped']} skipped")
    return results
'''

create_table_script = '''
def create_domain_table(conn, table_name, columns, description=None):
    """
    Create a domain table following agent_memory conventions.

    Args:
        conn: SQLite connection
        table_name: Name of the table to create
        columns: List of (name, type, constraints) tuples
                 e.g., [("name", "TEXT", "NOT NULL"), ("user_id", "INTEGER", "REFERENCES user(id)")]
        description: Table description for z_schema

    Returns: {"created": True, "table": table_name, "columns": [...]}
    """
    cursor = conn.cursor()

    # Build column definitions
    col_defs = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for col in columns:
        name, dtype = col[0], col[1]
        constraints = col[2] if len(col) > 2 else ""
        col_defs.append(f"{name} {dtype} {constraints}".strip())

    # Add timestamp columns
    col_defs.append("created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    col_defs.append("updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")

    # Create table
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (\\n    " + ",\\n    ".join(col_defs) + "\\n)"
    cursor.execute(sql)

    # Create updated_at trigger
    trigger_sql = f"""
    CREATE TRIGGER IF NOT EXISTS trg_{table_name}_updated_at
    AFTER UPDATE ON {table_name}
    FOR EACH ROW
    BEGIN
        UPDATE {table_name} SET updated_at = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid;
    END
    """
    cursor.execute(trigger_sql)

    # Document in z_schema
    if description:
        cursor.execute("INSERT OR REPLACE INTO z_schema (object_type, object_name, column_name, description) VALUES (?, ?, NULL, ?)",
                      ("table", table_name, description))

    # Document columns
    for col in columns:
        col_desc = col[3] if len(col) > 3 else None
        if col_desc:
            cursor.execute("INSERT OR REPLACE INTO z_schema (object_type, object_name, column_name, description) VALUES (?, ?, ?, ?)",
                          ("table", table_name, col[0], col_desc))

    conn.commit()
    return {"created": True, "table": table_name, "columns": [c[0] for c in columns] + ["created_at", "updated_at"]}
'''

scripts = [
    ('bump_version_smart', 'Bump semantic version (MAJOR/MINOR/PATCH)', 'python', bump_version_script, 'z_version', 'v1.0.0', 1),
    ('smart_remember_local', 'Store memory with heuristic importance. No API needed.', 'python', smart_remember_script, 'z_memory', 'v1.0.0', 1),
    ('decay_memories', 'Soft-delete old, low-importance, rarely-accessed memories.', 'python', decay_script, 'z_memory', 'v1.0.0', 1),
    ('purge_memories', 'Permanently delete long-inactive memories.', 'python', purge_script, 'z_memory', 'v1.0.0', 1),
    ('run_script_tests', 'TDD test runner for z_script_test.', 'python', test_runner_script, 'z_script_test', 'v1.0.0', 1),
    ('create_domain_table', 'Create domain table with proper timestamps and triggers.', 'python', create_table_script, 'all', 'v1.0.0', 1),
]

cursor.executemany('INSERT INTO z_script_catalog (script_name, description, language, script_body, applies_to, version_target, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)', scripts)
print("  Scripts added")

# =============================================================================
# SCRIPT PARAMS
# =============================================================================

params = [
    ('bump_version_smart', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('bump_version_smart', None, 'level', 'str', None, 'MAJOR, MINOR, or PATCH', 2),
    ('bump_version_smart', None, 'notes', 'str', '""', 'Version description', 3),
    ('smart_remember_local', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('smart_remember_local', None, 'content', 'str', None, 'The memory content', 2),
    ('smart_remember_local', None, 'memory_type', 'str', None, 'Category: fact, preference, lesson, etc.', 3),
    ('smart_remember_local', None, 'source', 'str', 'None', 'Origin: user, agent, file', 4),
    ('smart_remember_local', None, 'source_id', 'str', 'None', 'Optional FK to source', 5),
    ('smart_remember_local', None, 'tags', 'str', 'None', 'Comma-separated tags', 6),
    ('smart_remember_local', None, 'min_importance', 'int', '3', 'Reject below this', 7),
    ('decay_memories', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('decay_memories', None, 'days_old', 'int', '30', 'Minimum age to decay', 2),
    ('decay_memories', None, 'importance_threshold', 'int', '3', 'Max importance to decay', 3),
    ('decay_memories', None, 'min_access', 'int', '3', 'Protect if accessed >= this', 4),
    ('decay_memories', None, 'dry_run', 'bool', 'True', 'Preview without changes', 5),
    ('purge_memories', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('purge_memories', None, 'days_inactive', 'int', '90', 'Days since deactivation', 2),
    ('purge_memories', None, 'dry_run', 'bool', 'True', 'Preview without changes', 3),
    ('run_script_tests', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('run_script_tests', None, 'script_name', 'Optional[str]', 'None', 'Test specific script or all', 2),
    ('run_script_tests', None, 'verbose', 'bool', 'True', 'Print output', 3),
    ('create_domain_table', None, 'conn', 'sqlite3.Connection', None, 'SQLite connection', 1),
    ('create_domain_table', None, 'table_name', 'str', None, 'Name of table to create', 2),
    ('create_domain_table', None, 'columns', 'List[Tuple]', None, 'List of (name, type, constraints, description)', 3),
    ('create_domain_table', None, 'description', 'str', 'None', 'Table description for z_schema', 4),
]

cursor.executemany('INSERT INTO z_script_params VALUES (?,?,?,?,?,?,?)', params)
print("  Script params documented")

# =============================================================================
# PROMPTS
# =============================================================================

bootstrap_prompt = '''This database is a self-documenting agent memory system.

### First Steps
1. Query z_schema to understand table structures
2. Query z_glossary for term definitions
3. Check z_prompt_catalog for action patterns
4. Check z_script_catalog for reusable functions

### Memory Operations
- **Remember**: Use smart_remember_local() - auto-assesses importance
- **Recall**: Search z_memory with semantic embeddings
- **Forget**: Soft-delete with is_active=0
- **Decay**: Run decay_memories() to clean old noise
- **Purge**: Run purge_memories() for permanent deletion

### Domain Tables
- user, conversation, message, document, task
- Link memories via source/source_id fields

### Scripts
Load from z_script_catalog:
```python
cursor.execute("SELECT script_body FROM z_script_catalog WHERE script_name = ?", (name,))
exec(cursor.fetchone()[0])
```'''

remember_prompt = '''When user says "remember this", "don't forget", "save this":
1. Identify what to remember
2. Classify memory_type: fact, preference, lesson, decision, insight, security
3. Store with smart_remember_local(conn, content, memory_type, source="user")
4. Confirm: what was stored, importance level, signals detected'''

recall_prompt = '''When user asks "what do you remember", "do you recall", "remember when":
1. Extract the search query
2. Determine parameters: memory_type filter, include_inactive, min_importance
3. Search z_memory using embedding similarity
4. Present results with content, type, importance'''

forget_prompt = '''When user says "forget that", "delete memory", "that's wrong":
1. Identify the memory (by id, content search, or most recent)
2. Confirm before deletion
3. Soft delete by default (is_active=0)
4. Hard delete only if explicitly requested'''

reinforce_prompt = '''When user says "that's important", "remember that well", "boost":
1. Identify the memory
2. Determine boost: +1 default, +2 very important, +3 critical
3. UPDATE z_memory SET importance = MIN(10, importance + boost)
4. Confirm new importance level'''

importance_prompt = '''Assess memory importance 1-10:
- 10: Critical (identity, security)
- 8-9: Core preferences, key facts
- 6-7: Useful context, patterns
- 5: General (default)
- 3-4: Minor details
- 1-2: Trivial, noise

Boost: "remember this", names/dates/money, corrections
Reduce: hypothetical, duplicate, small talk'''

link_prompt = '''Link memories to domain entities via source/source_id:
- source="user", source_id=user_id → memory about a person
- source="conversation", source_id=conversation_id → from a discussion
- source="document", source_id=document_id → from a reference
- source="task", source_id=task_id → related to a task'''

create_table_prompt = '''When creating new domain tables, follow these conventions:

1. REQUIRED COLUMNS:
   - created_at DATETIME DEFAULT CURRENT_TIMESTAMP
   - updated_at DATETIME DEFAULT CURRENT_TIMESTAMP

2. REQUIRED TRIGGER:
   CREATE TRIGGER trg_{table}_updated_at
   AFTER UPDATE ON {table}
   FOR EACH ROW
   BEGIN
       UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid;
   END;

3. DOCUMENT IN z_schema:
   INSERT INTO z_schema (object_type, object_name, column_name, description)
   VALUES ('table', '{table}', NULL, 'Description of table purpose');
   -- Add row for each column

4. FOREIGN KEYS:
   - Use ON DELETE CASCADE for owned children
   - Use ON DELETE SET NULL for optional references
   - Name constraint: fk_{table}_{referenced_table}

Example:
CREATE TABLE project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_id INTEGER REFERENCES user(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);'''

prompts = [
    ('system_bootstrap', 'System Bootstrap', bootstrap_prompt, 'system', 'all', None, 'LLM', None, 1, 'v1.0'),
    ('handle_remember_command', 'remember this, save this', remember_prompt, 'agent_action', 'z_memory', 'content,memory_type,source', 'LLM', None, 1, 'v1.0'),
    ('handle_recall_command', 'what do you remember, recall', recall_prompt, 'agent_action', 'z_memory', 'query,top_k,memory_type', 'LLM', None, 1, 'v1.0'),
    ('handle_forget_command', 'forget that, delete memory', forget_prompt, 'agent_action', 'z_memory', 'memory_id,hard_delete', 'LLM', None, 1, 'v1.0'),
    ('handle_reinforce_command', "that's important, boost", reinforce_prompt, 'agent_action', 'z_memory', 'memory_id,boost', 'LLM', None, 1, 'v1.0'),
    ('assess_memory_importance', 'rate importance', importance_prompt, 'evaluation', 'z_memory', 'content,importance', 'LLM', None, 1, 'v1.0'),
    ('link_memory_to_domain', 'associate memory with entity', link_prompt, 'guidance', 'z_memory', 'source,source_id', 'LLM', None, 1, 'v1.0'),
    ('create_domain_table', 'create new table, add table', create_table_prompt, 'guidance', 'all', 'table_name,columns', 'LLM', None, 1, 'v1.0'),
]

cursor.executemany('INSERT INTO z_prompt_catalog (prompt_nickname, question_pattern, prompt_template, object_type, object_name, field_names, target_audience, example_response, is_active, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', prompts)
print("  Prompts added")

# =============================================================================
# SAMPLE TESTS
# =============================================================================

import json

tests = [
    ('smart_remember_local', 'test_executive_high_importance', 'Executive roles get high importance',
     json.dumps({"args": ["User is the CEO of Acme Corp", "fact", "user"]}),
     json.dumps({"stored": True}), None, None, "DELETE FROM z_memory WHERE content LIKE '%CEO of Acme%'"),
    ('smart_remember_local', 'test_small_talk_rejected', 'Small talk rejected at min_importance=5',
     json.dumps({"args": ["The weather is nice", "observation", "conversation"], "kwargs": {"min_importance": 5}}),
     json.dumps({"stored": False}), None, None, None),
    ('decay_memories', 'test_dry_run', 'Dry run returns without changes',
     json.dumps({"args": [30, 3, 3, True]}),
     json.dumps({"dry_run": True}), None, None, None),
]

cursor.executemany('INSERT INTO z_script_test (script_name, test_name, description, test_input, expected_output, expected_error, setup_sql, teardown_sql) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', tests)
print("  Tests added")

# =============================================================================
# INITIAL VERSION
# =============================================================================

cursor.execute("INSERT INTO z_version (version, description) VALUES ('v0.0.1', 'Initial release with generic example domain tables')")

conn.commit()
conn.close()

print(f"\nCreated {db_path}")
print("  - System tables: z_memory, z_schema, z_glossary, z_prompt_catalog, z_script_catalog, z_script_params, z_script_test, z_version")
print("  - Domain tables: user, conversation, message, document, task")
print("  - Timestamp triggers for auto-updated_at")
print("  - Self-documenting, self-testing, 100% local")
