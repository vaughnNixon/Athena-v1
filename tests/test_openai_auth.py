import pytest
import os
import time
import json
import base64
from unittest.mock import MagicMock, patch
import httpx

import openai_auth

def test_generate_pkce():
    verifier, challenge = openai_auth.generate_pkce()
    assert len(verifier) == 43
    assert len(challenge) > 0
    # The verifier should only contain permitted characters
    allowed_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert all(c in allowed_chars for c in verifier)

def test_parse_jwt_claims():
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("utf-8").rstrip("=")
    claims = {
        "chatgpt_account_id": "acc_123",
        "email": "test@example.com"
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("utf-8").rstrip("=")
    signature = "signature"
    token = f"{header}.{payload}.{signature}"
    
    parsed = openai_auth.parse_jwt_claims(token)
    assert parsed["chatgpt_account_id"] == "acc_123"
    assert parsed["email"] == "test@example.com"

def test_extract_account_id():
    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc_456"
        }
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("utf-8").rstrip("=")
    token = f"header.{payload}.sig"
    
    tokens = {"id_token": token}
    account_id = openai_auth.extract_account_id(tokens)
    assert account_id == "acc_456"



@patch("urllib.request.urlopen")
def test_exchange_code_for_tokens(mock_urlopen):
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"access_token": "acc_tok", "refresh_token": "ref_tok", "expires_in": 3600}'
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    res = openai_auth.exchange_code_for_tokens("test_code", "test_uri", "test_verifier")
    assert res["access_token"] == "acc_tok"
    assert res["refresh_token"] == "ref_tok"

@patch("urllib.request.urlopen")
def test_initiate_headless_flow(mock_urlopen):
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"device_auth_id": "id123", "user_code": "code456", "interval": "5"}'
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    res = openai_auth.initiate_headless_flow()
    assert res["device_auth_id"] == "id123"
    assert res["user_code"] == "code456"
