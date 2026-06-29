import queue
import threading
import json
import logging
import providers
import memory_engine

logger = logging.getLogger("athena.distillation")

_distill_queue = queue.Queue()
_worker_thread = None

def _distill_worker_loop():
    logger.info("Distillation background worker started.")
    while True:
        try:
            item = _distill_queue.get()
            if item is None:
                # Sentinel to stop worker
                break
            user_msg, agent_msg, scope_ids = item
            _run_distillation(user_msg, agent_msg, scope_ids)
        except Exception as exc:
            logger.error("Error in distillation worker: %s", exc)
        finally:
            _distill_queue.task_done()

def start_distillation_worker():
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_distill_worker_loop, daemon=True)
        _worker_thread.start()

def enqueue_distillation(user_msg: str, agent_msg: str, scope_ids: list):
    start_distillation_worker()
    _distill_queue.put((user_msg, agent_msg, scope_ids))

def _run_distillation(user_msg: str, agent_msg: str, scope_ids: list):
    try:
        client, model, provider = providers.get_routing_client()
    except Exception as exc:
        logger.error("Distillation aborted: no model client: %s", exc)
        return
        
    prompt = (
        "You are the distillation engine for Athena v1, a memory-first agent.\n"
        "Your objective: Extract important facts from the conversation, rate their importance and confidence, and return compact knowledge.\n"
        "Input:\n"
        f"User: {user_msg}\n"
        f"Agent: {agent_msg}\n\n"
        "Instructions:\n"
        "- Identify any new statements of facts, user preferences, strategies, decisions, or system settings.\n"
        "- Ignore greetings, small talk, and polite padding.\n"
        "- Return the output as a valid raw JSON object with a single key 'facts' containing a list of objects.\n"
        "- Each object must have: 'text' (string), 'importance' (int 1-10), and 'confidence' (float 0.0-1.0).\n"
        "Example output: {\"facts\": [{\"text\": \"User runs Windows 11.\", \"importance\": 7, \"confidence\": 0.95}]}"
    )
    
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a JSON-only factual distillation assistant."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            timeout=5.0
        )
        content = response.choices[0].message.content
        providers.record_success(provider)
        
        # Parse JSON
        data = json.loads(content)
        facts = data.get("facts", [])
        
        for fact_obj in facts:
            if isinstance(fact_obj, dict):
                fact_str = str(fact_obj.get("text", "")).strip()
                imp = int(fact_obj.get("importance", 5))
                conf = float(fact_obj.get("confidence", 0.8))
            else:
                fact_str = str(fact_obj).strip()
                imp = 5
                conf = 0.8
                
            if fact_str:
                result = memory_engine.insert_or_reinforce_fact(
                    fact=fact_str,
                    category="general",
                    importance=imp,
                    confidence=conf,
                    scope_ids=scope_ids
                )
                logger.info("Fact distillation result (%s): %s", result, fact_str[:40])
    except Exception as exc:
        logger.error("Failed to run fact distillation: %s", exc)
        providers.record_failure(provider)

