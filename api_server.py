"""
api_server.py -- Athena Mobile & Remote Control API Server
Provides HTTP REST API endpoints and a sleek mobile-friendly web UI
allowing full remote control of Athena from mobile devices or other web clients.
"""

import json
import logging
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Optional

import config
import memory_engine
from agent_loop import AthenaAgent
from providers_manager import get_manager

logger = logging.getLogger("athena.api_server")

_agent_cache = {}

def get_agent_instance(project_id: str = "default", session_id: str = "session_1") -> AthenaAgent:
    key = (project_id, session_id)
    if key not in _agent_cache:
        config.ensure_athena_dirs()
        memory_engine.initialize_db()
        _agent_cache[key] = AthenaAgent(project_id=project_id, session_id=session_id)
    return _agent_cache[key]


MOBILE_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Athena Mobile Control</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        body { background-color: #0d0d0d; color: #e5e7eb; display: flex; flex-direction: column; height: 100vh; max-width: 600px; margin: 0 auto; }
        header { background: #141414; padding: 12px 16px; border-bottom: 1px solid #262626; display: flex; justify-content: space-between; align-items: center; }
        header h1 { font-size: 18px; color: #d4a843; font-weight: 700; }
        header .status { font-size: 12px; color: #a3a3a3; background: #262626; padding: 4px 8px; border-radius: 12px; }
        #chat-log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
        .msg { max-width: 85%; padding: 10px 14px; border-radius: 12px; font-size: 15px; line-height: 1.4; word-wrap: break-word; }
        .msg.user { align-self: flex-end; background: #22c55e; color: #000; font-weight: 500; border-bottom-right-radius: 2px; }
        .msg.athena { align-self: flex-start; background: #1c1917; color: #e7e5e4; border: 1px solid #292524; border-bottom-left-radius: 2px; }
        .msg.athena strong { color: #d4a843; }
        .msg.system { align-self: center; background: #172554; color: #93c5fd; font-size: 13px; border-radius: 6px; }
        #input-area { background: #141414; padding: 12px 16px; border-top: 1px solid #262626; display: flex; gap: 8px; }
        input[type="text"] { flex: 1; background: #0a0a0a; border: 1px solid #333; color: #fff; padding: 12px; border-radius: 8px; font-size: 16px; outline: none; }
        input[type="text"]:focus { border-color: #d4a843; }
        button { background: #d4a843; color: #000; border: none; padding: 0 16px; border-radius: 8px; font-weight: 600; font-size: 15px; cursor: pointer; }
        button:disabled { opacity: 0.5; }
        .quick-actions { display: flex; gap: 8px; padding: 8px 16px; background: #0d0d0d; overflow-x: auto; border-top: 1px solid #1f1f1f; }
        .chip { background: #1a1a1a; color: #d4a843; border: 1px solid #333; padding: 6px 12px; border-radius: 16px; font-size: 12px; white-space: nowrap; cursor: pointer; }
    </style>
</head>
<body>
    <header>
        <h1>ATHENA MOBILE</h1>
        <div class="status" id="prov-status">Auto . Groq</div>
    </header>
    <div id="chat-log">
        <div class="msg system">Athena Remote Mobile Control Connected.</div>
    </div>
    <div class="quick-actions">
        <div class="chip" onclick="sendCmd('/topics')">Topics</div>
        <div class="chip" onclick="sendCmd('/providers')">Providers</div>
        <div class="chip" onclick="sendCmd('/caveman')">Caveman</div>
        <div class="chip" onclick="sendCmd('/newchat')">New Chat</div>
    </div>
    <div id="input-area">
        <input type="text" id="user-input" placeholder="Ask Athena anything..." onkeydown="if(event.key==='Enter') sendMsg()">
        <button id="send-btn" onclick="sendMsg()">Send</button>
    </div>

    <script>
        async function sendMsg() {
            const inp = document.getElementById('user-input');
            const txt = inp.value.trim();
            if (!txt) return;

            inp.value = '';
            addMsg(txt, 'user');
            
            const btn = document.getElementById('send-btn');
            btn.disabled = true;
            inp.disabled = true;

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: txt })
                });
                const data = await res.json();
                if (data.response) {
                    addMsg(data.response, 'athena');
                } else if (data.error) {
                    addMsg('Error: ' + data.error, 'system');
                }
            } catch (e) {
                addMsg('Network error connecting to Athena server.', 'system');
            } finally {
                btn.disabled = false;
                inp.disabled = false;
                inp.focus();
            }
        }

        function sendCmd(cmd) {
            document.getElementById('user-input').value = cmd;
            sendMsg();
        }

        function addMsg(text, type) {
            const log = document.getElementById('chat-log');
            const div = document.createElement('div');
            div.className = 'msg ' + type;
            div.innerText = text;
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        }
    </script>
</body>
</html>
"""


class AthenaRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._send_html(MOBILE_WEB_HTML)
        elif path == "/api/status":
            mgr = get_manager()
            h = mgr.get_healthiest_provider()
            active_p = mgr.providers.get(mgr.active_provider_id) if mgr.active_provider_id else h
            self._send_json({
                "status": "online",
                "version": "v1.3",
                "active_provider": active_p.name if active_p else "None",
                "active_model": mgr.active_model_override or (active_p.default_model if active_p else "None"),
                "providers_count": len(mgr.providers)
            })
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(post_data.decode("utf-8"))
        except Exception:
            payload = {}

        if path == "/api/chat":
            prompt = payload.get("prompt", "").strip()
            project = payload.get("project", "default")
            session = payload.get("session", "session_1")

            if not prompt:
                self._send_json({"error": "Prompt parameter required"}, 400)
                return

            try:
                agent = get_agent_instance(project_id=project, session_id=session)
                response = agent.run_one_turn(prompt)
                self._send_json({
                    "success": True,
                    "response": response,
                    "project": project,
                    "session": session
                })
            except Exception as e:
                logger.error("API error during chat turn: %s", e)
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "Not found"}, 404)

    def log_message(self, format, *args):
        pass


def start_api_server(host: str = "0.0.0.0", port: int = 8080):
    server = HTTPServer((host, port), AthenaRequestHandler)
    print(f"\n[Athena Server] Mobile Remote Control Server running at http://localhost:{port}")
    print(f"[Athena Server] Connect from mobile on your local Wi-Fi: http://<your-ip>:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Athena Server] Shutting down server.")
        server.server_close()
