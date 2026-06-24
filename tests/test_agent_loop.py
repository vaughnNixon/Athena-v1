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

def test_agent_tool_calling_memory_retrieval():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_tool")
    
    mock_client = MagicMock()
    
    # First response choice (with tool calls)
    mock_tc = MagicMock()
    mock_tc.id = "call_abc123"
    mock_tc.type = "function"
    mock_tc.function.name = "retrieve_memories"
    mock_tc.function.arguments = '{"query": "favorite programming language"}'
    
    mock_msg1 = MagicMock()
    mock_msg1.content = None
    mock_msg1.tool_calls = [mock_tc]
    
    mock_choice1 = MagicMock()
    mock_choice1.message = mock_msg1
    mock_res1 = MagicMock()
    mock_res1.choices = [mock_choice1]
    
    # Second response choice (final answer)
    mock_msg2 = MagicMock()
    mock_msg2.content = "Your favorite language is Python."
    mock_msg2.tool_calls = None
    
    mock_choice2 = MagicMock()
    mock_choice2.message = mock_msg2
    mock_res2 = MagicMock()
    mock_res2.choices = [mock_choice2]
    
    # Mock completions create calls sequentially
    mock_client.chat.completions.create.side_effect = [mock_res1, mock_res2]
    
    memory_engine.insert_or_reinforce_fact("User's favorite programming language is Python.", category="preference", importance=8, confidence=0.9, scope_ids=["test_proj"])
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation") as mock_enqueue:
            res = agent.run_one_turn("do you remember my favorite language?")
            assert res == "Your favorite language is Python."
            
            # Verify completions was called twice
            assert mock_client.chat.completions.create.call_count == 2
            
            # Check arguments of the second completions call
            call_args_list = mock_client.chat.completions.create.call_args_list
            second_call_kwargs = call_args_list[1][1]
            second_call_messages = second_call_kwargs["messages"]
            
            # The second call must contain:
            # - User message
            # - Assistant tool call message
            # - Tool response message containing retrieved memory
            assert second_call_messages[-3]["role"] == "user"
            assert second_call_messages[-2]["role"] == "assistant"
            assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == "retrieve_memories"
            assert second_call_messages[-1]["role"] == "tool"
            assert "favorite programming language is Python" in second_call_messages[-1]["content"]
            
            # Verify history persists only clean turns without raw tool calls/responses
            assert len(agent.history) == 2
            assert agent.history[0]["role"] == "user"
            assert agent.history[0]["content"] == "do you remember my favorite language?"
            assert agent.history[1]["role"] == "assistant"
            assert agent.history[1]["content"] == "Your favorite language is Python."

