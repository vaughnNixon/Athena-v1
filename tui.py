"""
tui.py -- Terminal UI Stub
Note: The full OpenCode-inspired TUI implementation has been safely preserved on the side in `tui_opencode_backup.py`.
This stub redirects execution to Athena's core CLI chat loop.
"""

def run(project_id: str = "default", session_id: str = "session_1") -> None:
    from main import run_chat_loop
    run_chat_loop(project_id=project_id, session_id=session_id)
