import pytest
import os
import tempfile
import sqlite3
import json
from pathlib import Path

# Override home dir for hermetic testing BEFORE importing config
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import memory_sweep

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    
    # Configure a tiny active budget for testing
    cfg = config.load_config()
    cfg.setdefault("memory", {})
    cfg["memory"]["active_token_budget"] = 100
    config.save_config(cfg)
    
    yield
    # Cleanup DB file
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_classify_and_maintain_budget():
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        chunks_data = [
            (1, "unclassified", "Chunk 1", "chunk 1", 1000, 1001, 10, 40, "{}", 1000, 1000),
            (2, "unclassified", "Chunk 2", "chunk 2", 1010, 1011, 10, 50, "{}", 1010, 1010),
            (3, "unclassified", "Chunk 3", "chunk 3", 1020, 1021, 10, 30, "{}", 1020, 1020),
            (4, "unclassified", "Chunk 4", "chunk 4", 1030, 1031, 10, 20, "{}", 1030, 1030)
        ]
        cursor.executemany("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, chunks_data)
        conn.commit()
    finally:
        conn.close()
        
    memory_sweep.run_memory_sweep()
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT sequence_number, tier, metadata FROM chunks ORDER BY sequence_number ASC")
        rows = cursor.fetchall()
        assert rows[0][0] == 1 and rows[0][1] == "passive"
        assert rows[1][0] == 2 and rows[1][1] == "active"
        assert rows[2][0] == 3 and rows[2][1] == "active"
        assert rows[3][0] == 4 and rows[3][1] == "active"
    finally:
        conn.close()

def test_mixed_boundary_chunk():
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        chunks_data = [
            (1, "unclassified", "Chunk 1", "chunk 1", 1000, 1001, 10, 40, "{}", 1000, 1000),
            (2, "unclassified", "Chunk 2", "chunk 2", 1010, 1011, 10, 55, "{}", 1010, 1010),
            (3, "unclassified", "Chunk 3", "chunk 3", 1020, 1021, 10, 30, "{}", 1020, 1020),
            (4, "unclassified", "Chunk 4", "chunk 4", 1030, 1031, 10, 20, "{}", 1030, 1030)
        ]
        cursor.executemany("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, chunks_data)
        conn.commit()
    finally:
        conn.close()
        
    memory_sweep.run_memory_sweep()
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT sequence_number, tier, metadata FROM chunks ORDER BY sequence_number ASC")
        rows = cursor.fetchall()
        
        assert rows[0][0] == 1 and rows[0][1] == "passive"
        assert rows[1][0] == 2 and rows[1][1] == "mixed"
        assert rows[2][0] == 3 and rows[2][1] == "active"
        assert rows[3][0] == 4 and rows[3][1] == "active"
        
        meta = json.loads(rows[1][2])
        assert "Spans Active/Passive boundary" in meta["annotation"]
        assert "workspace" in meta
    finally:
        conn.close()

def test_idempotency():
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        chunks_data = [
            (1, "unclassified", "Chunk 1", "chunk 1", 1000, 1001, 10, 40, "{}", 1000, 1000),
            (2, "unclassified", "Chunk 2", "chunk 2", 1010, 1011, 10, 55, "{}", 1010, 1010),
            (3, "unclassified", "Chunk 3", "chunk 3", 1020, 1021, 10, 30, "{}", 1020, 1020)
        ]
        cursor.executemany("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, chunks_data)
        conn.commit()
    finally:
        conn.close()
        
    memory_sweep.run_memory_sweep()
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chunk_id, tier, updated_at FROM chunks")
        state1 = cursor.fetchall()
    finally:
        conn.close()
        
    memory_sweep.run_memory_sweep()
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chunk_id, tier, updated_at FROM chunks")
        state2 = cursor.fetchall()
    finally:
        conn.close()
        
    assert state1 == state2
