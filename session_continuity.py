import os
import re
import time
import json
import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

import config

logger = logging.getLogger("athena.session_continuity")

# Provider context window map (in tokens)
PROVIDER_CONTEXT_WINDOWS = {
    "gemini": 1000000,
    "openrouter": 128000,
    "openai-api": 128000,
    "groq": 32768,
    "nvidia": 65536,
    "github-copilot": 128000
}

def get_db_path() -> Path:
    home = config.get_athena_home()
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / "session_continuity.db"

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_id TEXT,
            summary TEXT,
            summary_version INTEGER DEFAULT 0,
            summary_marker INTEGER DEFAULT 0,
            context_pressure REAL DEFAULT 0.0,
            status TEXT DEFAULT 'ACTIVE',
            created_at REAL,
            updated_at REAL,
            expires_at REAL,
            archived_at REAL
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            topic TEXT,
            mention_count INTEGER DEFAULT 1,
            last_mentioned REAL,
            score REAL DEFAULT 1.0,
            status TEXT DEFAULT 'ACTIVE',
            priority TEXT DEFAULT 'NORMAL',
            resurfaced INTEGER DEFAULT 0,
            expires_at REAL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS continuity_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            session_id TEXT,
            trigger TEXT,
            identity TEXT,
            summary TEXT,
            active_topics TEXT,
            recent_context TEXT,
            summary_marker INTEGER,
            created_at REAL,
            provider_from TEXT,
            provider_to TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );
        """)
        conn.commit()

class SessionContinuityLayer:
    def __init__(self, session_id: str, project_id: str = "default"):
        self.session_id = session_id.strip()
        self.project_id = project_id.strip()
        init_db()
        self._ensure_session_exists()

    def _ensure_session_exists(self):
        cfg = config.load_config().get("session_continuity", {})
        ttl_hours = cfg.get("session_ttl_hours", 24)
        now = time.time()
        expires_at = now + (ttl_hours * 3600)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT session_id FROM sessions WHERE session_id = ?", (self.session_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    INSERT INTO sessions (session_id, project_id, summary, summary_version, summary_marker, context_pressure, status, created_at, updated_at, expires_at)
                    VALUES (?, ?, '', 0, 0, 0.0, 'ACTIVE', ?, ?, ?)
                """, (self.session_id, self.project_id, now, now, expires_at))
            conn.commit()

    def get_session_context(self) -> Dict[str, Any]:
        self.sweep_expired_sessions()
        self._apply_topic_decay()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT summary, summary_version, summary_marker FROM sessions WHERE session_id = ?", (self.session_id,))
            s_row = cursor.fetchone()
            summary = s_row["summary"] if s_row else ""
            version = s_row["summary_version"] if s_row else 0
            marker = s_row["summary_marker"] if s_row else 0

            cursor.execute("""
                SELECT topic, score, status, priority FROM topics 
                WHERE session_id = ? AND status IN ('ACTIVE', 'DORMANT')
                ORDER BY score DESC LIMIT 5
            """, (self.session_id,))
            topics = [dict(r) for r in cursor.fetchall()]

        return {
            "summary": summary,
            "summary_version": version,
            "summary_marker": marker,
            "active_topics": topics
        }

    def get_summary_marker(self) -> int:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT summary_marker FROM sessions WHERE session_id = ?", (self.session_id,))
            row = cursor.fetchone()
            return row["summary_marker"] if row else 0

    def update_after_turn(self, user_msg: str, assistant_msg: str, msg_index: int):
        now = time.time()
        combined = f"{user_msg}\n{assistant_msg}"
        extracted = self._extract_topics(combined)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            for t_name in extracted:
                cursor.execute("SELECT id, mention_count, score, status, priority FROM topics WHERE session_id = ? AND LOWER(topic) = LOWER(?)", (self.session_id, t_name))
                row = cursor.fetchone()
                if row:
                    new_count = row["mention_count"] + 1
                    new_score = min(1.0, row["score"] + 0.3)
                    was_dormant = row["status"] == "DORMANT"
                    cursor.execute("""
                        UPDATE topics SET mention_count = ?, last_mentioned = ?, score = ?, status = 'ACTIVE', resurfaced = ?
                        WHERE id = ?
                    """, (new_count, now, new_score, 1 if was_dormant else 0, row["id"]))
                else:
                    cursor.execute("""
                        INSERT INTO topics (session_id, topic, mention_count, last_mentioned, score, status, priority, resurfaced, expires_at)
                        VALUES (?, ?, 1, ?, 1.0, 'ACTIVE', 'NORMAL', 0, ?)
                    """, (self.session_id, t_name, now, now + 86400))
            
            cursor.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, self.session_id))
            conn.commit()

    def _extract_topics(self, text: str) -> List[str]:
        # Stage 1: Regex noun-phrase scanner (capitalized words and significant terms)
        words = re.findall(r'\b[a-zA-Z0-9_\-]{3,}\b', text)
        stopwords = {"the", "this", "that", "here", "there", "when", "what", "user", "agent", "athena", "none", "true", "false", "asked", "about", "talked", "with", "from", "have", "been", "were", "would", "could", "should"}
        filtered = []
        for w in words:
            w_clean = w.strip()
            if w_clean.lower() not in stopwords and w_clean.lower() not in [f.lower() for f in filtered]:
                filtered.append(w_clean)

        return filtered[:5]

    def _apply_topic_decay(self):
        now = time.time()
        cfg = config.load_config().get("session_continuity", {})
        decay_interval = cfg.get("topic_decay_interval_minutes", 5) * 60
        dormant_thresh = cfg.get("topic_dormant_threshold", 0.40)
        inactive_thresh = cfg.get("topic_inactive_threshold", 0.15)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, last_mentioned, score, status, priority FROM topics WHERE session_id = ?", (self.session_id,))
            rows = cursor.fetchall()
            for r in rows:
                elapsed = now - r["last_mentioned"]
                if elapsed > decay_interval:
                    intervals = elapsed / decay_interval
                    new_score = max(0.0, r["score"] * (0.85 ** intervals))
                    
                    new_status = r["status"]
                    # PINNED topics score decays, but status never auto-deactivates
                    if r["priority"] != "PINNED":
                        if new_score < inactive_thresh:
                            new_status = "INACTIVE"
                        elif new_score < dormant_thresh:
                            new_status = "DORMANT"

                    cursor.execute("UPDATE topics SET score = ?, status = ? WHERE id = ?", (new_score, new_status, r["id"]))
            conn.commit()

    def pin_topic(self, topic: str):
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE topics SET priority = 'PINNED', status = 'ACTIVE', score = 1.0 WHERE session_id = ? AND LOWER(topic) = LOWER(?)", (self.session_id, topic))
            conn.commit()

    def unpin_topic(self, topic: str):
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE topics SET priority = 'NORMAL' WHERE session_id = ? AND LOWER(topic) = LOWER(?)", (self.session_id, topic))
            conn.commit()

    def should_retrieve_long_term(self, user_msg: str) -> bool:
        user_msg_lower = user_msg.lower()
        active_topics = self.get_active_topics(top_n=5)
        for t in active_topics:
            topic_name = t["topic"].lower()
            if topic_name in user_msg_lower and t["score"] >= 0.7:
                return False
        return True

    def get_active_topics(self, top_n: int = 5) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT topic, mention_count, last_mentioned, score, status, priority, resurfaced 
                FROM topics WHERE session_id = ? AND status = 'ACTIVE'
                ORDER BY score DESC LIMIT ?
            """, (self.session_id, top_n))
            return [dict(r) for r in cursor.fetchall()]

    def compute_context_pressure(self, messages: List[Dict[str, Any]], provider_id: str = "gemini") -> float:
        total_chars = 0
        for m in messages:
            content = m.get("content") or ""
            total_chars += len(str(content))
        
        estimated_tokens = total_chars // 4
        window = PROVIDER_CONTEXT_WINDOWS.get(provider_id.lower(), 128000)
        return min(1.0, round(estimated_tokens / window, 4))

    def _write_session(self, summary: str, summary_version: int, summary_marker: int, context_pressure: float):
        now = time.time()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sessions 
                SET summary = ?, summary_version = ?, summary_marker = ?, context_pressure = ?, updated_at = ?
                WHERE session_id = ?
            """, (summary, summary_version, summary_marker, context_pressure, now, self.session_id))
            conn.commit()

    def create_rotation_snapshot(self, trigger: str, identity: str, recent_context: List[Dict[str, Any]], provider_from: str) -> str:
        import uuid
        snap_id = f"snap_{uuid.uuid4().hex[:8]}"
        ctx = self.get_session_context()
        now = time.time()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO continuity_snapshots (snapshot_id, session_id, trigger, identity, summary, active_topics, recent_context, summary_marker, created_at, provider_from)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snap_id, self.session_id, trigger, identity, ctx["summary"],
                json.dumps(ctx["active_topics"]), json.dumps(recent_context), ctx["summary_marker"], now, provider_from
            ))
            conn.commit()
        return snap_id

    def load_latest_snapshot(self) -> Optional[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM continuity_snapshots WHERE session_id = ? ORDER BY created_at DESC LIMIT 1
            """, (self.session_id,))
            row = cursor.fetchone()
            if row:
                res = dict(row)
                res["active_topics"] = json.loads(res["active_topics"]) if res["active_topics"] else []
                res["recent_context"] = json.loads(res["recent_context"]) if res["recent_context"] else []
                return res
        return None

    def archive_session(self):
        now = time.time()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE sessions SET status = 'ARCHIVED', archived_at = ? WHERE session_id = ?", (now, self.session_id))
            conn.commit()

    def export_for_new_chat(self) -> Dict[str, Any]:
        ctx = self.get_session_context()
        return {
            "project_id": self.project_id,
            "summary": ctx["summary"],
            "summary_version": ctx["summary_version"],
            "summary_marker": ctx["summary_marker"],
            "active_topics": ctx["active_topics"]
        }

    def sweep_expired_sessions(self):
        now = time.time()
        cfg = config.load_config().get("session_continuity", {})
        ttl_hours = cfg.get("session_ttl_hours", 24)
        retention_hours = cfg.get("archive_retention_hours", 72)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sessions SET status = 'ARCHIVED', archived_at = ?
                WHERE status = 'ACTIVE' AND expires_at < ?
            """, (now, now))
            
            cutoff = now - (retention_hours * 3600)
            cursor.execute("""
                DELETE FROM sessions WHERE status = 'ARCHIVED' AND archived_at < ?
            """, (cutoff,))
            conn.commit()
