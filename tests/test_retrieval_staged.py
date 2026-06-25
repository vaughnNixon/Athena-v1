import pytest
import os
import tempfile
import time
import json
from unittest.mock import patch, MagicMock

# Setup hermetic environment
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import retrieval

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

def test_query_classification():
    # Test preferences
    assert retrieval.classify_query_intent("I prefer using python") == "preferences"
    assert retrieval.classify_query_intent("Nixon dislike Java") == "preferences"
    # Test projects
    assert retrieval.classify_query_intent("Let's build the app") == "projects"
    # Test timeline
    assert retrieval.classify_query_intent("what did we do yesterday") == "timeline"
    # Test people
    assert retrieval.classify_query_intent("who is Nixon") == "people"
    # Test tasks
    assert retrieval.classify_query_intent("check my todo list") == "tasks"
    # Test technical
    assert retrieval.classify_query_intent("database sqlite connection error") == "technical"
    # Test past_events
    assert retrieval.classify_query_intent("do you remember the history") == "past_events"
    # Test general fallback
    assert retrieval.classify_query_intent("hello world") == "general"

def test_staged_retrieval_flow():
    # Insert chunks with different tiers
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        
        # 1. Insert an active chunk about python
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "active", "We are building an agent in Python.", "building agent python", 1000, 1001, 35, 10, '{"projects": ["python", "athena"]}', 1000, 1000))
        active_chunk_id = cursor.lastrowid
        
        # Keywords for active chunk
        cursor.executemany("""
            INSERT INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)
        """, [(active_chunk_id, "agent"), (active_chunk_id, "python"), (active_chunk_id, "building")])
        
        # 2. Insert a passive chunk about java
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (2, "passive", "Nixon dislikes Java coding style.", "dislike java coding style", 1010, 1011, 32, 10, '{"legacy_category": "java"}', 1010, 1010))
        passive_chunk_id = cursor.lastrowid
        
        # Keywords for passive chunk
        cursor.executemany("""
            INSERT INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)
        """, [(passive_chunk_id, "dislike"), (passive_chunk_id, "java"), (passive_chunk_id, "coding")])
        
        conn.commit()
    finally:
        conn.close()

    # Configure defaults
    cfg = config.load_config()
    cfg["memory"] = {
        "keyword_confidence_threshold": 0.5,
        "embedding_enabled": False,
        "desperation_enabled": True
    }
    with patch("config.load_config", return_value=cfg):
        # Case A: Active match succeeds (Stage 1)
        res = retrieval.retrieve_memories_staged("building python agent")
        assert res["retrieval_stage"] == "active_search"
        assert active_chunk_id in res["matched_chunk_ids"]
        assert passive_chunk_id not in res["matched_chunk_ids"]
        
        # Case B: Active fails, Passive succeeds (Stage 2)
        res = retrieval.retrieve_memories_staged("java coding style")
        assert res["retrieval_stage"] == "passive_search"
        assert passive_chunk_id in res["matched_chunk_ids"]
        assert active_chunk_id not in res["matched_chunk_ids"]

def test_desperation_mode_trigger():
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "active", "We are building an agent in Python.", "building agent python", 1000, 1001, 35, 10, '{}', 1000, 1000))
        active_chunk_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    cfg = config.load_config()
    cfg["memory"] = {
        "keyword_confidence_threshold": 0.8,
        "embedding_enabled": False,
        "desperation_enabled": True
    }
    with patch("config.load_config", return_value=cfg):
        # Explicit error keyword "wrong" forces desperation mode
        res = retrieval.retrieve_memories_staged("that is wrong")
        assert res["retrieval_stage"] == "desperation_mode"
        assert active_chunk_id in res["matched_chunk_ids"]
        
        # Low confidence match triggers desperation mode
        res = retrieval.retrieve_memories_staged("ruby code")
        assert res["retrieval_stage"] == "desperation_mode"

def test_empty_database_fallback():
    # Empty DB retrieval
    res = retrieval.retrieve_memories_staged("any query")
    assert res["retrieval_stage"] == "none"
    assert res["matched_chunk_ids"] == []
    assert res["message"] == "I couldn't find a reliable memory for that request."

def test_semantic_retrieval_and_embeddings():
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "active", "Machine learning semantic search.", "machine learning semantic", 1000, 1001, 33, 10, '{}', 1000, 1000))
        chunk_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    cfg = config.load_config()
    cfg["memory"] = {
        "keyword_confidence_threshold": 0.9,  # Force it to bypass active keyword match
        "embedding_enabled": True,
        "embedding_provider": "gemini",
        "embedding_top_k": 1,
        "desperation_enabled": False
    }

    # Mock the providers routing client
    mock_client = MagicMock()
    mock_embeddings = MagicMock()
    mock_embeddings.create.return_value.data = [MagicMock(embedding=[0.1, 0.2, 0.3, 0.4])]
    mock_client.embeddings = mock_embeddings
    
    with patch("config.load_config", return_value=cfg), \
         patch("providers.get_routing_client", return_value=(mock_client, "text-embedding-004", "gemini")):
         
        # Run retrieval - first time (should generate and insert cache)
        res = retrieval.retrieve_memories_staged("semantic database")
        assert res["retrieval_stage"] == "semantic_search"
        assert chunk_id in res["matched_chunk_ids"]
        
        # Verify that chunk_embeddings was written
        conn = memory_engine.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT provider, model, dimensions, embedding FROM chunk_embeddings WHERE chunk_id = ?", (chunk_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "gemini"
            assert row[1] == "text-embedding-004"
            assert row[2] == 4
            vector = retrieval.blob_to_vector(row[3])
            assert pytest.approx(vector) == [0.1, 0.2, 0.3, 0.4]
        finally:
            conn.close()
            
        # Run retrieval - second time (should hit cache - mock_client.embeddings.create should only be called for the query embedding, not the chunk)
        mock_embeddings.create.reset_mock()
        mock_embeddings.create.return_value.data = [MagicMock(embedding=[0.1, 0.2, 0.3, 0.4])]
        
        res = retrieval.retrieve_memories_staged("semantic database")
        assert res["retrieval_stage"] == "semantic_search"
        # Mock embeddings.create should be called once (for the query), not twice (because chunk is cached)
        assert mock_embeddings.create.call_count == 1
