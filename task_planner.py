import json
import logging
import providers
from retrieval import retrieve_memories_staged

logger = logging.getLogger("athena.task_planner")

def plan(user_message: str, project_id: str = None) -> dict | None:
    """Queries Athena's memory for relevant past context, determines the skill and capability,
    and returns a structured plan dict or None if the query is conversational.
    """
    # 1. Query memory first
    retrieval_res = retrieve_memories_staged(user_message, scope_ids=[project_id] if project_id else None)
    supplied_chunks = retrieval_res.get("supplied_chunks", [])
    memory_context = "\n".join([c.get("raw_text", "") for c in supplied_chunks])
    
    # Extract prior outcome from the context
    prior_outcome = None
    for chunk in supplied_chunks:
        text = chunk.get("raw_text", "").lower()
        if "outcome" in text:
            if "success" in text:
                prior_outcome = "success"
                break
            elif "failed" in text or "failure" in text:
                prior_outcome = "failed"
                break
            elif "partial" in text:
                prior_outcome = "partial"
                break

    # 2. Determine skill and namespaced capability (Stage A: Deterministic Rules)
    q = user_message.lower()
    
    file_reader_phrases = ["read file", "open file", "parse file", "load file", "view file", "read_file", "open_file"]
    web_search_phrases = ["look up", "look-up", "search web", "web search"]
    
    has_file_reader = any(p in q for p in file_reader_phrases)
    has_web_search = any(p in q for p in web_search_phrases) or "search the web" in q
    has_code_runner = "run this code" in q or "run script" in q
    has_writer = "write a report" in q or "write a doc" in q or "draft a doc" in q
    
    matched_skills = []
    if has_file_reader: matched_skills.append("file_reader")
    if has_web_search: matched_skills.append("web_search")
    if has_code_runner: matched_skills.append("code_runner")
    if has_writer: matched_skills.append("writer")
    
    skill = None
    capability = None
    task_desc = user_message
    
    if len(matched_skills) == 1:
        skill = matched_skills[0]
    else:
        # Stage B: LLM Fallback
        skip_providers = []
        skip_keys = {}
        llm_success = False
        max_retries = 3
        attempts = 0
        while attempts < max_retries:
            attempts += 1
            try:
                client, model, provider = providers.get_routing_client(
                    skip_providers=skip_providers,
                    skip_keys=skip_keys
                )
            except Exception as exc:
                logger.warning("No healthy LLM provider for task planner: %s.", exc)
                break
                
            try:
                prompt = (
                    f"Analyze the following user query and decide if it is an explicit action task requiring a specialized subagent skill.\n"
                    f"User query: '{user_message}'\n\n"
                    f"Available skills:\n"
                    f"- 'web_search': Explicit requests to search online for real-time external web facts, news, live websites.\n"
                    f"- 'code_runner': Explicit requests to execute Python scripts, code blocks, complex computations.\n"
                    f"- 'writer': Explicit requests to compose long-form essays, drafts, technical documentation reports.\n"
                    f"- 'file_reader': Explicit requests to open and read local files on disk.\n\n"
                    f"CRITICAL RULE: Any question asking what you know, asking about past interactions, user preferences, "
                    f"personal details (e.g., pets, names, dogs, hobbies), general chat, or comments clarifying where information is stored "
                    f"are NOT subagent tasks. For all conversational questions or questions testing long-term memory, "
                    f"return exactly this JSON: {{\"is_task\": false, \"skill\": null, \"task_description\": null}}\n\n"
                    f"Otherwise, return JSON in this format:\n"
                    f"{{\"is_task\": true, \"skill\": \"<one of the skills above>\", \"task_description\": \"<cleaned task description>\"}}"
                )
                
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a JSON-only task planning assistant."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0
                )
                content = response.choices[0].message.content
                providers.record_success(provider)
                
                data = json.loads(content)
                if data.get("is_task"):
                    skill = data.get("skill")
                    task_desc = data.get("task_description") or user_message
                else:
                    skill = None
                llm_success = True
                break
            except Exception as exc:
                logger.warning("LLM task planner call failed on provider %s: %s. Retrying...", provider, exc)
                active_key = getattr(client, "key", None)
                if active_key:
                    skip_keys.setdefault(provider, []).append(active_key)
                else:
                    skip_providers.append(provider)
        
        if not llm_success:
            if len(matched_skills) > 1:
                skill = matched_skills[0]
            else:
                skill = None

    if not skill:
        return None

    # Resolve capability namespace if skill is web_search
    if skill == "web_search":
        if any(w in q for w in ["news", "headline", "latest", "today", "breaking"]):
            capability = "search.news"
        elif any(w in q for w in ["image", "photo", "picture", "logo"]):
            capability = "search.image"
        elif any(w in q for w in ["code", "repo", "github"]):
            capability = "search.code"
        elif any(w in q for w in ["map", "location", "near me"]):
            capability = "search.maps"
        else:
            capability = "search.web"

    return {
        "skill": skill,
        "capability": capability,
        "task_description": task_desc,
        "memory_context": memory_context,
        "prior_outcome": prior_outcome
    }
