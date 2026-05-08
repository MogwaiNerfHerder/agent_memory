"""
script_loader.py - Load access primitives from z_script_catalog and call them.

This module is the bridge between Python orchestration code and the
registered access scripts in z_script_catalog. It loads each script's body
once (lazy, cached), execs it into a private namespace, and exposes a
callable. All db access primitives should be invoked through ScriptCatalog
rather than reimplemented inline — that way the catalog stays the single
source of truth.

Usage:
    sc = ScriptCatalog(conn)
    result = sc.call("write_memory_for_client", client_id, content, "decision",
                     importance=8, sensitivity="sensitive")
    matches = sc.call("find_entity_by_alias", client_id, "Hans")
"""

import sqlite3


class ScriptCatalog:
    """Lazy-loaded, cached registry of scripts from z_script_catalog."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cache = {}  # name -> callable

    def call(self, name: str, *args, **kwargs):
        fn = self._load(name)
        return fn(self.conn, *args, **kwargs)

    def _load(self, name: str):
        if name in self._cache:
            return self._cache[name]
        row = self.conn.execute(
            "SELECT script_body FROM z_script_catalog WHERE script_name=? AND is_active=1",
            (name,),
        ).fetchone()
        if not row:
            raise KeyError(f"Script {name!r} not in z_script_catalog. Migrate or check name.")
        ns = {}
        exec(row[0], ns)
        if name not in ns:
            raise RuntimeError(
                f"Script {name!r} loaded but no top-level function with that name was defined."
            )
        self._cache[name] = ns[name]
        return ns[name]

    def has(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM z_script_catalog WHERE script_name=? AND is_active=1", (name,)
        ).fetchone() is not None
