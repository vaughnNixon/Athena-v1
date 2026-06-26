import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import memory_gating

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_gating_failed_outcome():
    payload = ["This is a valid long memory observation string."]
    aal = {"outcome": "failed", "confidence": 0.9}
    res = memory_gating.filter(payload, aal)
    assert not res["accepted"]
    assert len(res["rejected"]) == 1
    assert "failed" in res["reason"]

def test_gating_low_confidence():
    payload = ["This is a valid long memory observation string."]
    aal = {"outcome": "success", "confidence": 0.2}
    res = memory_gating.filter(payload, aal)
    assert not res["accepted"]
    assert len(res["rejected"]) == 1
    assert "confidence" in res["reason"].lower()

def test_gating_filtering_short_and_empty():
    payload = [
        "This is a valid long memory observation string that should be accepted.",
        "short",  # too short
        ""  # empty
    ]
    aal = {"outcome": "success", "confidence": 0.8}
    res = memory_gating.filter(payload, aal)
    assert len(res["accepted"]) == 1
    assert len(res["rejected"]) == 2
    assert "observation string" in res["accepted"][0]

def test_gating_duplicate_detection():
    fact_text = "This is a pre-existing fact observation in the memory."
    chunk_text = "This is a pre-existing chunk observation in the database."

    # 1. Insert pre-existing fact
    memory_engine.insert_or_reinforce_fact(
        fact=fact_text,
        category="general",
        importance=5,
        confidence=0.8,
        scope_ids=["test"]
    )
    
    # 2. Insert pre-existing chunk
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
                VALUES (1, 'unclassified', ?, 'preexist chunk', 0, 0, 10, 2, '{}', 0, 0)
            """, (chunk_text,))
    finally:
        conn.close()

    # Try gating payload containing duplicates
    payload = [
        fact_text,
        chunk_text,
        "This is a brand new unique memory observation string that should be accepted."
    ]
    aal = {"outcome": "success", "confidence": 0.8}
    res = memory_gating.filter(payload, aal)
    assert len(res["accepted"]) == 1
    assert "brand new" in res["accepted"][0]
    assert len(res["rejected"]) == 2
