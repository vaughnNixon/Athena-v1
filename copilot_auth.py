import os
import json
import logging
import time
import subprocess
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("athena.copilot_auth")

COPILOT_OAUTH_CLIENT_ID = "Ov23li8tweQw6odWQebz"
_CLASSIC_PAT_PREFIX = "ghp_"
_SUPPORTED_PREFIXES = ("gho_", "github_pat_", "ghu_")
COPILOT_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

_DEVICE_CODE_POLL_INTERVAL = 5
_DEVICE_CODE_POLL_SAFETY_MARGIN = 3
_jwt_cache: dict[str, tuple[str, float]] = {}
_JWT_REFRESH_MARGIN_SECONDS = 120
_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
_EDITOR_VERSION = "vscode/1.104.1"
_EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"

def validate_copilot_token(token: str) -> tuple[bool, str]:
    token = token.strip()
    if not token:
        return False, "Empty token"
    if token.startswith(_CLASSIC_PAT_PREFIX):
        return False, "Classic Personal Access Tokens (ghp_*) are not supported. Use gho_* (OAuth) or github_pat_* (fine-grained PAT with Copilot Requests permission)."
    return True, "OK"

def _gh_cli_candidates() -> list[str]:
    candidates = []
    resolved = shutil.which("gh")
    if resolved:
        candidates.append(resolved)
    for candidate in (
        "/opt/homebrew/bin/gh",
        "/usr/local/bin/gh",
        str(Path.home() / ".local" / "bin" / "gh"),
    ):
        if candidate in candidates:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates

def _try_gh_cli_token() -> Optional[str]:
    hostname = os.getenv("COPILOT_GH_HOST", "").strip()
    clean_env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    for gh_path in _gh_cli_candidates():
        cmd = [gh_path, "auth", "token"]
        if hostname:
            cmd += ["--hostname", hostname]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=clean_env,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as exc:
            logger.debug("gh CLI token lookup failed (%s): %s", gh_path, exc)
            continue
    return None

def resolve_copilot_token() -> tuple[str, str]:
    # 1. Check env vars
    for env_var in COPILOT_ENV_VARS:
        val = os.getenv(env_var, "").strip()
        if val:
            valid, msg = validate_copilot_token(val)
            if valid:
                return val, env_var
            else:
                logger.warning("Token from %s is not supported: %s", env_var, msg)
                
    # 2. Check gh CLI
    token = _try_gh_cli_token()
    if token:
        valid, msg = validate_copilot_token(token)
        if valid:
            return token, "gh auth token"
            
    return "", ""

def copilot_device_code_login(*, host: str = "github.com", timeout_seconds: float = 300) -> Optional[str]:
    import urllib.parse
    domain = host.rstrip("/")
    device_code_url = f"https://{domain}/login/device/code"
    access_token_url = f"https://{domain}/login/oauth/access_token"

    data = urllib.parse.urlencode({
        "client_id": COPILOT_OAUTH_CLIENT_ID,
        "scope": "read:user",
    }).encode()

    req = urllib.request.Request(
        device_code_url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "AthenaAgent/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            device_data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  ✗ Failed to start device authorization: {exc}")
        return None

    verification_uri = device_data.get("verification_uri", "https://github.com/login/device")
    user_code = device_data.get("user_code", "")
    device_code = device_data.get("device_code", "")
    interval = max(device_data.get("interval", _DEVICE_CODE_POLL_INTERVAL), 1)

    if not device_code or not user_code:
        print("  ✗ GitHub did not return a device code.")
        return None

    print()
    print(f"  Open this URL in your browser: {verification_uri}")
    print(f"  Enter this code: {user_code}")
    print()
    print("  Waiting for authorization...", end="", flush=True)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(interval + _DEVICE_CODE_POLL_SAFETY_MARGIN)

        poll_data = urllib.parse.urlencode({
            "client_id": COPILOT_OAUTH_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()

        poll_req = urllib.request.Request(
            access_token_url,
            data=poll_data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "AthenaAgent/1.0",
            },
        )

        try:
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
        except Exception:
            print(".", end="", flush=True)
            continue

        if result.get("access_token"):
            print(" ✓")
            return result["access_token"]

        error = result.get("error", "")
        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        elif error == "slow_down":
            server_interval = result.get("interval")
            if isinstance(server_interval, (int, float)) and server_interval > 0:
                interval = int(server_interval)
            else:
                interval += 5
            print(".", end="", flush=True)
            continue
        elif error == "expired_token":
            print("\n  ✗ Device code expired. Please try again.")
            return None
        elif error == "access_denied":
            print("\n  ✗ Authorization was denied.")
            return None
        elif error:
            print(f"\n  ✗ Authorization failed: {error}")
            return None

    print("\n  ✗ Timed out waiting for authorization.")
    return None

def _token_fingerprint(raw_token: str) -> str:
    import hashlib
    return hashlib.sha256(raw_token.encode()).hexdigest()[:16]

def exchange_copilot_token(raw_token: str, *, timeout: float = 10.0) -> tuple[str, float]:
    import urllib.request
    fp = _token_fingerprint(raw_token)
    cached = _jwt_cache.get(fp)
    if cached:
        api_token, expires_at = cached
        if time.time() < expires_at - _JWT_REFRESH_MARGIN_SECONDS:
            return api_token, expires_at

    req = urllib.request.Request(
        _TOKEN_EXCHANGE_URL,
        method="GET",
        headers={
            "Authorization": f"token {raw_token}",
            "User-Agent": _EXCHANGE_USER_AGENT,
            "Accept": "application/json",
            "Editor-Version": _EDITOR_VERSION,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise ValueError(f"Copilot token exchange failed: {exc}") from exc

    api_token = data.get("token", "")
    expires_at = data.get("expires_at", 0)
    if not api_token:
        raise ValueError("Copilot token exchange returned empty token")

    expires_at = float(expires_at) if expires_at else time.time() + 1800
    _jwt_cache[fp] = (api_token, expires_at)
    return api_token, expires_at

def get_copilot_api_token(raw_token: str) -> str:
    if not raw_token:
        return raw_token
    try:
        api_token, _ = exchange_copilot_token(raw_token)
        return api_token
    except Exception as exc:
        logger.debug("Copilot token exchange failed, using raw token: %s", exc)
        return raw_token

def copilot_request_headers(*, is_agent_turn: bool = True, is_vision: bool = False) -> dict[str, str]:
    headers = {
        "Editor-Version": "vscode/1.104.1",
        "User-Agent": "AthenaAgent/1.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Intent": "conversation-edits",
        "x-initiator": "agent" if is_agent_turn else "user",
    }
    if is_vision:
        headers["Copilot-Vision-Request"] = "true"
    return headers
