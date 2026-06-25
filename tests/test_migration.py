import pytest
import os
import tempfile
import sqlite3
import json
from pathlib import Path
from unittest.mock import patch

# Override home dir for hermetic testing BEFORE importing config
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    # Cleanup DB file
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_db_initialization_creates_new_tables():
    # initialize_db should have run in the fixture
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Verify tables exist
        tables = ["facts", "sessions", "chunks", "chunk_keywords", "skip_marks", "feedback_log", "schema_metadata"]
        for table in tables:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            assert cursor.fetchone() is not None, f"Table {table} does not exist"
            
        # Verify indexes exist
        indexes = ["idx_chunks_tier", "idx_chunks_timestamps", "idx_chunks_migrated_from", "idx_chunk_keywords_val"]
        for idx in indexes:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (idx,))
            assert cursor.fetchone() is not None, f"Index {idx} does not exist"
            
        # Verify foreign keys are enabled
        cursor.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1, "Foreign keys are not enabled"
        
    finally:
        conn.close()

def test_migration_is_idempotent_and_resumable():
    # 1. Insert 3 legacy facts
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO facts (fact_hash, fact, category, importance, confidence, decay_rate, mention_count, scope_ids, archived, created_at, updated_at)
            VALUES 
            ('hash1', 'Lucky is a Doberman dog.', 'dogs', 8, 0.9, 0.05, 1, '["default"]', 0, 1000, 1001),
            ('hash2', 'Indian Pariah is native to India.', 'breeds', 5, 0.8, 0.05, 2, '["default"]', 0, 1010, 1011),
            ('hash3', 'Nixon prefers Python.', 'general', 3, 0.7, 0.05, 1, '["default"]', 1, 1020, 1021)
        """)
        conn.commit()
    finally:
        conn.close()
        
    # 2. Verify initial progress stats
    prog1 = memory_engine.get_migration_progress()
    assert prog1["total_facts"] == 3
    assert prog1["migrated_facts"] == 0
    assert prog1["remaining_facts"] == 3
    assert prog1["percentage_complete"] == 0.0
    
    # 3. Run migration
    res1 = memory_engine.migrate_legacy_facts()
    assert res1["migrated_facts"] == 3
    assert res1["remaining_facts"] == 0
    assert res1["percentage_complete"] == 100.0
    
    # 4. Check chunks content in DB
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chunk_id, tier, raw_text, caveman_text, start_ts, end_ts, migrated_from_fact_id, metadata FROM chunks ORDER BY chunk_id ASC")
        chunks = cursor.fetchall()
        assert len(chunks) == 3
        
        # Verify first chunk details
        c_id, tier, raw, cave, start, end, mig_id, meta_json = chunks[0]
        assert tier == "unclassified" # Default tier for migrated facts
        assert raw == "Lucky is a Doberman dog."
        assert "LUCKY" in cave and "DOBERMAN" in cave
        assert start == 1000
        assert end == 1001
        assert mig_id is not None
        
        meta = json.loads(meta_json)
        assert meta["legacy_category"] == "dogs"
        assert meta["legacy_importance"] == 8
        assert "workspace" in meta
        assert meta["workspace"] is None
        
        # Check normalized keywords in chunk_keywords table
        cursor.execute("SELECT keyword FROM chunk_keywords WHERE chunk_id = ? ORDER BY keyword ASC", (c_id,))
        keywords = [row[0] for row in cursor.fetchall()]
        assert "lucky" in keywords
        assert "doberman" in keywords
        assert "dogs" in keywords # Category added to keywords
        assert "default" in keywords # Scope ID added to keywords
        
        # Verify schema_metadata updates
        cursor.execute("SELECT status FROM schema_metadata WHERE table_name = 'facts'")
        assert cursor.fetchone()[0] == "deprecated"
        cursor.execute("SELECT status FROM schema_metadata WHERE table_name = 'chunks'")
        assert cursor.fetchone()[0] == "active"
        
    finally:
        conn.close()

    # 5. Insert another fact and run migration again (Resumability/Idempotency check)
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO facts (fact_hash, fact, category, importance, confidence, decay_rate, mention_count, scope_ids, archived, created_at, updated_at)
            VALUES ('hash4', 'Athena remembers everything.', 'general', 6, 0.8, 0.05, 1, '["default"]', 0, 1030, 1031)
        """)
        conn.commit()
    finally:
        conn.close()
        
    prog2 = memory_engine.get_migration_progress()
    assert prog2["total_facts"] == 4
    assert prog2["migrated_facts"] == 3
    assert prog2["remaining_facts"] == 1
    
    res2 = memory_engine.migrate_legacy_facts()
    assert res2["migrated_facts"] == 4
    assert res2["remaining_facts"] == 0
    assert res2["percentage_complete"] == 100.0

def test_migration_rollback_on_failure():
    # 1. Insert a legacy fact
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO facts (fact_hash, fact, category, importance, confidence, decay_rate, mention_count, scope_ids, archived, created_at, updated_at)
            VALUES ('hash1', 'Rollback test fact.', 'test', 5, 0.8, 0.05, 1, '[]', 0, 2000, 2001)
        """)
        conn.commit()
    finally:
        conn.close()

    # 2. Define wrapper classes to simulate DB execution failure mid-transaction
    class ErrorInducingCursor:
        def __init__(self, real_cursor):
            self.real_cursor = real_cursor
        def __getattr__(self, name):
            return getattr(self.real_cursor, name)
        def execute(self, sql, *args):
            if "INSERT INTO chunk_keywords" in sql:
                raise sqlite3.OperationalError("Simulated database failure during keyword insertion")
            return self.real_cursor.execute(sql, *args)

    class ErrorInducingConnection:
        def __init__(self, real_conn):
            self.real_conn = real_conn
        def __getattr__(self, name):
            return getattr(self.real_conn, name)
        def cursor(self):
            return ErrorInducingCursor(self.real_conn.cursor())
        def __enter__(self):
            self.real_conn.__enter__()
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return self.real_conn.__exit__(exc_type, exc_val, exc_tb)

    original_get_conn = memory_engine.get_db_connection
    def mock_get_db_connection():
        return ErrorInducingConnection(original_get_conn())

    with patch("memory_engine.get_db_connection", new=mock_get_db_connection):
        with pytest.raises(sqlite3.OperationalError):
            memory_engine.migrate_legacy_facts()

    # 3. Verify that rollback happened: facts are untouched, chunks table is completely empty!
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM chunks")
        assert cursor.fetchone()[0] == 0, "Chunks were not rolled back"
        
        cursor.execute("SELECT COUNT(*) FROM facts")
        assert cursor.fetchone()[0] == 1, "Legacy facts table was corrupted"
        
        # Verify schema_metadata remains unpopulated/unaltered
        cursor.execute("SELECT COUNT(*) FROM schema_metadata")
        assert cursor.fetchone()[0] == 0
    finally:
        conn.close()
