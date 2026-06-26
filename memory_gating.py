import logging
import memory_engine

logger = logging.getLogger("athena.memory_gating")

def filter(memory_payload: list[str], aal_summary: dict) -> dict:
    """Gates subagent memory payloads.
    
    Rejects entire payload if outcome is 'failed' or confidence is below 0.3.
    Drops empty strings, items shorter than 20 characters, or items that
    already exist in facts or chunks.
    
    Returns:
        dict: {
            "accepted": list[str],
            "rejected": list[str],
            "reason": str
        }
    """
    outcome = aal_summary.get("outcome", "failed")
    confidence = aal_summary.get("confidence", 0.0)
    
    if outcome == "failed":
        return {
            "accepted": [],
            "rejected": memory_payload,
            "reason": "Execution outcome was failed."
        }
        
    if confidence < 0.3:
        return {
            "accepted": [],
            "rejected": memory_payload,
            "reason": f"Confidence score {confidence} is below the 0.3 gating threshold."
        }
        
    accepted = []
    rejected = []
    
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        for item in memory_payload:
            stripped = item.strip()
            if not stripped:
                rejected.append(item)
                continue
                
            if len(stripped) < 20:
                rejected.append(item)
                continue
                
            # Check duplicates in facts
            fact_hash = memory_engine.compute_fact_hash(stripped)
            cursor.execute("SELECT 1 FROM facts WHERE fact_hash = ?", (fact_hash,))
            if cursor.fetchone():
                rejected.append(item)
                continue
                
            # Check duplicates in chunks
            cursor.execute("SELECT 1 FROM chunks WHERE LOWER(TRIM(raw_text)) = ?", (stripped.lower(),))
            if cursor.fetchone():
                rejected.append(item)
                continue
                
            accepted.append(item)
    except Exception as exc:
        logger.exception("Error checking database in memory gating: %s", exc)
        return {
            "accepted": [],
            "rejected": memory_payload,
            "reason": f"Database check failed during gating: {exc}"
        }
    finally:
        conn.close()
        
    reason = "All items processed successfully."
    if rejected:
        reason = f"Processed items. {len(accepted)} accepted, {len(rejected)} rejected."
        
    return {
        "accepted": accepted,
        "rejected": rejected,
        "reason": reason
    }
