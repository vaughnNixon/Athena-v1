import os
import json
import logging
from pathlib import Path
import config
import providers
import retrieval
import distillation
import athena_compression

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
        """Executes a single conversational turn with LLM-controlled memory retrieval."""
        # 1. Build system prompt instructing the LLM that it has a memory retrieval tool
        if self.caveman_mode:
            base_system = (
                "Athena v1 is a memory-first agent.\n"
                "Defining trait: 'Athena remembers what others forget.'\n"
                "Athena v1 is NOT a chatbot. It is a long-term memory layer.\n"
                "Communicate only in terse, telegraphic, sparse prose (Caveman style).\n"
                "Remove all conversational fillers, intros, and greetings.\n"
                "You have access to a memory retrieval tool 'retrieve_memories'. If you need to recall past context, preferences, or project details to answer, use it."
            )
        else:
            base_system = (
                "You are Athena v1, a helpful, friendly, and natural conversational AI assistant with long-term memory capabilities.\n"
                "Your defining trait is: 'Athena remembers what others forget.'\n"
                "You have access to a memory retrieval tool 'retrieve_memories'. If the user asks about something you might have in memory (such as past conversations, user preferences, names, or project history), use the tool to retrieve relevant details before answering."
            )
        
        if system_message:
            base_system = f"{system_message}\n{base_system}"
            
        system_prompt = base_system
            
        # 2. Add user message to local history
        self.history.append({"role": "user", "content": user_message})
        
        # 3. Apply compression discipline
        # A. Caveman turn history summarization (triggers if history is > 1000 tokens)
        self.history = athena_compression.run_caveman_summarization(self.history, self.project_id)
        
        # B. Headroom dynamic compression (reversibly crushes logs/JSON/RAG chunks)
        compressed_history = athena_compression.compress_history_via_headroom(self.history)
        
        # Assemble message payload
        messages = [
            {"role": "system", "content": system_prompt}
        ] + compressed_history
        
        # Define memory retrieval tool spec
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "retrieve_memories",
                    "description": "Retrieve relevant long-term memories, facts, preferences, and project details from the database matching the given query.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query to match against memories."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]
        
        # 4. Execute API call with rotational failover
        skip_providers = []
        skip_keys = {}
        
        while True:
            try:
                client, model, provider = providers.get_routing_client(
                    skip_providers=skip_providers,
                    skip_keys=skip_keys
                )
            except Exception as exc:
                logger.critical("No providers or keys left to try: %s", exc)
                raise RuntimeError(f"Athena API call failed on all providers: {exc}") from exc
                
            logger.info("Executing LLM call using: Provider=%s, Model=%s", provider, model)
            
            try:
                active_key = getattr(client, "key", None)
                
                # Try calling with tools enabled first.
                # If tool calling is not supported by the provider, fall back to pre-retrieval.
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.2,
                        tools=tools
                    )
                    use_tools = True
                except Exception as tool_exc:
                    tool_exc_str = str(tool_exc).lower()
                    if "tool" in tool_exc_str or "function" in tool_exc_str or "unsupported" in tool_exc_str or "400" in tool_exc_str:
                        logger.warning("Provider %s does not support tool calling: %s. Falling back to pre-retrieval.", provider, tool_exc)
                        
                        # Pre-retrieve memories as fallback
                        memories_block = retrieval.retrieve_relevant_memories(
                            query=user_message,
                            scope_ids=[self.project_id],
                            limit=5
                        )
                        fallback_messages = list(messages)
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
                tool_calls = getattr(msg_obj, "tool_calls", None) if use_tools else None
                
                if tool_calls:
                    # Indicate memory access to the user
                    from rich.console import Console
                    console = Console()
                    console.print("[dim]Athena is recalling memories...[/dim]", end="\r")
                    
                    # Create ephemeral messages log for second completions turn
                    ephemeral_messages = list(messages)
                    
                    # Append assistant message with tool calls
                    assistant_msg = {
                        "role": "assistant",
                        "content": msg_obj.content
                    }
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        } for tc in tool_calls
                    ]
                    ephemeral_messages.append(assistant_msg)
                    
                    # Process tool calls
                    for tool_call in tool_calls:
                        if tool_call.function.name == "retrieve_memories":
                            import json
                            try:
                                args = json.loads(tool_call.function.arguments)
                                q = args.get("query", user_message)
                            except Exception:
                                q = user_message
                                
                            memories_block = retrieval.retrieve_relevant_memories(
                                query=q,
                                scope_ids=[self.project_id],
                                limit=5
                            )
                            ephemeral_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": "retrieve_memories",
                                "content": memories_block or "No memories found."
                            })
                            
                    # Make final LLM completions call
                    response2 = client.chat.completions.create(
                        model=model,
                        messages=ephemeral_messages,
                        temperature=0.2
                    )
                    msg_obj = response2.choices[0].message
                
                raw_content = getattr(msg_obj, "content", None)
                assistant_content = raw_content.strip() if raw_content else "ACK."
                
                # Append success response to history and save
                history_entry = {"role": "assistant", "content": assistant_content}
                if getattr(msg_obj, "codex_reasoning_items", None):
                    history_entry["codex_reasoning_items"] = msg_obj.codex_reasoning_items
                if getattr(msg_obj, "codex_message_items", None):
                    history_entry["codex_message_items"] = msg_obj.codex_message_items
                    
                self.history.append(history_entry)
                self._save_history()
                
                # 5. Trigger non-blocking distillation worker
                distillation.enqueue_distillation(
                    user_msg=user_message,
                    agent_msg=assistant_content,
                    scope_ids=[self.project_id]
                )
                
                return assistant_content
                
            except Exception as exc:
                logger.warning("LLM execution failed for provider '%s': %s. Retrying with failover...", provider, exc)
                if active_key:
                    skip_keys.setdefault(provider, []).append(active_key)
                else:
                    skip_providers.append(provider)

