import sqlite3
import hashlib
import re
import time
import json
import logging
from pathlib import Path
from config import get_athena_home

logger = logging.getLogger("athena.memory_engine")

def get_db_path() -> Path:
    return get_athena_home() / "athena_v1.db"

def get_db_connection():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    # Enable WAL mode for concurrency and performance
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def initialize_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_hash TEXT UNIQUE NOT NULL,
                    fact TEXT NOT NULL,
                    category TEXT NOT NULL,
                    importance INTEGER DEFAULT 5,
                    confidence REAL DEFAULT 0.8,
                    decay_rate REAL DEFAULT 0.05,
                    mention_count INTEGER DEFAULT 1,
                    scope_ids TEXT DEFAULT '[]',
                    archived INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    turns_count INTEGER DEFAULT 0,
                    tokens_count INTEGER DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_hash ON facts(fact_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_archived ON facts(archived)")
    except Exception as exc:
        logger.error("Failed to initialize SQLite database: %s", exc)
    finally:
        conn.close()

def normalize_fact_text(fact: str) -> str:
    # Lowercase, remove special characters/punctuation, collapse spaces
    cleaned = fact.lower().strip()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned

def compute_fact_hash(fact: str) -> str:
    normalized = normalize_fact_text(fact)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def insert_or_reinforce_fact(
    fact: str, 
    category: str = "general", 
    importance: int = 5, 
    confidence: float = 0.8, 
    scope_ids: list = None,
    decay_rate: float = 0.05
) -> str:
    initialize_db()
    scopes = scope_ids or ["global"]
    fact_hash = compute_fact_hash(fact)
    now = int(time.time())
    
    conn = get_db_connection()
    try:
        with conn:
            # Try to insert fresh fact
            conn.execute("""
                INSERT INTO facts (
                    fact_hash, fact, category, importance, confidence, 
                    decay_rate, mention_count, scope_ids, archived, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)
            """, (fact_hash, fact.strip(), category.strip(), importance, confidence, 
                  decay_rate, json.dumps(scopes), now, now))
            return "inserted"
    except sqlite3.IntegrityError:
        # Fact already exists, reinforce it
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, importance, confidence, scope_ids, mention_count 
                    FROM facts WHERE fact_hash = ?
                """, (fact_hash,))
                row = cursor.fetchone()
                if row:
                    row_id, old_importance, old_confidence, old_scopes_json, old_mentions = row
                    
                    # Reinforce metrics
                    new_mentions = old_mentions + 1
                    new_importance = min(10, old_importance + 1)
                    new_confidence = min(1.0, old_confidence + 0.1)
                    
                    # Update scopes
                    try:
                        old_scopes = json.loads(old_scopes_json)
                        if not isinstance(old_scopes, list):
                            old_scopes = []
                    except Exception:
                        old_scopes = []
                    for s in scopes:
                        if s not in old_scopes:
                            old_scopes.append(s)
                            
                    # Update DB row, making sure it is un-archived
                    conn.execute("""
                        UPDATE facts SET 
                            importance = ?, 
                            confidence = ?, 
                            mention_count = ?, 
                            scope_ids = ?, 
                            archived = 0, 
                            updated_at = ?
                        WHERE id = ?
                    """, (new_importance, new_confidence, new_mentions, json.dumps(old_scopes), now, row_id))
                    return "reinforced"
        except Exception as exc:
            logger.error("Failed to reinforce fact: %s", exc)
            return "error"
    except Exception as exc:
        logger.error("Failed to insert fact: %s", exc)
        return "error"
    finally:
        conn.close()
    return "error"

def decay_memories(time_elapsed_days: float):
    if time_elapsed_days <= 0:
        return
    initialize_db()
    conn = get_db_connection()
    now = int(time.time())
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, importance, decay_rate, fact FROM facts WHERE archived = 0")
            rows = cursor.fetchall()
            for row in rows:
                row_id, importance, decay_rate, fact = row
                # Apply decay math
                decay_amount = decay_rate * time_elapsed_days
                new_importance = max(0.0, float(importance) - decay_amount)
                
                # If importance drops below 1.0, archive it
                archived = 1 if new_importance < 1.0 else 0
                
                # Convert to integer importance for DB schema consistency
                new_importance_int = int(round(new_importance))
                
                conn.execute("""
                    UPDATE facts SET 
                        importance = ?, 
                        archived = ?, 
                        updated_at = ?
                    WHERE id = ?
                """, (new_importance_int, archived, now, row_id))
                if archived:
                    logger.info("Archived decayed memory: %s (Importance: %s -> %s)", 
                                fact[:30], importance, new_importance_int)
    except Exception as exc:
        logger.error("Failed to apply decay: %s", exc)
    finally:
        conn.close()

def get_diagnostics_stats() -> dict:
    stats = {
        "total_facts": 0,
        "active_facts": 0,
        "archived_facts": 0,
        "db_size_bytes": 0
    }
    db_path = get_db_path()
    if db_path.exists():
        stats["db_size_bytes"] = db_path.stat().st_size
    else:
        return stats
        
    initialize_db()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM facts")
        stats["total_facts"] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM facts WHERE archived = 0")
        stats["active_facts"] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM facts WHERE archived = 1")
        stats["archived_facts"] = cursor.fetchone()[0]
    except Exception as exc:
        logger.error("Failed to get DB stats: %s", exc)
    finally:
        conn.close()
    return stats
