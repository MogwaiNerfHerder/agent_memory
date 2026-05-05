"""
agent_memory.py - Portable Agent Memory System

Drop agent_memory.db + agent_memory.py into any project for instant semantic memory.

Usage:
    from agent_memory import AgentMemory

    memory = AgentMemory()           # Uses agent_memory.db in same directory
    memory.help()                    # Show available operations

    # Store
    memory.remember("User prefers bullet points", "preference")
    memory.smart_remember("CEO of Acme Corp", "fact")  # Auto-importance

    # Recall
    results = memory.recall("formatting preferences")
    context = memory.context("current topic")  # Formatted for LLM injection

    # Lifecycle
    memory.reinforce(memory_id, boost=2)
    memory.forget(memory_id)
    memory.decay(dry_run=True)
    memory.purge(dry_run=True)

Requirements:
    pip install sentence-transformers numpy
"""

import sqlite3
import numpy as np
from pathlib import Path
from typing import Optional, List, Tuple, Any


class AgentMemory:
    """Portable agent memory with semantic recall."""

    def __init__(self, db_path: str = None):
        """Initialize memory system.

        Args:
            db_path: Path to agent_memory.db. Defaults to same directory as this file.
        """
        if db_path is None:
            db_path = Path(__file__).parent / "agent_memory.db"
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._embedder = None
        self._migrate()

    @property
    def embedder(self):
        """Lazy-load embedding model."""
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        return self._embedder

    # =========================================================================
    # CORE MEMORY OPERATIONS
    # =========================================================================

    def remember(
        self,
        content: str,
        memory_type: str,
        source: str = None,
        importance: int = 5,
        tags: str = None,
        summary: str = None,
    ) -> int:
        """Store a memory with explicit importance.

        Args:
            content: The memory text
            memory_type: Category (fact, preference, lesson, decision, insight, error)
            source: Origin (user, agent, file, api)
            importance: Priority 1-10 (default 5)
            tags: Comma-separated tags
            summary: Brief summary

        Returns:
            memory_id of stored memory
        """
        embedding = self.embedder.encode(content).astype(np.float32)

        cursor = self.conn.execute("""
            INSERT INTO z_memory
            (content, summary, memory_type, source, importance, tags, embedding, embedding_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (content, summary, memory_type, source, importance, tags,
              embedding.tobytes(), "all-MiniLM-L6-v2"))

        self.conn.commit()
        return cursor.lastrowid

    def smart_remember(
        self,
        content: str,
        memory_type: str,
        source: str = None,
        min_importance: int = 3,
        **kwargs
    ) -> dict:
        """Store memory with auto-assessed importance.

        Uses heuristics to determine importance. Rejects low-value content.

        Returns:
            dict with stored, memory_id, importance, rationale, signals
        """
        script = self._get_script("smart_remember_local")
        exec(script, globals())
        return smart_remember_local(self.conn, content, memory_type, source, min_importance=min_importance, **kwargs)

    def recall(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = None,
        min_importance: int = 0,
        include_inactive: bool = False,
    ) -> List[Tuple[float, int, str, str, int]]:
        """Find similar memories using semantic search.

        Args:
            query: Search text
            top_k: Number of results
            memory_type: Filter by type
            min_importance: Minimum importance threshold
            include_inactive: Include decayed memories ("remember when...")

        Returns:
            List of (similarity, memory_id, content, type, importance)
        """
        query_vec = self.embedder.encode(query).astype(np.float32)

        sql = "SELECT memory_id, content, memory_type, importance, embedding FROM z_memory WHERE embedding IS NOT NULL AND deleted_at IS NULL"
        params = []

        if not include_inactive:
            sql += " AND is_active=1"
        if memory_type:
            sql += " AND memory_type=?"
            params.append(memory_type)
        if min_importance > 0:
            sql += " AND importance>=?"
            params.append(min_importance)

        rows = self.conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            stored_vec = np.frombuffer(row["embedding"], dtype=np.float32)
            similarity = float(np.dot(query_vec, stored_vec) /
                             (np.linalg.norm(query_vec) * np.linalg.norm(stored_vec)))
            results.append((similarity, row["memory_id"], row["content"],
                          row["memory_type"], row["importance"]))

        results.sort(reverse=True)

        # Update access stats
        for _, mid, *_ in results[:top_k]:
            self.conn.execute(
                "UPDATE z_memory SET accessed_at=CURRENT_TIMESTAMP, access_count=access_count+1 WHERE memory_id=?",
                (mid,)
            )
        self.conn.commit()

        return results[:top_k]

    def context(self, query: str, top_k: int = 5, memory_type: str = None) -> str:
        """Get relevant memories formatted for LLM context injection.

        Args:
            query: Search text
            top_k: Number of memories to include
            memory_type: Filter by type

        Returns:
            Markdown-formatted memory context
        """
        memories = self.recall(query, top_k, memory_type)
        if not memories:
            return ""

        lines = ["## Relevant Memories"]
        for sim, mid, content, mtype, importance in memories:
            lines.append(f"- [{mtype}] {content}")
        return "\n".join(lines)

    def forget(self, memory_id: int, hard_delete: bool = False):
        """Remove a memory.

        Args:
            memory_id: ID of memory to remove
            hard_delete: If True, permanently delete. Otherwise soft-delete (recoverable).
        """
        if hard_delete:
            self.conn.execute("DELETE FROM z_memory WHERE memory_id=?", (memory_id,))
        else:
            self.conn.execute("UPDATE z_memory SET is_active=0 WHERE memory_id=?", (memory_id,))
        self.conn.commit()

    def restore(self, memory_id: int) -> bool:
        """Restore a purged memory (undo soft-delete).

        Args:
            memory_id: ID of memory to restore

        Returns:
            True if a memory was restored, False if not found or not purged
        """
        cursor = self.conn.execute(
            "UPDATE z_memory SET deleted_at = NULL, is_active = 1 WHERE memory_id = ? AND deleted_at IS NOT NULL",
            (memory_id,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def reinforce(self, memory_id: int, boost: int = 1):
        """Increase importance of a memory.

        Args:
            memory_id: ID of memory to boost
            boost: Amount to increase (default 1, max importance is 10)
        """
        self.conn.execute(
            "UPDATE z_memory SET importance = MIN(10, importance + ?) WHERE memory_id=?",
            (boost, memory_id)
        )
        self.conn.commit()

    # =========================================================================
    # LIFECYCLE OPERATIONS
    # =========================================================================

    def decay(self, days_old: int = 30, importance_threshold: int = 3, dry_run: bool = True) -> dict:
        """Soft-delete old, low-importance, rarely-accessed memories.

        Args:
            days_old: Minimum age to consider
            importance_threshold: Max importance to decay
            dry_run: If True, report without changing

        Returns:
            dict with decayed_count, decayed_ids, candidates
        """
        script = self._get_script("decay_memories")
        exec(script, globals())
        return decay_memories(self.conn, days_old, importance_threshold, 3, dry_run)

    def purge(self, days_inactive: int = 90, dry_run: bool = True) -> dict:
        """Soft-delete long-inactive memories (sets deleted_at, preserves for recovery).

        Args:
            days_inactive: Days since deactivation
            dry_run: If True, report without changing

        Returns:
            dict with purged_count, purged_ids
        """
        script = self._get_script("purge_memories")
        exec(script, globals())
        return purge_memories(self.conn, days_inactive, dry_run)

    # =========================================================================
    # INTROSPECTION
    # =========================================================================

    def stats(self) -> dict:
        """Get memory statistics."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) as purged,
                COUNT(DISTINCT memory_type) as types,
                AVG(importance) as avg_importance,
                AVG(access_count) as avg_access
            FROM z_memory
        """).fetchone()
        return {
            "total": row["total"],
            "active": row["active"],
            "purged": row["purged"],
            "types": row["types"],
            "avg_importance": round(row["avg_importance"] or 0, 2),
            "avg_access": round(row["avg_access"] or 0, 2),
        }

    def list_memories(self, include_inactive: bool = False, limit: int = 20) -> List[dict]:
        """List memories (excludes purged)."""
        sql = "SELECT memory_id, content, memory_type, importance, access_count, is_active FROM z_memory WHERE deleted_at IS NULL"
        if not include_inactive:
            sql += " AND is_active=1"
        sql += " ORDER BY importance DESC, access_count DESC LIMIT ?"

        rows = self.conn.execute(sql, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def bootstrap(self) -> str:
        """Get the system bootstrap prompt."""
        row = self.conn.execute(
            "SELECT prompt_template FROM z_prompt_catalog WHERE prompt_nickname='system_bootstrap'"
        ).fetchone()
        return row["prompt_template"] if row else ""

    def version(self) -> str:
        """Get current version."""
        row = self.conn.execute(
            "SELECT version, description FROM z_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return f"{row['version']}: {row['description']}"

    # =========================================================================
    # INTERNAL
    # =========================================================================

    def _migrate(self):
        """Auto-migrate schema for older databases."""
        cursor = self.conn.execute("PRAGMA table_info(z_memory)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "deleted_at" not in columns:
            self.conn.execute("ALTER TABLE z_memory ADD COLUMN deleted_at TIMESTAMP")
            self.conn.commit()

    def _get_script(self, name: str) -> str:
        """Get script body from z_script_catalog."""
        row = self.conn.execute(
            "SELECT script_body FROM z_script_catalog WHERE script_name=? AND is_active=1",
            (name,)
        ).fetchone()
        if not row:
            raise ValueError(f"Script '{name}' not found")
        return row["script_body"]

    def help(self):
        """Show available operations."""
        print("""
Agent Memory System
===================

STORE:
  memory.remember(content, type, source, importance=5)  - Store with explicit importance
  memory.smart_remember(content, type, source)          - Auto-assess importance

RECALL:
  memory.recall(query, top_k=5)                         - Semantic search
  memory.context(query)                                 - Formatted for LLM injection

MANAGE:
  memory.reinforce(memory_id, boost=1)                  - Increase importance
  memory.forget(memory_id, hard_delete=False)           - Remove memory
  memory.restore(memory_id)                             - Recover a purged memory

LIFECYCLE:
  memory.decay(days_old=30, dry_run=True)               - Soft-delete old noise
  memory.purge(days_inactive=90, dry_run=True)          - Soft-delete inactive (recoverable)

INTROSPECT:
  memory.stats()                                        - Memory statistics
  memory.list_memories()                                - List all memories
  memory.bootstrap()                                    - Get bootstrap prompt
  memory.version()                                      - Database version

Memory Types: fact, preference, lesson, decision, insight, error, security
""")

    def __repr__(self):
        stats = self.stats()
        return f"<AgentMemory memories={stats['active']} version='{self.version().split(':')[0]}'>"


# Convenience function
def connect(db_path: str = None) -> AgentMemory:
    """Connect to agent memory database."""
    return AgentMemory(db_path)


if __name__ == "__main__":
    memory = AgentMemory()
    memory.help()
    print(f"\n{memory}")
    print(f"\nBootstrap prompt available: {len(memory.bootstrap())} chars")
