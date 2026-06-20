import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
from agent_loop import AthenaAgent

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

def test_caveman_mode_toggle_prompt_construction():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess")
    
    # By default, caveman_mode should be False
    assert agent.caveman_mode is False
    
    # Mock routing client and completions
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "ACK."
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    # Toggle caveman_mode to True
    agent.caveman_mode = True
    
    # 1. Run turn with caveman_mode = True
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation") as mock_enqueue:
            res = agent.run_one_turn("hello")
            assert res == "ACK."
            
            # Check what messages were sent to LLM
            called_args = mock_client.chat.completions.create.call_args[1]
            messages = called_args["messages"]
            system_prompt = messages[0]["content"]
            assert "terse, telegraphic, sparse prose (Caveman style)" in system_prompt
            assert "Remove all conversational fillers" in system_prompt

    # 2. Toggle caveman_mode to False
    agent.caveman_mode = False
    
    # Run turn with caveman_mode = False
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation") as mock_enqueue:
            res = agent.run_one_turn("how are you?")
            assert res == "ACK."
            
            # Check what messages were sent to LLM
            called_args = mock_client.chat.completions.create.call_args[1]
            messages = called_args["messages"]
            system_prompt = messages[0]["content"]
            # Should NOT contain caveman instructions
            assert "terse, telegraphic, sparse prose (Caveman style)" not in system_prompt
            assert "Remove all conversational fillers" not in system_prompt
            # Should still contain base memory prompt
            assert "Athena remembers what others forget" in system_prompt
