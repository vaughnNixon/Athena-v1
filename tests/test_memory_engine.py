import pytest
import tempfile
import shutil
import sqlite3
import os
from pathlib import Path

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    # Cleanup temp db
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_fact_normalization_and_hashing():
    fact = "  Athena v1 is a memory-first agent!  "
    norm = memory_engine.normalize_fact_text(fact)
    assert norm == "athena v1 is a memoryfirst agent"
    
    hash1 = memory_engine.compute_fact_hash(fact)
    hash2 = memory_engine.compute_fact_hash("athena v1 is a memoryfirst agent")
    assert hash1 == hash2

def test_insert_and_reinforce():
    fact = "User likes Python."
    # Insert new fact
    res1 = memory_engine.insert_or_reinforce_fact(fact, category="user_pref", importance=6, confidence=0.8)
    assert res1 == "inserted"
    
    # Verify DB contents
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT fact, category, importance, confidence, mention_count, archived FROM facts")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "User likes Python."
    assert row[1] == "user_pref"
    assert row[2] == 6
    assert row[3] == 0.8
    assert row[4] == 1
    assert row[5] == 0
    conn.close()
    
    # Reinforce the same fact
    res2 = memory_engine.insert_or_reinforce_fact(fact, category="user_pref", importance=6, confidence=0.8)
    assert res2 == "reinforced"
    
    # Verify reinforcement boosted values
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT importance, confidence, mention_count FROM facts")
    row = cursor.fetchone()
    assert row[0] == 7  # Importance promoted (6 + 1)
    assert row[1] == 0.9 # Confidence promoted (0.8 + 0.1)
    assert row[2] == 2   # Mention count incremented
    conn.close()
