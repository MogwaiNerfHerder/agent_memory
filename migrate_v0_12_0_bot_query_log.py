"""
migrate_v0_12_0_bot_query_log.py - Audit table for bot-initiated knowledge queries.

Every query the knowledge bot runs writes a row here. Useful for:
  - Verifying the channel→client_slug isolation actually held
  - Forensics if a bug ever surfaced cross-tenant data
  - Usage analytics

Idempotent.
"""

import argparse
import os
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default=os.path.join(os.path.dirname(__file__), "agent_memory.db"))
    args = p.parse_args()
    conn = sqlite3.connect(os.path.expanduser(args.path))
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Migrating {args.path} -> v0.12.0 (bot_query_log)...")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_query_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            client_slug TEXT NOT NULL REFERENCES client(slug) ON DELETE RESTRICT,
            slack_user_id TEXT,
            slack_thread_ts TEXT,
            tool_name TEXT NOT NULL,
            tool_args TEXT,
            result_summary TEXT,
            result_count INTEGER,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_query_log_channel ON bot_query_log(channel_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_query_log_client ON bot_query_log(client_slug, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_query_log_user ON bot_query_log(slack_user_id, created_at DESC)")
    print("  bot_query_log created (or already existed)")

    docs = [
        ("table", "bot_query_log", None,
         "Audit trail for bot-initiated knowledge queries. Every channel→client_slug-scoped query the knowledge bot runs writes a row. Used for forensics if cross-tenant access ever happens."),
        ("column", "bot_query_log", "log_id", "Primary key"),
        ("column", "bot_query_log", "channel_id", "Slack channel where the query originated"),
        ("column", "bot_query_log", "client_slug", "Client tenant the channel resolved to (FK to client.slug). The wrapper looked this up from channel_routing.json before the query ran."),
        ("column", "bot_query_log", "slack_user_id", "Slack user who @-mentioned the bot"),
        ("column", "bot_query_log", "slack_thread_ts", "Slack thread timestamp (for tracing back to the conversation)"),
        ("column", "bot_query_log", "tool_name", "Which knowledge tool ran (dossier, notes, find_entity, timeline, ...)"),
        ("column", "bot_query_log", "tool_args", "JSON dump of the tool's args (excluding client_slug which is always = the resolved one)"),
        ("column", "bot_query_log", "result_summary", "Short summary of what was returned (e.g. 'dossier on Hans Sherman: 38 notes')"),
        ("column", "bot_query_log", "result_count", "Numeric count where applicable (rows returned)"),
        ("column", "bot_query_log", "duration_ms", "Wall-clock duration of the underlying query"),
        ("column", "bot_query_log", "created_at", "When the query ran"),
    ]
    conn.executemany("INSERT OR REPLACE INTO z_schema VALUES (?,?,?,?)", docs)

    row = conn.execute("SELECT version FROM z_version ORDER BY id DESC LIMIT 1").fetchone()
    if not (row and row[0] == "v0.12.0"):
        conn.execute(
            "INSERT INTO z_version (version, description) VALUES ('v0.12.0', 'Add bot_query_log table for auditing knowledge-bot queries with channel→client_slug attribution')"
        )
        print("  bumped to v0.12.0")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
