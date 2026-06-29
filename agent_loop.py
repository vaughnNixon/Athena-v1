import os
import sys
import json
import logging
from pathlib import Path
import time
from rich.console import Console
import config
import providers
import retrieval
import distillation
import chunk_pipeline
import athena_compression
import learning_engine
import threading
import task_planner
import worker
import memory_gating

logger = logging.getLogger("athena.agent_loop")

class AthenaAgent:
    def __init__(self, project_id: str = "default", session_id: str = "session_1"):
        self.project_id = project_id.strip()
        self.session_id = session_id.strip()
        self.history_file = config.get_athena_home() / "sessions" / f"{self.session_id}.json"
        
        # Ensure sessions folder exists
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history = self._load_history()
        self.caveman_mode = False
        self.last_retrieval_info = {
            "query": "",
            "matched_chunk_ids": [],
            "retrieval_stage": "none",
            "timestamp": 0
        }
        self.last_retrieval_trace = None
        self.last_subagent_result = None
        self.last_subagent_gating = None
        
        from session_continuity import SessionContinuityLayer
        self.scl = SessionContinuityLayer(self.session_id, self.project_id)
        self._msg_index = len(self.history)
        self._snapshot_written = False
        
    def _load_history(self) -> list:
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Failed to load session history: %s", exc)
        return []
        
    def _save_history(self):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error("Failed to save session history: %s", exc)

    def run_one_turn(self, user_message: str, system_message: str = None) -> str:
        """
        Executes a single conversational turn with LLM-controlled memory retrieval.
        
        Flow:
        1. Call LLM with retrieve_memories tool available (but no memory pre-loaded)
        2. If LLM calls retrieve_memories tool:
           a. Execute memory retrieval
           b. Inject facts into chat history
           c. Call LLM again with facts present
        3. If LLM doesn't call tool:
           a. Return response immediately (no extra latency)
        """
        # Task Planner Routing
        plan = task_planner.plan(user_message, project_id=self.project_id)
        if plan:
            result = worker.execute(plan)
            self.last_subagent_result = result
            
            # Gate the memory payload
            gate_result = memory_gating.filter(result.memory_payload, result.aal_summary)
            self.last_subagent_gating = gate_result
            
            # Ingest approved items (synchronously in tests to avoid DB locks, otherwise in background thread)
            if "pytest" in sys.modules:
                chunk_pipeline.process_memory_payload(gate_result["accepted"], [self.project_id])
            else:
                threading.Thread(
                    target=chunk_pipeline.process_memory_payload,
                    args=(gate_result["accepted"], [self.project_id]),
                    daemon=True
                ).start()
            
            # Phase 2: Synthesize raw subagent result using LLM
            final_output = result.user_output
            if result.aal_summary.get("outcome") == "success" and result.user_output:
                try:
                    if self.caveman_mode:
                        system_instructions = (
                            "You are Athena. Extract the exact core answer from the tool execution data. "
                            "Respond exclusively in ultra-terse telegraphic caveman style (e.g. 'Anthropic new models Mythos, Fable.'). "
                            "Do NOT output generic acknowledgments like 'ACK' or raw search blocks."
                        )
                    else:
                        system_instructions = (
                            "You are Athena. Synthesize the raw tool execution data into a clear, natural, and comprehensive answer "
                            "directly addressing the user's query (e.g. 'Based on recent news, Anthropic's latest models are Mythos and Fable...'). "
                            "Do NOT output raw search blocks or repeating lists of snippets."
                        )
                    synth_messages = [
                        {"role": "system", "content": system_instructions},
                        {"role": "user", "content": f"User query: '{user_message}'\n\nRaw Skill Output:\n{result.user_output}"}
                    ]
                    synth_resp = self._call_llm_with_tools(messages=synth_messages, enable_tools=False)
                    if synth_resp.get("content"):
                        final_output = synth_resp["content"]
                except Exception as synth_exc:
                    logger.warning("Failed to synthesize subagent output via LLM: %s", synth_exc)
            elif result.aal_summary.get("outcome") == "failed":
                try:
                    synth_messages = [
                        {"role": "system", "content": "You are Athena, a helpful companion. The subagent task execution encountered an error or requested a skill that is not currently installed on this machine. Respond warmly, politely, and naturally in plain English explaining what you can or cannot do right now, without dumping raw technical Python error strings or internal class names."},
                        {"role": "user", "content": f"User query: '{user_message}'\n\nInternal Error Note: {result.user_output}"}
                    ]
                    synth_resp = self._call_llm_with_tools(messages=synth_messages, enable_tools=False)
                    if synth_resp.get("content"):
                        final_output = synth_resp["content"]
                except Exception as synth_exc:
                    logger.warning("Failed to synthesize subagent failure output: %s", synth_exc)

            # Store user message and synthesized result in history
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": final_output})
            self._save_history()
            
            return final_output

        # 1. Build system prompt
        if self.caveman_mode:
            base_system = (
                "Athena v1 is a memory-first agent.\n"
                "Defining trait: 'Athena remembers what others forget.'\n"
                "You have access to a retrieve_memories tool for searching long-term memory.\n"
                "Use this tool when you need context about past interactions or project details.\n"
                "Communicate only in terse, telegraphic, sparse prose (Caveman style).\n\n"
                "Style Toggle Instruction:\n"
                "Previous conversation may contain natural responses. Ignore previous writing style. "
                "Continue using the same facts, reasoning, and context, but from this point onward "
                "respond exclusively in sparse Caveman style."
            )
        else:
            base_system = (
                "You are Athena, a warm, intelligent, and natural conversational AI companion with long-term memory.\n"
                "Your defining trait is: 'Athena remembers what others forget.'\n"
                "You have access to a retrieve_memories tool that searches your long-term memory.\n"
                "Call retrieve_memories when you need context about past interactions, user preferences, or project details.\n"
                "Use memory to seamlessly personalize responses and remember details the user has shared.\n\n"
                "MEMORY ACCURACY & DIRECT ANSWER RULE:\n"
                "When the user asks for a specific detail (such as a name, date, or specific title), examine your retrieved memories carefully. "
                "If the exact detail requested is NOT in your memory, directly admit that you don't have that specific piece of information yet "
                "(e.g., 'I remember your dog is a brown Doberman mix with a lovely personality, but I don't know his name yet! What is his name?'). "
                "NEVER just list related facts while ignoring the exact question asked.\n\n"
                "CRITICAL BEHAVIOR RULE: When responding to casual greetings or check-ins (e.g. 'how are you', 'hello', 'what's up'), "
                "respond warmly and naturally as a companion (e.g. 'I'm doing great, thanks for asking! How are you doing today?'). "
                "NEVER list your technical capabilities, tools, or background features unless the user explicitly asks about how you work.\n\n"
                "Style Toggle Instruction:\n"
                "Previous conversation may contain Caveman responses. Treat them as factual summaries only. "
                "Continue the same conversation naturally from this point onward."
            )
        
        if system_message:
            base_system = f"{system_message}\n{base_system}"
        
        # Load persona files (identity.md, soul.md, user.md)
        persona = config.load_persona_files()
        persona_blocks = []
        if persona.get("identity"):
            persona_blocks.append(f"--- IDENTITY ---\n{persona['identity']}")
        if persona.get("soul"):
            persona_blocks.append(f"--- SOUL ---\n{persona['soul']}")
        if persona.get("user"):
            persona_blocks.append(f"--- USER PROFILE ---\n{persona['user']}")
            
        if persona_blocks:
            system_prompt = "\n\n".join(persona_blocks) + "\n\n" + base_system
        else:
            system_prompt = base_system

        # Check for personal entity profile mentions (Lucky, Ringgu, family, friends)
        import people_manager
        people_ctx = people_manager.get_relevant_people_context(user_message)
        if people_ctx:
            system_prompt = system_prompt + "\n\n" + people_ctx

        # Check for decision records context (ADRs)
        import decisions_manager
        decisions_ctx = decisions_manager.get_relevant_decisions_context(user_message)
        if decisions_ctx:
            system_prompt = system_prompt + "\n\n" + decisions_ctx

        
        # 2. Add user message to local history
        self.history.append({"role": "user", "content": user_message})
        
        # 3. Apply compression discipline
        self.history = athena_compression.run_caveman_summarization(self.history, self.project_id)
        compressed_history = athena_compression.compress_history_via_headroom(self.history)
        
        # 4. Assemble message payload with SCL Layer
        scl_ctx = self.scl.get_session_context()
        scl_sys_msgs = []
        if scl_ctx.get("summary"):
            scl_sys_msgs.append({"role": "system", "content": f"[ATHENA SESSION SUMMARY (AAL)]\n{scl_ctx['summary']}"})
        if scl_ctx.get("active_topics"):
            t_lines = [f"• {t['topic']} | score:{t['score']} | status:{t['status']} | priority:{t['priority']}" for t in scl_ctx["active_topics"]]
            scl_sys_msgs.append({"role": "system", "content": f"[ATHENA ACTIVE TOPICS]\n" + "\n".join(t_lines)})

        messages = [
            {"role": "system", "content": system_prompt}
        ] + scl_sys_msgs + compressed_history
        
        # 5. Execute API call with tool support (FIRST PASS)
        response = self._call_llm_with_tools(messages=messages)
        
        # 6. Handle tool calls (if any)
        if response.get("tool_calls"):
            # Indicate memory access to the user
            console = Console()
            console.print("[dim]Athena is recalling memories...[/dim]", end="\r")
            
            for tool_call in response["tool_calls"]:
                if tool_call["name"] == "retrieve_memories":
                    # Execute memory retrieval
                    query = tool_call["arguments"].get("query", user_message)
                    staged_result = retrieval.retrieve_memories_staged(
                        query=query,
                        scope_ids=[self.project_id]
                    )
                    
                    # Update stats and retrieval metadata
                    learning_engine.increment_query_count(retrieval.classify_query_intent(query))
                    self.last_retrieval_info = {
                        "query": query,
                        "matched_chunk_ids": staged_result.get("matched_chunk_ids", []),
                        "retrieval_stage": staged_result.get("retrieval_stage", "none"),
                        "timestamp": int(time.time())
                    }
                    self.last_retrieval_trace = staged_result.get("trace")
                    
                    # Format staged chunks into text block for LLM context
                    memories_block = _format_staged_result(staged_result)
                    
                    # Inject tool result into history
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call["name"],
                                    "arguments": json.dumps(tool_call["arguments"])
                                }
                            }
                        ]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": memories_block if memories_block else "No relevant memories found."
                    })
                    
                    # Call LLM again with memory context
                    response = self._call_llm_with_tools(messages=messages)
                elif tool_call["name"] == "report_correction":
                    user_corr = tool_call["arguments"].get("user_correction", user_message)
                    prev_user_msg = None
                    prev_assistant_content = None
                    for item in reversed(self.history):
                        if item.get("role") == "user" and prev_user_msg is None:
                            prev_user_msg = item.get("content", "")
                        elif item.get("role") == "assistant" and prev_assistant_content is None:
                            prev_assistant_content = item.get("content", "")
                    
                    if prev_user_msg and prev_assistant_content:
                        last_info = getattr(self, "last_retrieval_info", None) or {
                            "query": prev_user_msg,
                            "matched_chunk_ids": [],
                            "retrieval_stage": "none",
                            "timestamp": int(time.time()) - 10
                        }
                        learn_res = learning_engine.learn_from_feedback(
                            user_query=prev_user_msg,
                            user_correction=user_corr,
                            last_retrieval_info=last_info,
                            prev_response_text=prev_assistant_content
                        )
                        logger.info("Adaptive learning executed via tool call: %s", learn_res.get("explanation"))
                        staged_result = retrieval.retrieve_memories_staged(
                            query=prev_user_msg,
                            scope_ids=[self.project_id]
                        )
                        self.last_retrieval_info = {
                            "query": prev_user_msg,
                            "matched_chunk_ids": staged_result.get("matched_chunk_ids", []),
                            "retrieval_stage": staged_result.get("retrieval_stage", "none"),
                            "timestamp": int(time.time())
                        }
                        self.last_retrieval_trace = staged_result.get("trace")
                        memories_block = _format_staged_result(staged_result)
                        tool_output = f"[Athena has adjusted memory retrieval based on feedback]\nUpdated long-term memories retrieved:\n{memories_block}"
                    else:
                        tool_output = "Correction feedback recorded."

                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call["name"],
                                    "arguments": json.dumps(tool_call["arguments"])
                                }
                            }
                        ]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_output
                    })
                    response = self._call_llm_with_tools(messages=messages)
        
        # 7. Extract response content
        assistant_content = response.get("content", "ACK.")
        
        # 8. Save to history and trigger distillation
        history_entry = {"role": "assistant", "content": assistant_content}
        if response.get("codex_reasoning_items"):
            history_entry["codex_reasoning_items"] = response["codex_reasoning_items"]
        if response.get("codex_message_items"):
            history_entry["codex_message_items"] = response["codex_message_items"]
            
        self.history.append(history_entry)
        self._save_history()
        
        # 8.1 SCL Update & Context Pressure Check
        self._msg_index += 2
        self.scl.update_after_turn(user_message, assistant_content, self._msg_index)
        
        if "pytest" not in sys.modules:
            from background_queue import BackgroundQueue, JobType
            bq = BackgroundQueue()
            bq.enqueue(JobType.MEMORY_DISTILLATION, {
                "user_msg": user_message,
                "agent_msg": assistant_content,
                "scope_ids": [self.project_id]
            })
            bq.enqueue(JobType.SESSION_SUMMARY, {
                "session_id": self.session_id,
                "project_id": self.project_id,
                "history": self.history,
                "from_marker": self.scl.get_summary_marker()
            }, priority=5)

        pressure = self.scl.compute_context_pressure(messages)
        if pressure >= 0.95:
            console = Console()
            console.print("[yellow]💬 Context nearly full. Type /newchat to continue with full memory.[/yellow]")
        elif pressure >= 0.85:
            console = Console()
            console.print("[dim]⚠ Context at 85% — summarizing soon.[/dim]")
        
        # 9. Trigger async chunk ingestion from the latest turn
        def run_ingestion_thread():
            try:
                turn_messages = [
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": assistant_content}
                ]
                chunk_pipeline.process_conversation_to_chunks(turn_messages)
                logger.info("Chunk ingestion completed for turn.")
            except Exception as chunk_exc:
                logger.warning("Chunk ingestion failed (non-fatal): %s", chunk_exc)
        threading.Thread(target=run_ingestion_thread, daemon=True).start()
        
        return assistant_content

    def _call_llm_with_tools(self, messages: list, model: str = None, provider: str = None, enable_tools: bool = True) -> dict:
        """
        Execute a single LLM call with optional tool support.
        
        Returns dict with:
        - content: str (response text, if no tool call)
        - tool_calls: list (if LLM called tools)
        """
        skip_providers = []
        skip_keys = {}
        
        # Clean messages payload to keep only standard keys for completions API compatibility
        cleaned_messages = []
        for msg in messages:
            cleaned_msg = {
                "role": msg.get("role"),
                "content": msg.get("content")
            }
            if "name" in msg:
                cleaned_msg["name"] = msg["name"]
            if "tool_calls" in msg:
                cleaned_msg["tool_calls"] = msg["tool_calls"]
            if "tool_call_id" in msg:
                cleaned_msg["tool_call_id"] = msg["tool_call_id"]
            cleaned_messages.append(cleaned_msg)
            
        while True:
            try:
                client, model, provider = providers.get_routing_client(
                    skip_providers=skip_providers,
                    skip_keys=skip_keys
                )
            except Exception as exc:
                logger.critical("No providers or keys left: %s", exc)
                raise RuntimeError(f"Athena API call failed on all providers: {exc}") from exc
            
            logger.info("Executing LLM call: Provider=%s, Model=%s", provider, model)
            
            try:
                active_key = getattr(client, "key", None)
                
                try:
                    if enable_tools:
                        tools = [
                            get_retrieve_memories_tool_definition(),
                            get_report_correction_tool_definition()
                        ]
                        response = client.chat.completions.create(
                            model=model,
                            messages=cleaned_messages,
                            tools=tools,
                            tool_choice="auto",
                            temperature=0.2
                        )
                        use_tools = True
                    else:
                        response = client.chat.completions.create(
                            model=model,
                            messages=cleaned_messages,
                            temperature=0.2
                        )
                        use_tools = False
                except Exception as tool_exc:
                    tool_exc_str = str(tool_exc).lower()
                    if any(term in tool_exc_str for term in ["tool", "function", "unsupported", "400"]):
                        logger.warning("Provider %s does not support tool calling: %s. Falling back to pre-retrieval.", provider, tool_exc)
                        
                        user_msg = messages[-1]["content"] if messages and messages[-1]["role"] == "user" else ""
                        staged_result = retrieval.retrieve_memories_staged(
                            query=user_msg,
                            scope_ids=[self.project_id]
                        )
                        
                        # Update stats and retrieval metadata
                        learning_engine.increment_query_count(retrieval.classify_query_intent(user_msg))
                        self.last_retrieval_info = {
                            "query": user_msg,
                            "matched_chunk_ids": staged_result.get("matched_chunk_ids", []),
                            "retrieval_stage": staged_result.get("retrieval_stage", "none"),
                            "timestamp": int(time.time())
                        }
                        self.last_retrieval_trace = staged_result.get("trace")
                        
                        memories_block = _format_staged_result(staged_result)
                        fallback_messages = list(cleaned_messages)
                        if memories_block:
                            sys_msg = fallback_messages[0].copy()
                            sys_msg["content"] = f"{sys_msg['content']}\n\n[ATHENA FALLBACK MEMORY]\n{memories_block}"
                            fallback_messages[0] = sys_msg
                            
                        response = client.chat.completions.create(
                            model=model,
                            messages=fallback_messages,
                            temperature=0.2
                        )
                        use_tools = False
                    else:
                        raise tool_exc
                
                msg_obj = response.choices[0].message
                tool_calls_raw = getattr(msg_obj, "tool_calls", None) if use_tools else None
                tool_calls = []
                if tool_calls_raw:
                    for tc in tool_calls_raw:
                        tool_calls.append({
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": json.loads(tc.function.arguments)
                        })
                
                content = getattr(msg_obj, "content", None)
                content = content.strip() if content else ""
                
                codex_reasoning_items = getattr(msg_obj, "codex_reasoning_items", None)
                codex_message_items = getattr(msg_obj, "codex_message_items", None)
                
                res_dict = {
                    "content": content or "ACK.",
                    "tool_calls": tool_calls
                }
                if codex_reasoning_items:
                    res_dict["codex_reasoning_items"] = codex_reasoning_items
                if codex_message_items:
                    res_dict["codex_message_items"] = codex_message_items
                return res_dict
                
            except Exception as exc:
                logger.warning("LLM execution failed for provider '%s': %s. Retrying with failover...", provider, exc)
                if not getattr(self, "_snapshot_written", False):
                    try:
                        sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
                        snap_id = self.scl.create_rotation_snapshot(
                            trigger="provider_failure",
                            identity=sys_prompt,
                            recent_context=self.history[-6:] if self.history else [],
                            provider_from=provider
                        )
                        from background_queue import BackgroundQueue, JobType
                        BackgroundQueue().enqueue(JobType.ROTATION_SNAPSHOT, {"snap_id": snap_id, "session_id": self.session_id}, priority=1)
                        self._snapshot_written = True
                    except Exception as snap_exc:
                        logger.warning("Failed to create rotation snapshot: %s", snap_exc)

                active_key = getattr(client, "key", None)
                if active_key:
                    skip_keys.setdefault(provider, []).append(active_key)
                else:
                    skip_providers.append(provider)

def get_retrieve_memories_tool_definition() -> dict:
    """
    Returns the OpenAI-compatible tool definition for memory retrieval.
    Used by the LLM to request memory search when needed.
    """
    return {
        "type": "function",
        "function": {
            "name": "retrieve_memories",
            "description": (
                "Retrieve relevant long-term memories based on a query. "
                "Use this when you need context about past interactions, user preferences, "
                "project details, or any information stored in your long-term memory. "
                "Call this tool only if the query genuinely requires past knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A concise search query describing what you need to remember. "
                            "Example: 'what dental clinic project did we start' or 'user budget preferences'"
                        )
                    }
                },
                "required": ["query"]
            }
        }
    }


def get_report_correction_tool_definition() -> dict:
    """
    Returns the OpenAI-compatible tool definition for user feedback correction.
    Used by the LLM when the user explicitly points out an error in previous turn.
    """
    return {
        "type": "function",
        "function": {
            "name": "report_correction",
            "description": (
                "Call this tool ONLY when the user explicitly points out a mistake, error, or incorrect fact in your previous response. "
                "Do NOT call this tool for casual greetings, general questions, or conversation. "
                "Use this tool to trigger adaptive learning and update memory weights when you made an error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_correction": {
                        "type": "string",
                        "description": "The specific correction provided by the user."
                    }
                },
                "required": ["user_correction"]
            }
        }
    }


def _format_staged_result(staged_result: dict) -> str:
    """
    Converts a staged retrieval result dict into a formatted text block
    suitable for LLM context injection.
    """
    chunks = staged_result.get("supplied_chunks", [])
    if not chunks:
        return staged_result.get("message", "")
    
    lines = ["[ATHENA MEMORY]"]
    for chunk in chunks:
        text = chunk.get("caveman_text") or chunk.get("raw_text", "")
        if text:
            lines.append(f"• {text}")
    
    formatted = "\n".join(lines)
    
    # Headroom check: Limit injection size to ~500 tokens (roughly 2000 chars)
    while len(formatted) > 2000 and len(chunks) > 1:
        chunks.pop()
        lines = ["[ATHENA MEMORY]"]
        for chunk in chunks:
            text = chunk.get("caveman_text") or chunk.get("raw_text", "")
            if text:
                lines.append(f"• {text}")
        formatted = "\n".join(lines)
    
    return formatted
