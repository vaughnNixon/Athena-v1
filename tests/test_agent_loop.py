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
            assert "Use this tool when you need context" in system_prompt

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
            assert "Use this tool when you need context" not in system_prompt
            # Should still contain base memory prompt
            assert "Athena remembers what others forget" in system_prompt

def test_retrieve_memories_tool_definition():
    from agent_loop import get_retrieve_memories_tool_definition
    tool = get_retrieve_memories_tool_definition()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "retrieve_memories"
    assert "query" in tool["function"]["parameters"]["properties"]
    assert tool["function"]["description"]

def test_run_one_turn_without_tool_call():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_notool")
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Hello there!"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("hello")
            assert "hello" in res.lower() or "there" in res.lower()
            # Verify completions create was called once
            assert mock_client.chat.completions.create.call_count == 1

def test_run_one_turn_with_tool_call():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_toolcall")
    
    # Store a fact
    memory_engine.insert_or_reinforce_fact("Dental clinic: Grace Dental, Kochi", category="projects", importance=8, confidence=1.0, scope_ids=["test_proj"])
    
    mock_client = MagicMock()
    
    # First response with tool call
    mock_tc = MagicMock()
    mock_tc.id = "call_xyz"
    mock_tc.type = "function"
    mock_tc.function.name = "retrieve_memories"
    mock_tc.function.arguments = '{"query": "dental clinic"}'
    
    mock_msg1 = MagicMock()
    mock_msg1.content = None
    mock_msg1.tool_calls = [mock_tc]
    mock_choice1 = MagicMock()
    mock_choice1.message = mock_msg1
    mock_res1 = MagicMock()
    mock_res1.choices = [mock_choice1]
    
    # Second response (final answer)
    mock_msg2 = MagicMock()
    mock_msg2.content = "Here are the details for Grace Dental clinic in Kochi."
    mock_msg2.tool_calls = None
    mock_choice2 = MagicMock()
    mock_choice2.message = mock_msg2
    mock_res2 = MagicMock()
    mock_res2.choices = [mock_choice2]
    
    mock_client.chat.completions.create.side_effect = [mock_res1, mock_res2]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("tell me about the dental clinic project")
            assert "Grace Dental" in res or "dental" in res.lower()
            assert mock_client.chat.completions.create.call_count == 2

def test_tool_call_fallover_on_api_failure():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_failover")
    
    mock_client_fail = MagicMock()
    mock_client_fail.chat.completions.create.side_effect = Exception("API rate limit exceeded")
    
    mock_client_ok = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Successful response"
    mock_choice.message.tool_calls = None
    mock_client_ok.chat.completions.create.return_value.choices = [mock_choice]
    
    call_idx = 0
    def side_effect_routing(*args, **kwargs):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return (mock_client_fail, "model-fail", "provider-fail")
        else:
            return (mock_client_ok, "model-ok", "provider-ok")
            
    with patch("providers.get_routing_client", side_effect=side_effect_routing):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("hello")
            assert res == "Successful response"

def test_tool_unsupported_fallback():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_fallback")
    
    # Pre-populate memory
    memory_engine.insert_or_reinforce_fact("Secret code is 12345.", category="secret", importance=10, confidence=1.0, scope_ids=["test_proj"])
    
    mock_client = MagicMock()
    
    mock_choice = MagicMock()
    mock_choice.message.content = "The secret code is 12345."
    mock_choice.message.tool_calls = None
    mock_res = MagicMock()
    mock_res.choices = [mock_choice]
    
    def completions_side_effect(*args, **kwargs):
        if "tools" in kwargs:
            raise Exception("InvalidRequestError: tools is not supported by this model")
        return mock_res
        
    mock_client.chat.completions.create.side_effect = completions_side_effect
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("what is the secret code?")
            assert "12345" in res
            
            # Verify the second call received the fallback memories
            call_args_list = mock_client.chat.completions.create.call_args_list
            assert len(call_args_list) == 2
            
            # Check the second call messages
            second_call_kwargs = call_args_list[1][1]
            assert "tools" not in second_call_kwargs
            
            second_call_messages = second_call_kwargs["messages"]
            system_prompt = second_call_messages[0]["content"]
            assert "[ATHENA FALLBACK MEMORY]" in system_prompt
            assert "Secret code is 12345." in system_prompt

def test_end_to_end_conversation_with_memory():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_e2e")
    
    mock_client = MagicMock()
    mock_choice1 = MagicMock()
    mock_choice1.message.content = "I noted that your favorite color is blue."
    mock_choice1.message.tool_calls = None
    mock_res1 = MagicMock()
    mock_res1.choices = [mock_choice1]
    
    mock_client.chat.completions.create.return_value = mock_res1
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("My favorite color is blue.")
            assert "blue" in res.lower()

def test_latency_comparison():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_latency")
    
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Response"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    import time
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            start = time.time()
            agent.run_one_turn("hello")
            latency = time.time() - start
            assert latency < 3.0


def test_presentation_style_toggle_continuity():
    agent = AthenaAgent(project_id="test_proj", session_id="test_sess_continuity")
    
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Normal response 1"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    # 1. First turn - Normal Mode (caveman OFF)
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("User message 1")
            assert res == "Normal response 1"
            
            # Check style toggle instruction in system prompt
            called_args = mock_client.chat.completions.create.call_args[1]
            sys_prompt = called_args["messages"][0]["content"]
            assert "Treat them as factual summaries only. Continue the same conversation naturally" in sys_prompt

    # 2. Toggle to Caveman Mode ON, run second turn
    agent.caveman_mode = True
    mock_choice.message.content = "ACK."
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("User message 2")
            assert res == "ACK."
            
            # Check style toggle instruction and history continuity
            called_args = mock_client.chat.completions.create.call_args[1]
            messages = called_args["messages"]
            sys_prompt = messages[0]["content"]
            
            assert "Ignore previous writing style. Continue using the same facts" in sys_prompt
            # History MUST contain the normal turn!
            # messages should be: [system, User message 1, Normal response 1, User message 2]
            assert len(messages) == 4
            assert messages[1]["role"] == "user"
            assert messages[1]["content"] == "User message 1"
            assert messages[2]["role"] == "assistant"
            assert messages[2]["content"] == "Normal response 1"
            assert messages[3]["role"] == "user"
            assert messages[3]["content"] == "User message 2"

    # 3. Toggle to Caveman Mode OFF, run third turn
    agent.caveman_mode = False
    mock_choice.message.content = "Normal response 3"
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        with patch("distillation.enqueue_distillation"):
            res = agent.run_one_turn("User message 3")
            assert res == "Normal response 3"
            
            # Check history continuity
            called_args = mock_client.chat.completions.create.call_args[1]
            messages = called_args["messages"]
            sys_prompt = messages[0]["content"]
            
            assert "Treat them as factual summaries only. Continue the same conversation naturally" in sys_prompt
            # History MUST contain all turns, including the caveman turn!
            # messages should be: [system, User message 1, Normal response 1, User message 2, ACK., User message 3]
            assert len(messages) == 6
            assert messages[1]["content"] == "User message 1"
            assert messages[2]["content"] == "Normal response 1"
            assert messages[3]["content"] == "User message 2"
            assert messages[4]["content"] == "ACK."
            assert messages[5]["content"] == "User message 3"






