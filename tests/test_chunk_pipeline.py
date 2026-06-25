import pytest
import os
import tempfile
import sqlite3
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# Override home dir for hermetic testing BEFORE importing config
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import chunk_pipeline

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

def test_detect_sentences():
    text = "Hello world! This is v1.1 of Athena. See https://athena.ai/info. 3.14 is pi? Yes, e.g. correct."
    sentences = chunk_pipeline.detect_sentences(text)
    assert sentences == [
        "Hello world!",
        "This is v1.1 of Athena.",
        "See https://athena.ai/info.",
        "3.14 is pi?",
        "Yes, e.g. correct."
    ]

def test_split_long_sentence():
    text = "first part of text; second part-third part,end."
    parts = chunk_pipeline.split_long_sentence(text, limit=20)
    assert parts == ["first part of text;", " second part-", "third part,end."]

def test_build_chronological_chunks_preserves_order():
    messages = [
        {"role": "user", "content": "Hello. I want to build a house that has at least five bedrooms, a large kitchen, three bathrooms, a double garage, and a beautiful garden in the backyard."},
        {"role": "assistant", "content": "Sure, I can help you design that. What is your estimated budget for this project? We will need to plan carefully to fit all these requirements."},
        {"role": "user", "content": "My budget is around $500,000. Do you think we can also include a swimming pool and a small guest house within this budget, or would that be too tight?"}
    ]
    chunks = chunk_pipeline.build_chronological_chunks(messages, target_chunk_size=250)
    assert len(chunks) > 1
    first_chunk = chunks[0]["raw_text"]
    assert "User: Hello. I want to build a house" in first_chunk

def test_tiny_chunk_merging():
    messages = [
        {"role": "user", "content": "Tiny."},
        {"role": "assistant", "content": "Indeed."},
        {"role": "user", "content": "Let's talk about something extremely long to satisfy the normal chunk length requirements, such as a detailed discussion about database schemas, API routing, and software architecture."}
    ]
    chunks = chunk_pipeline.build_chronological_chunks(messages, target_chunk_size=1000)
    assert len(chunks) == 1
    raw_text = chunks[0]["raw_text"]
    assert "User: Tiny." in raw_text
    assert "Assistant: Indeed." in raw_text

def test_deterministic_fallback_caveman_and_keywords():
    text = "User prefers Python for backend work. Implemented OAuth rotation successfully."
    res = chunk_pipeline.fallback_enrich_chunk(text)
    assert "python" in res["caveman_text"]
    assert "oauth" in res["caveman_text"]
    assert "for" not in res["caveman_text"]
    assert "python" in res["keywords"]
    assert "oauth" in res["keywords"]
    assert res["metadata"]["enrichment_type"] == "fallback"

def test_llm_enrichment_success():
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    
    mock_json_content = json.dumps({
        "caveman_text": "user prefer python backend. oauth rotation implemented.",
        "keywords": ["python", "backend", "oauth", "rotation"],
        "annotations": {
            "entities": ["user"],
            "projects": ["oauth rotation"],
            "technologies": ["python"],
            "themes": ["backend development"]
        }
    })
    mock_message.content = mock_json_content
    mock_choice.message = mock_message
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "mock-model", "mock-provider")):
        enrichment = chunk_pipeline.enrich_chunk_with_llm("test chunk text")
        
    assert enrichment["caveman_text"] == "user prefer python backend. oauth rotation implemented."
    assert "python" in enrichment["keywords"]
    assert enrichment["metadata"]["enrichment_type"] == "llm"
    assert enrichment["metadata"]["technologies"] == ["python"]

def test_llm_enrichment_failover_and_fallback():
    def mock_fail(*args, **kwargs):
        raise RuntimeError("Network timeout")
        
    with patch("providers.get_routing_client", side_effect=mock_fail):
        enrichment = chunk_pipeline.enrich_chunk_with_llm("Some sample conversation text.")
        
    assert enrichment["metadata"]["enrichment_type"] == "fallback"
    assert len(enrichment["keywords"]) > 0

def test_process_conversation_to_chunks_saves_to_db():
    messages = [
        {"role": "user", "content": "I prefer Python. What do you think?", "timestamp": 1000},
        {"role": "assistant", "content": "Python is excellent for backend.", "timestamp": 1010}
    ]
    
    mock_enrichment = {
        "caveman_text": "user prefer python. python excellent backend.",
        "keywords": ["python", "backend"],
        "metadata": {
            "workspace": None,
            "project": None,
            "skill": None,
            "annotation": None,
            "enrichment_type": "mocked"
        }
    }
    
    with patch("chunk_pipeline.enrich_chunk_with_llm", return_value=mock_enrichment):
        chunk_ids = chunk_pipeline.process_conversation_to_chunks(messages)
        
    assert len(chunk_ids) == 1
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, metadata FROM chunks WHERE chunk_id = ?", (chunk_ids[0],))
        row = cursor.fetchone()
        assert row is not None
        seq, tier, raw, cave, start, end, meta_json = row
        assert seq == 1
        assert tier == "unclassified"
        assert "User: I prefer Python. What do you think?" in raw
        assert cave == "user prefer python. python excellent backend."
        assert start == 1000
        assert end == 1010
        
        meta = json.loads(meta_json)
        assert meta["enrichment_type"] == "mocked"
        
        cursor.execute("SELECT keyword FROM chunk_keywords WHERE chunk_id = ? ORDER BY keyword ASC", (chunk_ids[0],))
        kws = [r[0] for r in cursor.fetchall()]
        assert kws == ["backend", "python"]
    finally:
        conn.close()
