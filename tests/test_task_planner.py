import pytest
import os
import tempfile
import json
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import task_planner

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

def test_deterministic_rules():
    # Test file_reader rule
    plan_res = task_planner.plan("read file path/to/file.txt")
    assert plan_res is not None
    assert plan_res["skill"] == "file_reader"
    assert "read file" in plan_res["task_description"]

    # Test web_search rule
    plan_res = task_planner.plan("search the web for python 3.14 features")
    assert plan_res is not None
    assert plan_res["skill"] == "web_search"

    # Test code_runner rule
    plan_res = task_planner.plan("run this code snippet")
    assert plan_res is not None
    assert plan_res["skill"] == "code_runner"

    # Test writer rule
    plan_res = task_planner.plan("write a report about the project")
    assert plan_res is not None
    assert plan_res["skill"] == "writer"

def test_ambiguous_and_conversational_llm_fallback():
    mock_client = MagicMock()
    mock_choice = MagicMock()
    
    # 1. Test ambiguous task
    mock_choice.message.content = json.dumps({
        "is_task": True,
        "skill": "code_runner",
        "task_description": "Run calculations on numbers"
    })
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "mock-model", "mock-provider")):
        plan_res = task_planner.plan("analyze the growth rate of customer base")
        assert plan_res is not None
        assert plan_res["skill"] == "code_runner"
        assert plan_res["task_description"] == "Run calculations on numbers"

    # 2. Test conversational query (should return None)
    mock_choice.message.content = json.dumps({
        "is_task": False,
        "skill": None,
        "task_description": None
    })
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "mock-model", "mock-provider")):
        plan_res = task_planner.plan("hello there! how's it going?")
        assert plan_res is None

def test_prior_outcome_extraction():
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chunks (sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, char_count, token_estimate, metadata, created_at, updated_at)
                VALUES (1, 'active', 'Subagent writer resolved goal: write a doc. Finding: outcome: success, draft is ready.', 'resolved write a doc success', 0, 0, 50, 10, '{}', 0, 0)
            """)
            chunk_id = cursor.lastrowid
            cursor.execute("INSERT INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)", (chunk_id, "write"))
            cursor.execute("INSERT INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)", (chunk_id, "doc"))
    finally:
        conn.close()
    
    # Run plan and check prior_outcome
    plan_res = task_planner.plan("write a doc", project_id="test_proj")
    assert plan_res is not None
    assert plan_res["prior_outcome"] == "success"
