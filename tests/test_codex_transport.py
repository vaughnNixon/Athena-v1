import pytest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import codex_transport

def test_responses_tools_conversion():
    input_tools = [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "Run shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"}
                    },
                    "required": ["cmd"]
                }
            }
        }
    ]
    converted = codex_transport.responses_tools(input_tools)
    assert len(converted) == 1
    assert converted[0]["type"] == "function"
    assert converted[0]["name"] == "exec_command"
    assert converted[0]["description"] == "Run shell command"
    assert converted[0]["strict"] is False
    assert "cmd" in converted[0]["parameters"]["properties"]

def test_chat_messages_to_responses_input_conversion():
    messages = [
        {"role": "system", "content": "You are a caveman."},
        {"role": "user", "content": "hello athena"},
        {
            "role": "assistant",
            "content": "Me Athena.",
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "xyzabc", "_issuer_kind": "codex_backend"}
            ],
            "codex_message_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Me Athena."}]
                }
            ]
        },
        {"role": "tool", "tool_call_id": "call_123", "content": "tool success"}
    ]

    # Convert with matching issuer
    converted = codex_transport.chat_messages_to_responses_input(
        messages,
        current_issuer_kind="codex_backend"
    )

    # 1. System message should be ignored
    # 2. User message should be {"role": "user", "content": "hello athena"}
    # 3. Assistant message should replay the reasoning items and message items
    # 4. Tool output should be converted to function_call_output
    assert len(converted) == 4
    
    assert converted[0] == {"role": "user", "content": "hello athena"}
    
    # Check reasoning replay (note: id and _issuer_kind are stripped)
    assert converted[1]["type"] == "reasoning"
    assert converted[1]["encrypted_content"] == "xyzabc"
    assert "id" not in converted[1]
    assert "_issuer_kind" not in converted[1]
    
    # Check message item replay
    assert converted[2]["type"] == "message"
    assert converted[2]["role"] == "assistant"
    assert converted[2]["content"] == [{"type": "output_text", "text": "Me Athena."}]
    
    # Check tool output conversion
    assert converted[3]["type"] == "function_call_output"
    assert converted[3]["call_id"] == "call_123"
    assert converted[3]["output"] == "tool success"

def test_chat_messages_to_responses_input_cross_issuer():
    messages = [
        {
            "role": "assistant",
            "content": "hello",
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "xyzabc", "_issuer_kind": "xai_responses"}
            ]
        }
    ]

    # If issuer doesn't match, the reasoning item should be dropped
    converted = codex_transport.chat_messages_to_responses_input(
        messages,
        current_issuer_kind="codex_backend"
    )
    # The reasoning item is dropped, leaving only the standard assistant message
    assert len(converted) == 1
    assert converted[0] == {"role": "assistant", "content": "hello"}

@patch("codex_transport.codex_responses_call")
def test_codex_client_wrapper(mock_responses_call):
    # Setup mock return values
    mock_msg = codex_transport.AssistantMessage(
        content="Hello!",
        tool_calls=[],
        reasoning="thinking...",
        codex_reasoning_items=[{"type": "reasoning", "encrypted_content": "123"}],
        codex_message_items=[]
    )
    mock_responses_call.return_value = (mock_msg, "stop")

    client = codex_transport.CodexClient("fake_token", "fake_account")
    response = client.chat.completions.create(
        model="gpt-5.5",
        messages=[
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "hi"}
        ]
    )

    # Verify parameters sent to codex_responses_call
    mock_responses_call.assert_called_once()
    called_kwargs = mock_responses_call.call_args[1]
    assert called_kwargs["access_token"] == "fake_token"
    assert called_kwargs["account_id"] == "fake_account"
    assert called_kwargs["model"] == "gpt-5.5"
    assert called_kwargs["instructions"] == "sys prompt"
    assert len(called_kwargs["input_items"]) == 1
    assert called_kwargs["input_items"][0] == {"role": "user", "content": "hi"}

    # Verify mock response matches standard openai SDK shapes
    assert len(response.choices) == 1
    choice = response.choices[0]
    assert choice.finish_reason == "stop"
    assert choice.message.content == "Hello!"
    assert choice.message.reasoning == "thinking..."
    assert choice.message.codex_reasoning_items == [{"type": "reasoning", "encrypted_content": "123"}]

def test_consume_raw_sse_stream():
    stream_lines = [
        b"event: response.output_text.delta",
        b'data: {"delta": "Hello "}',
        b"event: response.output_text.delta",
        b'data: {"delta": "world!"}',
        b"event: response.completed",
        b'data: {"response": {"id": "resp_999", "status": "completed", "usage": {"total_tokens": 15}}}',
        b"data: [DONE]"
    ]
    res = codex_transport._consume_raw_sse_stream(stream_lines, "gpt-5.5")
    assert res["output_text"] == "Hello world!"
    assert res["status"] == "completed"
    assert res["id"] == "resp_999"
    assert res["usage"] == {"total_tokens": 15}
    assert len(res["output"]) == 1
    assert res["output"][0]["content"][0]["text"] == "Hello world!"
