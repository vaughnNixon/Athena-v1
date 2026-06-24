import sqlite3
import time
import json
import logging
from pathlib import Path
import config
import memory_engine

logger = logging.getLogger("athena.retrieval")

def apply_lazy_decay_to_fact(cursor, row_id, old_importance, decay_rate, updated_at, now):
    elapsed_seconds = now - updated_at
    if elapsed_seconds < 86400:
        return old_importance, 0
        
    decay_days = elapsed_seconds / 86400.0
    decay_amount = decay_rate * decay_days
    new_importance = max(0.0, float(old_importance) - decay_amount)
    archived = 1 if new_importance < 1.0 else 0
    new_importance_int = int(round(new_importance))
    
    cursor.execute("""
        UPDATE facts SET 
            importance = ?, 
            archived = ?, 
            updated_at = ?
        WHERE id = ?
    """, (new_importance_int, archived, now, row_id))
    return new_importance_int, archived

def get_word_set(text: str) -> set:
    normalized = memory_engine.normalize_fact_text(text)
    return set(normalized.split())

def calculate_overlap_score(query_words: set, fact_words: set) -> float:
    if not query_words:
        return 0.0
    overlap = query_words.intersection(fact_words)
    return len(overlap) / len(query_words)

def retrieve_relevant_memories(query: str, scope_ids: list, limit: int = 5) -> str:
    cfg = config.load_config()
    mem_cfg = cfg.get("memory", {})
    weights = mem_cfg.get("weights", {"keyword": 1.0, "importance": 1.0, "confidence": 1.0})
    w_k = float(weights.get("keyword", 1.0))
    w_i = float(weights.get("importance", 1.0))
    w_c = float(weights.get("confidence", 1.0))
    
    scopes = scope_ids or ["global"]
    query_words = get_word_set(query)
    now = int(time.time())
    
    conn = memory_engine.get_db_connection()
    scored_facts = []
    try:
        cursor = conn.cursor()
        # Query all non-archived facts
        cursor.execute("""
            SELECT id, fact, category, importance, confidence, decay_rate, scope_ids, mention_count, updated_at
            FROM facts WHERE archived = 0
        """)
        rows = cursor.fetchall()
        
        for row in rows:
            row_id, fact_text, category, importance, confidence, decay_rate, scopes_json, mention_count, updated_at = row
            
            # 1. Apply lazy decay check
            importance, archived = apply_lazy_decay_to_fact(
                cursor, row_id, importance, decay_rate, updated_at, now
            )
            if archived:
                continue
                
            # 2. Score
            fact_words = get_word_set(fact_text)
            kw_score = calculate_overlap_score(query_words, fact_words)
            
            # Normalise importance to 0-1 scale
            imp_norm = float(importance) / 10.0
            
            # Recency penalty (lower penalty for more recent facts)
            # Days elapsed / 30 days scale
            days_elapsed = (now - updated_at) / 86400.0
            recency_penalty = min(0.5, days_elapsed / 30.0) # Cap penalty at 0.5
            
            # Scope bonus
            try:
                fact_scopes = json.loads(scopes_json)
            except Exception:
                fact_scopes = []
            has_scope_overlap = any(s in fact_scopes for s in scopes)
            scope_bonus = 0.5 if has_scope_overlap else 0.0
            
            composite_score = (kw_score * w_k) + (imp_norm * w_i) + (confidence * w_c) - recency_penalty + scope_bonus
            
            scored_facts.append({
                "id": row_id,
                "fact": fact_text,
                "category": category,
                "confidence": confidence,
                "score": composite_score,
                "query_overlap": kw_score
            })
            
        # Commit decay updates
        conn.commit()
    except Exception as exc:
        logger.error("Failed to retrieve memories: %s", exc)
        return ""
    finally:
        conn.close()
        
    if not scored_facts:
        return ""
        
    # Sort descending by composite score
    scored_facts.sort(key=lambda x: x["score"], reverse=True)
    top_matches = scored_facts[:limit]
    
    # Self-improving query evolution: reinforce the retrieved facts
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            for match in top_matches:
                # Evolve keywords by adding query terms to the category if overlap was strong (> 0.2)
                if match["query_overlap"] > 0.2:
                    current_cat = match["category"]
                    new_keywords = [w for w in query_words if w not in get_word_set(match["fact"])][:3]
                    if new_keywords:
                        updated_cat = current_cat
                        for kw in new_keywords:
                            if kw not in current_cat.split(","):
                                updated_cat = f"{updated_cat},{kw}"
                        
                        # Increment mention, boost confidence by 0.05, and update time
                        new_conf = min(1.0, match["confidence"] + 0.05)
                        conn.execute("""
                            UPDATE facts SET 
                                category = ?, 
                                confidence = ?, 
                                mention_count = mention_count + 1, 
                                updated_at = ?
                            WHERE id = ?
                        """, (updated_cat, new_conf, now, match["id"]))
    except Exception as exc:
        logger.error("Failed to reinforce retrieved query terms: %s", exc)
    finally:
        conn.close()
        
    # Format [ATHENA MEMORY] block
    lines = ["[ATHENA MEMORY]"]
    for match in top_matches:
        lines.append(f"• {match['fact']}")
        
    formatted_block = "\n".join(lines)
    
    # Headroom check: Limit injection size to ~500 tokens (roughly 2000 chars)
    # If the block is too large, drop lower scoring facts until it fits.
    while len(formatted_block) > 2000 and len(top_matches) > 1:
        top_matches.pop()
        lines = ["[ATHENA MEMORY]"]
        for match in top_matches:
            lines.append(f"• {match['fact']}")
        formatted_block = "\n".join(lines)
        
    return formatted_block

PHATIC_PHRASES = {
    "hello", "hi", "hey", "greetings", "yo", "sup", "howdy", "athena",
    "hola", "bonjour", "namaste", "good morning", "good afternoon",
    "good evening", "goodnight", "good night", "bye", "goodbye", "exit", "quit",
    "hello athena", "hi athena", "hey athena", "yo athena",
    "how are you", "how are you doing", "how's it going", "hows it going",
    "what's up", "whats up", "what is up", "what's going on", "whats going on",
    "is anyone there", "are you there", "are you alive", "u alive", "hello u alive", "hello athena u alive",
    "test", "testing"
}

STOP_WORDS = {
    "is", "are", "you", "there", "it", "a", "the", "to", "how", "what", "do",
    "hello", "hi", "hey", "athena", "alive", "u", "greetings", "yo", "sup",
    "howdy", "hola", "bonjour", "namaste", "good", "morning", "afternoon",
    "evening", "night", "goodnight", "goodbye", "bye", "exit", "quit",
    "test", "testing", "doing", "going", "on", "up", "who", "anyone",
    "here", "someone", "am", "i", "was", "were", "be", "been", "have", "has",
    "had", "will", "would", "shall", "should", "can", "could", "may", "might"
}

def is_phatic_query(query: str) -> bool:
    cleaned = query.lower().strip().rstrip("?.!,")
    if cleaned in PHATIC_PHRASES:
        return True
    words = [w.strip("?.!,:;") for w in cleaned.split() if w]
    words = [w for w in words if w]
    if not words:
        return True
    if all(w in STOP_WORDS for w in words):
        return True
    return False



