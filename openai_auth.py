import os
import json
import time
import logging
import hashlib
import secrets
import base64
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
import config

logger = logging.getLogger("athena.openai_auth")

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
PORT = 1455

HTML_SUCCESS = """<!doctype html>
<html>
  <head>
    <title>OpenCode - Codex Authorization Successful</title>
    <style>
      body {
        font-family: system-ui, -apple-system, sans-serif;
        display: flex;
        justify-content: center;
        align-items: center;
        height: 100vh;
        margin: 0;
        background: #131010;
        color: #f1ecec;
      }
      .container {
        text-align: center;
        padding: 2rem;
      }
      h1 {
        color: #f1ecec;
        margin-bottom: 1rem;
      }
      p {
        color: #b7b1b1;
      }
    </style>
  </head>
  <body>
    <div class="container">
      <h1>Authorization Successful</h1>
      <p>You can close this window and return to Athena.</p>
    </div>
    <script>
      setTimeout(() => window.close(), 2000)
    </script>
  </body>
</html>"""

HTML_ERROR = """<!doctype html>
<html>
  <head>
    <title>OpenCode - Codex Authorization Failed</title>
    <style>
      body {
        font-family: system-ui, -apple-system, sans-serif;
        display: flex;
        justify-content: center;
        align-items: center;
        height: 100vh;
        margin: 0;
        background: #131010;
        color: #f1ecec;
      }
      .container {
        text-align: center;
        padding: 2rem;
      }
      h1 {
        color: #fc533a;
        margin-bottom: 1rem;
      }
      p {
        color: #b7b1b1;
      }
      .error {{
        color: #ff917b;
        font-family: monospace;
        margin-top: 1rem;
        padding: 1rem;
        background: #3c140d;
        border-radius: 0.5rem;
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <h1>Authorization Failed</h1>
      <p>An error occurred during authorization.</p>
      <div class="error">{error}</div>
    </div>
  </body>
</html>"""

def generate_pkce() -> tuple[str, str]:
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(chars) for _ in range(43))
    sha256_hash = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(sha256_hash).decode("utf-8").rstrip("=")
    return verifier, challenge

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        query = urllib.parse.parse_qs(parsed_url.query)
        error = query.get("error_description", query.get("error", [None]))[0]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]

        if error:
            self.server.oauth_error = error
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_ERROR.format(error=error).encode("utf-8"))
            return

        if not code or state != self.server.expected_state:
            err_msg = "Missing authorization code" if not code else "Invalid state - potential CSRF attack"
            self.server.oauth_error = err_msg
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_ERROR.format(error=err_msg).encode("utf-8"))
            return

        self.server.oauth_code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML_SUCCESS.encode("utf-8"))

def run_callback_server(expected_state: str, port: int = 1455) -> tuple[str, str]:
    server = HTTPServer(("localhost", port), OAuthCallbackHandler)
    server.expected_state = expected_state
    server.oauth_code = None
    server.oauth_error = None
    server.timeout = 1.0

    print(f"  Waiting for browser callback on port {port}...")
    start_time = time.monotonic()
    timeout = 300  # 5 minutes
    
    while time.monotonic() - start_time < timeout:
        server.handle_request()
        if server.oauth_code or server.oauth_error:
            break

    server.server_close()
    if server.oauth_code:
        return server.oauth_code, None
    elif server.oauth_error:
        return None, server.oauth_error
    else:
        return None, "OAuth authorization timed out."

def build_authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "opencode"
    }
    return f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(params)}"

def exchange_code_for_tokens(code: str, redirect_uri: str, verifier: str) -> dict:
    url = f"{ISSUER}/oauth/token"
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": verifier
    }).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "opencode/1.0.0"
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def parse_jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1]
        missing_padding = len(payload) % 4
        if missing_padding:
            payload += "=" * (4 - missing_padding)
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}

def extract_account_id(tokens: dict) -> str:
    id_token = tokens.get("id_token")
    if id_token:
        claims = parse_jwt_claims(id_token)
        account_id = (
            claims.get("chatgpt_account_id") or
            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or
            (claims.get("organizations", [{}])[0].get("id") if claims.get("organizations") else None)
        )
        if account_id:
            return account_id
            
    access_token = tokens.get("access_token")
    if access_token:
        claims = parse_jwt_claims(access_token)
        account_id = (
            claims.get("chatgpt_account_id") or
            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or
            (claims.get("organizations", [{}])[0].get("id") if claims.get("organizations") else None)
        )
        if account_id:
            return account_id
            
    return ""

def initiate_headless_flow() -> dict:
    url = f"{ISSUER}/api/accounts/deviceauth/usercode"
    req = urllib.request.Request(
        url,
        data=json.dumps({"client_id": CLIENT_ID}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "opencode/1.0.0"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def poll_headless_token(device_auth_id: str, user_code: str, interval: int = 5) -> dict:
    import time
    url = f"{ISSUER}/api/accounts/deviceauth/token"
    poll_data = json.dumps({
        "device_auth_id": device_auth_id,
        "user_code": user_code
    }).encode("utf-8")
    
    while True:
        req = urllib.request.Request(
            url,
            data=poll_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "opencode/1.0.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code in (403, 404):
                pass
            else:
                raise ValueError(f"Device authorization failed: {err.code}") from err
        except Exception as exc:
            raise ValueError(f"Connection error during polling: {exc}") from exc
            
        time.sleep(interval + 3)

def refresh_access_token(refresh_token: str) -> dict:
    url = f"{ISSUER}/oauth/token"
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID
    }).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "opencode/1.0.0"
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def save_chatgpt_credentials(access_token: str, refresh_token: str, expires_in: int, account_id: str):
    expires_at = int(time.time() + expires_in)
    env_file = config.get_athena_home() / ".env"
    
    env_lines = []
    if env_file.exists():
        env_lines = env_file.read_text(encoding="utf-8").splitlines()
        
    keys_to_remove = {
        "CHATGPT_ACCESS_TOKEN",
        "CHATGPT_REFRESH_TOKEN",
        "CHATGPT_EXPIRES_AT",
        "CHATGPT_ACCOUNT_ID"
    }
    env_lines = [
        line for line in env_lines
        if not any(line.strip().startswith(f"{key}=") for key in keys_to_remove)
    ]
    
    env_lines.append(f"CHATGPT_ACCESS_TOKEN={access_token}")
    env_lines.append(f"CHATGPT_REFRESH_TOKEN={refresh_token}")
    env_lines.append(f"CHATGPT_EXPIRES_AT={expires_at}")
    if account_id:
        env_lines.append(f"CHATGPT_ACCOUNT_ID={account_id}")
        
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

def load_chatgpt_credentials() -> dict:
    env = config.load_env()
    expires_at_val = env.get("CHATGPT_EXPIRES_AT", "0")
    try:
        expires_at = int(expires_at_val) if expires_at_val else 0
    except ValueError:
        expires_at = 0
        
    return {
        "access_token": env.get("CHATGPT_ACCESS_TOKEN", ""),
        "refresh_token": env.get("CHATGPT_REFRESH_TOKEN", ""),
        "expires_at": expires_at,
        "account_id": env.get("CHATGPT_ACCOUNT_ID", "")
    }

def get_chatgpt_access_token(force_refresh: bool = False) -> tuple[str, str]:
    creds = load_chatgpt_credentials()
    if not creds["refresh_token"]:
        return "", ""
        
    if force_refresh or not creds["access_token"] or time.time() >= creds["expires_at"] - 120:
        try:
            tokens = refresh_access_token(creds["refresh_token"])
            access = tokens["access_token"]
            refresh = tokens["refresh_token"]
            expires_in = tokens.get("expires_in", 3600)
            account_id = extract_account_id(tokens) or creds["account_id"]
            
            save_chatgpt_credentials(access, refresh, expires_in, account_id)
            return access, account_id
        except Exception as exc:
            logger.warning("Failed to refresh ChatGPT access token: %s", exc)
            return creds["access_token"], creds["account_id"]
            
    return creds["access_token"], creds["account_id"]


