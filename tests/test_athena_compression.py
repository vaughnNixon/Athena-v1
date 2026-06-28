import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import athena_compression
import memory_engine

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    # Cleanup
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_compress_history_via_headroom_fallback():
    # Force _HEADROOM_AVAILABLE to False to test fallback code path
    with patch("athena_compression._HEADROOM_AVAILABLE", False):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "{\"key\": \"" + "val" * 800 + "\"}"}, # long tool message
            {"role": "assistant", "content": "response"}
        ]
        
        compressed = athena_compression.compress_history_via_headroom(messages)
        assert compressed[0] == messages[0]
        assert compressed[2] == messages[2]
        assert len(compressed[1]["content"]) <= 2050 # original ~1500 chars, collapsed/truncated
        assert "truncated" in compressed[1]["content"]

def test_compress_history_via_headroom_real():
    # Test real headroom compression when available
    import json
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": json.dumps([{"key": "val", "index": i} for i in range(150)])}, # long tool JSON array
        {"role": "assistant", "content": "response"}
    ]
    
    compressed = athena_compression.compress_history_via_headroom(messages)
    assert compressed[0] == messages[0]
    assert compressed[2] == messages[2]
    # Check that it compressed (should be shorter than original)
    assert len(compressed[1]["content"]) < len(messages[1]["content"])

def test_run_caveman_summarization_noop_when_short():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"}
    ]
    res = athena_compression.run_caveman_summarization(messages, "test_proj")
    assert res == messages

def test_run_caveman_summarization_triggers():
    # Long message history (> 4000 chars)
    long_msg = "test word " * 500 # 5000 chars
    messages = [
        {"role": "user", "content": long_msg},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "tell me more"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "last turn"},
        {"role": "assistant", "content": "last response"}
    ]
    
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Telegraphic compression of details."
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        res = athena_compression.run_caveman_summarization(messages, "test_proj")
        
    # Should replace older turns with summary
    assert len(res) == 5 # 1 system summary + last 4 turns
    assert res[0]["role"] == "system"
    assert "Telegraphic compression of details." in res[0]["content"]
    assert res[1] == messages[-4]
    
    # Check that it also registered as a fact in memory engine
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT fact FROM facts WHERE category = 'history'")
    row = cursor.fetchone()
    assert row is not None
    assert "Telegraphic compression of details." in row[0]
    conn.close()
