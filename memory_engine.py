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
    # Enable foreign key constraint enforcement
    conn.execute("PRAGMA foreign_keys=ON")
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
            # Create chunks table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence_number INTEGER NOT NULL,
                    tier TEXT NOT NULL CHECK(tier IN ('active', 'passive', 'mixed', 'unclassified')),
                    raw_text TEXT NOT NULL,
                    caveman_text TEXT NOT NULL,
                    start_ts INTEGER NOT NULL,
                    end_ts INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    migrated_from_fact_id INTEGER,
                    FOREIGN KEY(migrated_from_fact_id) REFERENCES facts(id) ON DELETE SET NULL
                )
            """)
            # Create normalized chunk keywords table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunk_keywords (
                    chunk_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    PRIMARY KEY (chunk_id, keyword),
                    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
                )
            """)
            # Create skip marks table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skip_marks (
                    skip_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id INTEGER NOT NULL,
                    query_type TEXT NOT NULL,
                    skip_score REAL NOT NULL CHECK(skip_score >= 0.0 AND skip_score <= 1.0),
                    feedback_count INTEGER DEFAULT 0,
                    last_updated_ts INTEGER NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                    UNIQUE(chunk_id, query_type)
                )
            """)
            # Create feedback log table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_log (
                    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_query TEXT NOT NULL,
                    athena_answer_summary TEXT NOT NULL,
                    user_correction_text TEXT NOT NULL,
                    chunks_used_in_answer TEXT NOT NULL,
                    chunks_used_in_desperation TEXT NOT NULL,
                    was_helpful INTEGER CHECK(was_helpful IN (0, 1)),
                    retrieval_stage TEXT,
                    matched_chunk_ids TEXT,
                    desperation_chunk_ids TEXT,
                    explanation TEXT,
                    timestamp INTEGER NOT NULL
                )
            """)
            
            # Check and alter feedback_log if missing new columns (backward compatibility)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(feedback_log)")
            columns = [row[1] for row in cursor.fetchall()]
            if columns:
                new_cols = {
                    "retrieval_stage": "TEXT",
                    "matched_chunk_ids": "TEXT",
                    "desperation_chunk_ids": "TEXT",
                    "explanation": "TEXT"
                }
                for col_name, col_type in new_cols.items():
                    if col_name not in columns:
                        conn.execute(f"ALTER TABLE feedback_log ADD COLUMN {col_name} {col_type}")

            # Create query statistics table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_statistics (
                    query_type TEXT PRIMARY KEY,
                    total_queries INTEGER DEFAULT 0,
                    corrected_queries INTEGER DEFAULT 0,
                    accuracy REAL DEFAULT 1.0,
                    last_updated_ts INTEGER NOT NULL
                )
            """)

            # Create schema metadata table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    table_name TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    migrated_at INTEGER,
                    migration_notes TEXT
                )
            """)
            # Create chunk embeddings table for semantic cached storage
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    chunk_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(chunk_id, provider, model),
                    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
                )
            """)
            
            # Indexes for facts
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_hash ON facts(fact_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_archived ON facts(archived)")
            
            # Indexes for chunks
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_tier ON chunks(tier)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_sequence ON chunks(sequence_number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_timestamps ON chunks(start_ts, end_ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_migrated_from ON chunks(migrated_from_fact_id)")
            
            # Indexes for chunk keywords
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_keywords_val ON chunk_keywords(keyword)")
            
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
                    
                    # Reinforce metrics (cap mentions at 1000 to prevent unbounded growth)
                    new_mentions = min(1000, old_mentions + 1)
                    new_importance = min(10, old_importance + 1)
                    new_confidence = min(1.0, old_confidence + 0.1)
                    
                    # Update scopes
                    try:
                        old_scopes = json.loads(old_scopes_json)
                        if not isinstance(old_scopes, list):
                            old_scopes = ["global"]
                    except Exception:
                        logger.warning("Corrupted scopes JSON for fact_hash %s, resetting to global", fact_hash)
                        old_scopes = ["global"]

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

def get_migration_progress() -> dict:
    """
    Returns statistics on the facts-to-chunks migration progress:
    - total_facts: Total rows in the legacy 'facts' table
    - migrated_facts: Number of legacy facts already in the 'chunks' table
    - remaining_facts: Facts that still need migration
    - percentage_complete: Progress percentage (0.0 to 100.0)
    """
    initialize_db()
    conn = get_db_connection()
    stats = {
        "total_facts": 0,
        "migrated_facts": 0,
        "remaining_facts": 0,
        "percentage_complete": 100.0
    }
    try:
        cursor = conn.cursor()
        
        # Total legacy facts
        cursor.execute("SELECT COUNT(*) FROM facts")
        stats["total_facts"] = cursor.fetchone()[0]
        
        # Migrated facts count
        cursor.execute("SELECT COUNT(DISTINCT migrated_from_fact_id) FROM chunks WHERE migrated_from_fact_id IS NOT NULL")
        stats["migrated_facts"] = cursor.fetchone()[0]
        
        stats["remaining_facts"] = max(0, stats["total_facts"] - stats["migrated_facts"])
        if stats["total_facts"] > 0:
            stats["percentage_complete"] = round((stats["migrated_facts"] / stats["total_facts"]) * 100.0, 2)
        else:
            stats["percentage_complete"] = 100.0
            
    except Exception as exc:
        logger.error("Failed to fetch migration progress: %s", exc)
    finally:
        conn.close()
    return stats

def migrate_legacy_facts() -> dict:
    """
    Idempotent and resumable transaction-wrapped facts migration to chunk system.
    """
    initialize_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Fetch initial progress
    progress = get_migration_progress()
    if progress["remaining_facts"] == 0:
        return progress
        
    # Get current max sequence_number in chunks (defaulting to 0)
    cursor.execute("SELECT IFNULL(MAX(sequence_number), 0) FROM chunks")
    current_seq = cursor.fetchone()[0]
        
    # 2. Query facts that have NOT been migrated yet, ordered chronologically (created_at ASC, id ASC)
    cursor.execute("""
        SELECT id, fact, category, importance, confidence, decay_rate, mention_count, scope_ids, archived, created_at, updated_at
        FROM facts f
        WHERE NOT EXISTS (
            SELECT 1 FROM chunks c WHERE c.migrated_from_fact_id = f.id
        )
        ORDER BY created_at ASC, id ASC
    """)
    facts_to_migrate = cursor.fetchall()
    
    new_migrated_count = 0
    now_ts = int(time.time())
    
    try:
        # Wrap everything in a single transaction
        with conn:
            for row in facts_to_migrate:
                (fact_id, fact_text, category, importance, confidence, 
                 decay_rate, mention_count, scope_ids_json, archived, created_at, updated_at) = row
                 
                # tier defaults to 'unclassified' for all migrated legacy facts
                tier = "unclassified"
                
                # Increment sequence number
                current_seq += 1
                
                # Deterministic caveman conversion fallback
                words = fact_text.split()
                fillers = {"the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "in", "on", "at", "for"}
                caveman_words = [w.upper() for w in words if w.lower() not in fillers]
                caveman_text = " ".join(caveman_words) if caveman_words else fact_text.upper()
                
                # Extract keywords (words > 2 chars, lowercase, stripped of non-alphanumeric chars)
                words_cleaned = re.findall(r"\w+", fact_text.lower())
                keywords_set = set(w for w in words_cleaned if len(w) > 2)
                
                # Add category keywords
                if category:
                    for cat in category.split(","):
                        cat_clean = cat.strip().lower()
                        if cat_clean:
                            keywords_set.add(cat_clean)
                            
                # Add scope keywords
                try:
                    scopes = json.loads(scope_ids_json)
                    if isinstance(scopes, list):
                        for s in scopes:
                            s_clean = s.strip().lower()
                            if s_clean:
                                keywords_set.add(s_clean)
                except Exception:
                    pass
                
                # Future-proof metadata with reserved keys
                metadata_dict = {
                    "workspace": None,
                    "project": None,
                    "skill": None,
                    "annotation": None,
                    "legacy_category": category,
                    "legacy_importance": importance,
                    "legacy_confidence": confidence,
                    "legacy_decay_rate": decay_rate,
                    "legacy_mention_count": mention_count,
                    "legacy_scope_ids": scope_ids_json
                }
                
                char_count = len(fact_text)
                token_estimate = char_count // 4
                
                # 1. Insert core chunk
                cursor.execute("""
                    INSERT INTO chunks (
                        sequence_number, tier, raw_text, caveman_text, start_ts, end_ts, 
                        char_count, token_estimate, metadata, created_at, updated_at, migrated_from_fact_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    current_seq, tier, fact_text, caveman_text, created_at, updated_at,
                    char_count, token_estimate, json.dumps(metadata_dict), created_at, updated_at, fact_id
                ))
                chunk_id = cursor.lastrowid
                
                # 2. Insert normalized keywords into chunk_keywords table
                for kw in sorted(list(keywords_set)):
                    cursor.execute("""
                        INSERT INTO chunk_keywords (chunk_id, keyword)
                        VALUES (?, ?)
                    """, (chunk_id, kw))
                    
                new_migrated_count += 1
                
            # 3. Update schema_metadata status for facts (deprecated) and chunks (active)
            cursor.execute("""
                INSERT INTO schema_metadata (table_name, schema_version, status, migrated_at, migration_notes)
                VALUES ('facts', 1, 'deprecated', ?, ?)
                ON CONFLICT(table_name) DO UPDATE SET
                    status='deprecated',
                    migrated_at=excluded.migrated_at,
                    migration_notes=excluded.migration_notes
            """, (now_ts, f"Migrated {new_migrated_count} facts successfully."))
            
            cursor.execute("""
                INSERT INTO schema_metadata (table_name, schema_version, status, migrated_at, migration_notes)
                VALUES ('chunks', 1, 'active', ?, 'New chunk memory system active.')
                ON CONFLICT(table_name) DO UPDATE SET
                    status='active',
                    migrated_at=excluded.migrated_at,
                    migration_notes=excluded.migration_notes
            """, (now_ts,))
            
        logger.info("Migrated %d facts to the new chunk-based memory architecture.", new_migrated_count)
    except Exception as exc:
        logger.error("Migration failed and transaction was rolled back: %s", exc)
        raise exc
    finally:
        conn.close()
        
    return get_migration_progress()

def insert_chunk(
    tier: str,
    raw_text: str,
    start_ts: int,
    end_ts: int,
    metadata_dict: dict = None,
    caveman_text: str = None,
    migrated_from_fact_id: int = None,
    keywords: list = None
) -> int:
    """
    Inserts a new chunk into the database, assigning the next available sequence number.
    Ensures that once assigned, the sequence number is never modified.
    """
    initialize_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if tier not in ('active', 'passive', 'mixed', 'unclassified'):
        raise ValueError(f"Invalid tier: {tier}")
        
    try:
        with conn:
            # Get next sequence number inside the transaction
            cursor.execute("SELECT IFNULL(MAX(sequence_number), 0) FROM chunks")
            next_seq = cursor.fetchone()[0] + 1
            
            # Compute caveman text if not provided
            if not caveman_text:
                words = raw_text.split()
                fillers = {"the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "in", "on", "at", "for"}
                caveman_words = [w.upper() for w in words if w.lower() not in fillers]
                caveman_text = " ".join(caveman_words) if caveman_words else raw_text.upper()
                
            # Process metadata
            meta = metadata_dict or {}
            # Ensure future-proof metadata keys exist if dict provided
            for key in ["workspace", "project", "skill", "annotation"]:
                if key not in meta:
                    meta[key] = None
                    
            char_count = len(raw_text)
            token_estimate = char_count // 4
            now_ts = int(time.time())
            
            cursor.execute("""
                INSERT INTO chunks (
                    sequence_number, tier, raw_text, caveman_text, start_ts, end_ts,
                    char_count, token_estimate, metadata, created_at, updated_at, migrated_from_fact_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                next_seq, tier, raw_text, caveman_text, start_ts, end_ts,
                char_count, token_estimate, json.dumps(meta), now_ts, now_ts, migrated_from_fact_id
            ))
            chunk_id = cursor.lastrowid
            
            # Determine keywords if not provided
            if keywords is None:
                # Extract keywords (words > 2 chars, lowercase, stripped of non-alphanumeric chars)
                words_cleaned = re.findall(r"\w+", raw_text.lower())
                keywords_set = set(w for w in words_cleaned if len(w) > 2)
            else:
                keywords_set = set(k.strip().lower() for k in keywords if k.strip())
                
            for kw in sorted(list(keywords_set)):
                cursor.execute("""
                    INSERT OR IGNORE INTO chunk_keywords (chunk_id, keyword)
                    VALUES (?, ?)
                """, (chunk_id, kw))
                
            return chunk_id
    except Exception as exc:
        logger.error("Failed to insert chunk: %s", exc)
        raise exc
    finally:
        conn.close()

