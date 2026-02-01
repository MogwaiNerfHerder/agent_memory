# Agent Memory

A portable, self-documenting, self-testing semantic memory system for AI agents.

**No API keys. Runs 100% local. 44KB.**

## Quick Start

```bash
pip install sentence-transformers numpy
```

```python
from agent_memory import AgentMemory

memory = AgentMemory()

# Store with auto-importance
memory.smart_remember("User is CEO of Acme Corp", "fact", "user")
# → importance: 9 (detected: executive_role)

# Semantic recall
results = memory.recall("who is the user")
# → finds "CEO of Acme Corp" even though query didn't mention "CEO"

# Get context for LLM injection
context = memory.context("current topic")
# → "## Relevant Memories\n- [fact] User is CEO..."
```

## Features

### Semantic Memory
- Vector embeddings via `sentence-transformers` (all-MiniLM-L6-v2)
- Cosine similarity search
- No cloud dependencies

### Auto-Importance
Heuristics detect significance:
- Executive roles → +2
- "Important", "critical" → +2
- Security/credentials → +3
- Names, dates, money → +1
- Small talk → -2

### Memory Lifecycle
```
remember() → ACTIVE → decay() → INACTIVE → purge() → GONE
                ↑                    ↓
            reinforce()          recall(include_inactive=True)
```

### Self-Documenting
The database teaches agents how to use it:
- `z_schema` - table/column documentation
- `z_glossary` - term definitions
- `z_prompt_catalog` - action patterns (remember, recall, forget)
- `z_script_catalog` - reusable functions with signatures
- `z_script_test` - TDD test cases

## API

### Store
```python
# Explicit importance
memory.remember(content, type, source, importance=5)

# Auto-assessed importance (rejects low-value content)
memory.smart_remember(content, type, source, min_importance=3)
```

### Recall
```python
# Semantic search
results = memory.recall(query, top_k=5)
# → [(similarity, id, content, type, importance), ...]

# Formatted for LLM context
context = memory.context(query)
# → Markdown string

# Search archived memories
results = memory.recall(query, include_inactive=True)
```

### Manage
```python
memory.reinforce(memory_id, boost=1)      # Increase importance
memory.forget(memory_id)                   # Soft delete (recoverable)
memory.forget(memory_id, hard_delete=True) # Permanent
```

### Lifecycle
```python
# Preview what would decay
memory.decay(days_old=30, dry_run=True)

# Actually decay old, low-importance, rarely-accessed memories
memory.decay(days_old=30, dry_run=False)

# Permanently delete long-inactive memories
memory.purge(days_inactive=90, dry_run=False)
```

### Introspect
```python
memory.stats()          # {total, active, types, avg_importance, avg_access}
memory.list_memories()  # List all memories
memory.bootstrap()      # Get system bootstrap prompt
memory.version()        # Database version
memory.help()           # Show all operations
```

## Memory Types

| Type | Use For |
|------|---------|
| `fact` | Objective information (names, roles, relationships) |
| `preference` | User likes/dislikes, communication style |
| `lesson` | Learned behaviors, best practices |
| `decision` | Choices made, rationale |
| `insight` | Observations, patterns |
| `error` | Mistakes to avoid, fixes applied |
| `security` | Credentials, sensitive info (auto high importance) |

## Database Schema

```
agent_memory.db
│
│  MEMORY SYSTEM
├── z_memory          # Vector memory store
├── z_schema          # Self-documentation
├── z_glossary        # Term definitions
├── z_prompt_catalog  # LLM action patterns
├── z_script_catalog  # Reusable scripts
├── z_script_params   # Function signatures
├── z_script_test     # TDD test cases
├── z_version         # Version history
│
│  DOMAIN TABLES (examples)
├── user              # People the agent interacts with
├── conversation      # Interaction sessions
├── message           # Individual exchanges
├── document          # Reference materials
└── task              # Tracked to-dos
```

## Linking Memories to Domain

Memories can reference domain entities via `source` and `source_id`:

```python
# Memory about a specific user
memory.remember(
    "John prefers bullet points over paragraphs",
    "preference",
    source="user",
    source_id="123"  # John's user_id
)

# Memory from a conversation
memory.remember(
    "Decided to use PostgreSQL for the backend",
    "decision",
    source="conversation",
    source_id="456"
)

# Later: "What do we know about John?"
results = memory.recall("John preferences")
```

This creates a knowledge graph: memories ↔ domain entities.

## Files

| File | Purpose |
|------|---------|
| `agent_memory.py` | Python interface |
| `agent_memory.db` | SQLite database (the brain) |
| `create_agent_memory_db.py` | Script to recreate fresh database |

## Recreate Database

```bash
python create_agent_memory_db.py
```

## Run Tests

```python
from agent_memory import AgentMemory
import sqlite3

memory = AgentMemory()
cursor = memory.conn.cursor()

# Load test runner
cursor.execute("SELECT script_body FROM z_script_catalog WHERE script_name='run_script_tests'")
exec(cursor.fetchone()[0])

# Run all tests
run_script_tests(memory.conn)
```

## Philosophy

1. **Self-documenting**: The database teaches agents how to use it
2. **Self-testing**: TDD built into the schema
3. **Local-first**: No API keys, no cloud, no dependencies beyond Python
4. **Semantic**: Find by meaning, not just keywords
5. **Lifecycle-aware**: Memories fade, important ones persist

## License

MIT
