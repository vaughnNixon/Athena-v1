import pytest
import os
import tempfile
import sqlite3
import json
import time
from unittest.mock import patch, MagicMock

# Setup hermetic environment
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir
os.environ["ATHENA_TESTING"] = "1"  # Bypass non-gaming timers during test run

import config
import memory_engine
import retrieval
import learning_engine

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

def test_correction_intent_classification():
    assert retrieval.classify_query_intent("Wrong.") == "correction"
    assert retrieval.classify_query_intent("No, you got it wrong.") == "correction"
    assert retrieval.classify_query_intent("That is incorrect memory") == "correction"
    assert retrieval.classify_query_intent("Try again.") == "correction"
    assert retrieval.classify_query_intent("Not what I meant.") == "correction"
    assert retrieval.classify_query_intent("You missed the dental project context") == "correction"
    assert retrieval.classify_query_intent("regular query about python") != "correction"

def test_learning_pipeline_skip_marks_tuning():
    # Insert chunks
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        # Chunk 1: Matched chunk that was incorrect
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "active", "We build Java backend software.", "build java backend", 1000, 1001, 30, 10, '{"projects": ["java"]}', 1000, 1000))
        c1_id = cursor.lastrowid
        
        # Chunk 2: Desperation chunk that contained correct facts
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (2, "passive", "Dental clinic: Grace Dental, Kochi", "dental clinic grace dental", 1010, 1011, 35, 10, '{"projects": ["dental"]}', 1010, 1010))
        c2_id = cursor.lastrowid
        
        cursor.executemany("INSERT INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)", [
            (c1_id, "java"), (c1_id, "backend"),
            (c2_id, "dental"), (c2_id, "grace"), (c2_id, "clinic")
        ])
        conn.commit()
    finally:
        conn.close()
        
    last_retrieval_info = {
        "query": "tell me about the project",
        "matched_chunk_ids": [c1_id],
        "retrieval_stage": "active_search",
        "timestamp": int(time.time())
    }
    
    # Mock LLM choosing c2_id as the useful one
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps({"useful_chunk_ids": [c2_id]})
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "mock-model", "mock-provider")):
        res = learning_engine.learn_from_feedback(
            user_query="tell me about the project",
            user_correction="Wrong, I meant the dental project.",
            last_retrieval_info=last_retrieval_info,
            prev_response_text="We did a Java backend project."
        )
        
    assert res["success"] is True
    assert c2_id in res["useful_chunk_ids"]
    assert c1_id in res["penalized_chunk_ids"]
    
    # Query database and verify skip marks
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        # c1_id (irrelevant matched) should be penalized (score 0.2)
        cursor.execute("SELECT skip_score, feedback_count FROM skip_marks WHERE chunk_id = ?", (c1_id,))
        row1 = cursor.fetchone()
        assert row1 is not None
        assert row1[0] == 0.2
        assert row1[1] == 1
        
        # c2_id (useful) should be rewarded (score 0.0)
        cursor.execute("SELECT skip_score, feedback_count FROM skip_marks WHERE chunk_id = ?", (c2_id,))
        row2 = cursor.fetchone()
        assert row2 is not None
        assert row2[0] == 0.0
        assert row2[1] == 1
        
        # Verify query_statistics
        cursor.execute("SELECT total_queries, corrected_queries, accuracy FROM query_statistics WHERE query_type = 'projects'")
        stats = cursor.fetchone()
        assert stats is not None
        assert stats[1] == 1
        assert stats[2] == 0.0
        
    finally:
        conn.close()

def test_anti_gaming_mechanisms():
    # When ATHENA_TESTING is unset, it should block rate-limited or stale events
    with patch.dict(os.environ):
        if "ATHENA_TESTING" in os.environ:
            del os.environ["ATHENA_TESTING"]
        learning_engine._last_learning_ts = time.time()
        res = learning_engine.learn_from_feedback(
            user_query="test",
            user_correction="Wrong",
            last_retrieval_info={"timestamp": time.time()},
            prev_response_text="wrong"
        )
        assert res["success"] is False
        assert "Rate limit" in res["explanation"]

def test_rollback_cleanse():
    # Populate some skip marks and statistics
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
                VALUES (1, 'active', 'dummy', 'dummy', 1000, 1000, 5, 1, '{}', 1000, 1000)
            """)
            chunk_id = cursor.lastrowid
            conn.execute("INSERT INTO skip_marks (chunk_id, query_type, skip_score, feedback_count, last_updated_ts) VALUES (?, 'projects', 0.5, 2, 1000)", (chunk_id,))
            conn.execute("INSERT INTO query_statistics (query_type, total_queries, corrected_queries, accuracy, last_updated_ts) VALUES ('projects', 10, 2, 0.8, 1000)")
    finally:
        conn.close()
        
    learning_engine.reset_skip_marks()
    learning_engine.reset_query_statistics()
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM skip_marks")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT COUNT(*) FROM query_statistics")
        assert cursor.fetchone()[0] == 0
    finally:
        conn.close()
