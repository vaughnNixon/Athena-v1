import os
import sys
import argparse
import logging
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.prompt import Confirm
import config
import memory_engine
import diagnostics
import copilot_auth
from agent_loop import AthenaAgent

console = Console()

def setup_logger():
    home = config.get_athena_home()
    config.ensure_athena_dirs()
    log_file = home / "logs" / "agent.log"
    
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        encoding="utf-8"
    )

def run_onboarding():
    from providers_manager import get_manager
    console.print("\n[bold gold3]Athena v1 — Interactive Setup Wizard[/bold gold3]\n")
    console.print("This wizard will configure your model providers and credentials.\n")
    
    mgr = get_manager()
    
    while True:
        console.print("\n[bold cyan]Main Setup Menu[/bold cyan]")
        console.print("  [bold cyan]1[/bold cyan]: Configure Gemini Keys")
        console.print("  [bold cyan]2[/bold cyan]: Configure OpenAI-compatible Keys")
        console.print("  [bold cyan]3[/bold cyan]: Log in to GitHub Copilot (Keyless)")
        console.print("  [bold cyan]4[/bold cyan]: Configure Search & Service Keys (Tavily, Brave, etc.)")
        console.print("  [bold cyan]5[/bold cyan]: Select active default provider")
        console.print("  [bold cyan]6[/bold cyan]: Exit Setup Wizard")
        
        choice = Prompt.ask("Select an option", choices=["1", "2", "3", "4", "5", "6"], default="6")
        
        if choice == "6":
            console.print("\n[bold red]Exiting Setup Wizard.[/bold red]\n")
            break
            
        elif choice == "3":
            console.print("\n[bold cyan]Starting GitHub OAuth Device Code Flow...[/bold cyan]")
            token = copilot_auth.copilot_device_code_login()
            if token:
                env_file = config.get_athena_home() / ".env"
                env_lines = []
                if env_file.exists():
                    env_lines = env_file.read_text(encoding="utf-8").splitlines()
                
                env_lines = [l for l in env_lines if not l.strip().startswith("COPILOT_GITHUB_TOKEN=")]
                env_lines.append(f"COPILOT_GITHUB_TOKEN={token}")
                env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
                
                console.print("\n[bold green][OK] GitHub Copilot login successful. Token saved to .env.[/bold green]\n")
            else:
                console.print("\n[bold red][FAIL] GitHub Copilot login failed or timed out.[/bold red]\n")
                
        elif choice == "4":
            from service_providers_manager import get_service_manager
            sm = get_service_manager()
            console.print("\n[bold gold3]Configure Search & Service Provider Keys[/bold gold3]")
            search_providers = [p for p in sm.providers.values() if p.category == "search"]
            choices = []
            for i, p in enumerate(search_providers, start=1):
                idx_str = str(i)
                choices.append(idx_str)
                keys_count = len(p.api_keys)
                console.print(f"  [bold cyan]{idx_str}[/bold cyan]: {p.name} ({keys_count} keys configured)")
            back_idx = str(len(search_providers) + 1)
            choices.append(back_idx)
            console.print(f"  [bold cyan]{back_idx}[/bold cyan]: Back to main menu")
            
            sel = Prompt.ask("Choose search provider to configure", choices=choices, default=back_idx)
            if sel != back_idx:
                selected_p = search_providers[int(sel) - 1]
                key_in = Prompt.ask(f"Enter API key for {selected_p.name}", password=True).strip()
                if key_in:
                    if key_in not in selected_p.api_keys:
                        selected_p.api_keys.append(key_in)
                    selected_p.enabled = True
                    sm.save_providers()
                    console.print(f"[bold green]Key saved! Enabled search provider '{selected_p.name}'.[/bold green]\n")
                else:
                    console.print("[yellow]No key entered. Unchanged.[/yellow]\n")

        elif choice == "5":
            console.print("\n[bold gold3]Select Active Default Provider[/bold gold3]")
            active_choices = ["auto"]
            for pid, p in mgr.providers.items():
                if p.enabled:
                    active_choices.append(pid)
            
            current_active = mgr.active_provider_id or "auto"
            selected_active = Prompt.ask("Choose active provider", choices=active_choices, default=current_active)
            if selected_active == "auto":
                mgr.active_provider_id = None
            else:
                mgr.active_provider_id = selected_active
            mgr.save_providers()
            console.print(f"[bold green]Active default provider set to: {selected_active}[/bold green]\n")
            
        elif choice in ("1", "2"):
            ptype = "gemini" if choice == "1" else "openai_compatible"
            ptype_name = "Gemini" if choice == "1" else "OpenAI-compatible"
            
            while True:
                console.print(f"\n[bold cyan]{ptype_name} Key Configuration[/bold cyan]")
                
                # Fetch matching providers
                matching_providers = [p for p in mgr.providers.values() if p.type == ptype]
                
                sub_choices = ["1"]
                console.print("  [bold cyan]1[/bold cyan]: Add a new provider")
                
                for i, p in enumerate(matching_providers, start=2):
                    sub_idx = str(i)
                    sub_choices.append(sub_idx)
                    console.print(f"  [bold cyan]{sub_idx}[/bold cyan]: {p.name} (has {len(p.api_keys)} keys)")
                    
                back_idx = str(len(matching_providers) + 2)
                sub_choices.append(back_idx)
                console.print(f"  [bold cyan]{back_idx}[/bold cyan]: Back to main menu")
                
                sub_choice = Prompt.ask("Choose a sub-option", choices=sub_choices, default=back_idx)
                
                if sub_choice == back_idx:
                    break
                    
                elif sub_choice == "1":
                    console.print(f"\n[bold gold3]Add New {ptype_name} Provider[/bold gold3]")
                    name = Prompt.ask("Provider Name (e.g. Grok)").strip()
                    if not name:
                        console.print("[red]Cancelled: Provider Name is required.[/red]")
                        continue
                        
                    default_url = "https://generativelanguage.googleapis.com/v1beta/openai/" if ptype == "gemini" else "https://api.x.ai/v1"
                    base_url = Prompt.ask("Base URL", default=default_url)
                    
                    default_model_choice = "gemini-2.5-flash" if ptype == "gemini" else "grok-4"
                    default_model = Prompt.ask("Default Model", default=default_model_choice)
                    
                    api_keys = []
                    console.print("Enter API keys. Press Enter on an empty line when finished:")
                    while True:
                        k = Prompt.ask("Add Key", password=True)
                        if not k:
                            break
                        api_keys.append(k.strip())
                        
                    new_p = mgr.add_provider(
                        name=name,
                        type=ptype,
                        base_url=base_url,
                        default_model=default_model,
                        api_keys=api_keys
                    )
                    console.print(f"\n[bold green]Successfully added provider '{new_p.name}' (id: {new_p.id})[/bold green]\n")
                    
                else:
                    selected_idx = int(sub_choice) - 2
                    p = matching_providers[selected_idx]
                    
                    console.print(f"\n[bold gold3]Configure Existing Provider: {p.name}[/bold gold3]")
                    console.print(f"Base URL: [dim]{p.base_url}[/dim]")
                    console.print(f"Default Model: [dim]{p.default_model}[/dim]")
                    console.print(f"Currently has {len(p.api_keys)} keys.")
                    
                    api_keys = []
                    console.print("Enter API keys to append to this provider. Press Enter on an empty line when finished:")
                    while True:
                        k = Prompt.ask("Add Key", password=True)
                        if not k:
                            break
                        api_keys.append(k.strip())
                    
                    if api_keys:
                        p.api_keys.extend(api_keys)
                        for key in api_keys:
                            if key not in p.key_stats:
                                p.key_stats[key] = {
                                    "failures": 0,
                                    "successes": 0,
                                    "last_success": None,
                                    "last_failure": None
                                }
                        mgr.save_providers()
                        console.print(f"\n[bold green]Successfully appended {len(api_keys)} keys to '{p.name}'.[/bold green]\n")
                    else:
                        console.print("\n[yellow]No new keys added.[/yellow]\n")

def run_chat_loop(project_id: str, session_id: str):
    from rich.table import Table
    from providers_manager import get_manager
    setup_logger()
    memory_engine.initialize_db()
    
    import memory_sweep
    try:
        memory_sweep.run_memory_sweep()
    except Exception as e:
        logger.error("Failed to run startup memory sweep: %s", e)
    
    # Try fetching Caveman skills in background if they don't exist
    config.fetch_caveman_skills(force=False)
    
    mgr = get_manager()
    active_prov_id = mgr.active_provider_id
    if active_prov_id:
        p = mgr.providers.get(active_prov_id)
        prov_name = p.name if p else active_prov_id
        model_name = mgr.active_model_override or (p.default_model if p else "")
    else:
        healthiest = mgr.get_healthiest_provider()
        prov_name = f"Auto ({healthiest.name})" if healthiest else "Auto"
        model_name = mgr.active_model_override or (healthiest.default_model if healthiest else "")
    
    console.print(Panel(
        f"[bold gold3]Athena v1 Interactive Chat Shell[/bold gold3]\n"
        f"[dim]Project Namespace: [cyan]{project_id}[/cyan] | Session: [cyan]{session_id}[/cyan][/dim]\n"
        f"[dim]Active Provider: [green]{prov_name}[/green] | Model: [green]{model_name}[/green][/dim]\n"
        f"Type [bold red]/quit[/bold red] or [bold red]/exit[/bold red] to end the session.",
        title="Athena v1"
    ))
    
    agent = AthenaAgent(project_id=project_id, session_id=session_id)
    
    # Print database stats on startup
    stats = memory_engine.get_diagnostics_stats()
    console.print(f"[dim]Loaded database: {stats['total_facts']} facts total ({stats['active_facts']} active, {stats['archived_facts']} decayed).[/dim]\n")
    
    while True:
        try:
            user_input = Prompt.ask("\n[bold green]User[/bold green]")
            stripped_input = user_input.strip()
            
            if not stripped_input:
                continue
                
            if stripped_input.lower() in {"/quit", "/exit", "quit", "exit"}:
                # Run a final memory sweep to tier any new chunks before exiting
                try:
                    memory_sweep.run_memory_sweep()
                    console.print("[dim]Memory sweep completed.[/dim]")
                except Exception as e:
                    logger.error("Exit sweep failed: %s", e)
                console.print("\n[bold red]Exiting Athena session. Goodbye.[/bold red]\n")
                break
                
            if stripped_input.lower() == "/caveman":
                agent.caveman_mode = not agent.caveman_mode
                status = "ON" if agent.caveman_mode else "OFF"
                color = "green" if agent.caveman_mode else "yellow"
                console.print(f"[bold {color}]Caveman style toggled {status}.[/bold {color}]")
                continue
                
            cmd_lower = stripped_input.lower()
            if cmd_lower == "/providers":
                mgr = get_manager()
                table = Table(title="Athena Providers Configuration", title_style="bold gold3")
                table.add_column("ID", style="cyan")
                table.add_column("Name", style="bold white")
                table.add_column("Type", style="green")
                table.add_column("Default Model", style="yellow")
                table.add_column("Keys Count", justify="right")
                table.add_column("Enabled", style="bold")
                table.add_column("Stats (Success/Fail/Consec)", style="dim")
                table.add_column("Active", style="bold magenta")
                
                for pid, p in mgr.providers.items():
                    is_active = ""
                    if mgr.active_provider_id == pid:
                        is_active = "★"
                    elif not mgr.active_provider_id:
                        healthiest = mgr.get_healthiest_provider()
                        if healthiest and healthiest.id == pid:
                            is_active = "★ (Auto)"
                    
                    status_str = "[green]Yes[/green]" if p.enabled else "[red]No[/red]"
                    keys_count = len(p.api_keys) if p.api_keys else 0
                    s = p.stats
                    stats_str = f"{s['successful_requests']}/{s['failed_requests']}/{s.get('consecutive_failures', 0)}"
                    
                    table.add_row(
                        p.id,
                        p.name,
                        p.type,
                        p.default_model,
                        str(keys_count),
                        status_str,
                        stats_str,
                        is_active
                    )
                console.print(table)
                if mgr.active_model_override:
                    console.print(f"[dim]Model override is active: [green]{mgr.active_model_override}[/green]. Use [bold]/model select default[/bold] to clear.[/dim]")
                continue
                
            if cmd_lower.startswith("/provider"):
                mgr = get_manager()
                parts = stripped_input.split()
                
                if len(parts) == 1:
                    active_p_id = mgr.active_provider_id
                    active_p = mgr.providers.get(active_p_id) if active_p_id else None
                    if active_p:
                        model_name = mgr.active_model_override or active_p.default_model
                        console.print(f"Current provider: [green]{active_p.name}[/green] (model: [green]{model_name}[/green])")
                    else:
                        healthiest = mgr.get_healthiest_provider()
                        if healthiest:
                            model_name = mgr.active_model_override or healthiest.default_model
                            console.print(f"Current provider: [green]Auto ({healthiest.name})[/green] (model: [green]{model_name}[/green])")
                        else:
                            console.print("[yellow]No active or healthy provider available.[/yellow]")
                    continue
                
                subcmd = parts[1].lower()
                
                if subcmd == "add":
                    console.print("\n[bold gold3]Add New AI Provider[/bold gold3]")
                    name = Prompt.ask("Provider Name (e.g. Grok)").strip()
                    if not name:
                        console.print("[red]Cancelled: Provider Name is required.[/red]")
                        continue
                        
                    ptype = Prompt.ask("Provider Type", choices=["openai_compatible", "gemini"], default="openai_compatible")
                    base_url = Prompt.ask("Base URL", default="https://api.x.ai/v1" if ptype == "openai_compatible" else "https://generativelanguage.googleapis.com/v1beta/openai/")
                    default_model = Prompt.ask("Default Model")
                    
                    api_keys = []
                    console.print("Enter API keys. Press Enter on an empty line when finished:")
                    while True:
                        k = Prompt.ask("Add Key", password=True)
                        if not k:
                            break
                        api_keys.append(k.strip())
                        
                    new_p = mgr.add_provider(
                        name=name,
                        type=ptype,
                        base_url=base_url,
                        default_model=default_model,
                        api_keys=api_keys
                    )
                    console.print(f"\n[bold green]Successfully added provider '{new_p.name}' (id: {new_p.id})[/bold green]\n")
                    continue
                    
                elif subcmd == "remove":
                    if len(parts) < 3:
                        console.print("[red]Usage: /provider remove <provider_id>[/red]")
                        continue
                    pid = parts[2].strip()
                    if mgr.remove_provider(pid):
                        console.print(f"[bold green]Provider '{pid}' removed successfully.[/bold green]")
                    else:
                        console.print(f"[bold red]Provider '{pid}' not found.[/bold red]")
                    continue
                    
                elif subcmd == "enable":
                    if len(parts) < 3:
                        console.print("[red]Usage: /provider enable <provider_id>[/red]")
                        continue
                    pid = parts[2].strip()
                    if mgr.enable_provider(pid, True):
                        console.print(f"[bold green]Provider '{pid}' enabled.[/bold green]")
                    else:
                        console.print(f"[bold red]Provider '{pid}' not found.[/bold red]")
                    continue
                    
                elif subcmd == "disable":
                    if len(parts) < 3:
                        console.print("[red]Usage: /provider disable <provider_id>[/red]")
                        continue
                    pid = parts[2].strip()
                    if mgr.enable_provider(pid, False):
                        console.print(f"[bold green]Provider '{pid}' disabled.[/bold green]")
                    else:
                        console.print(f"[bold red]Provider '{pid}' not found.[/bold red]")
                    continue
                    
                elif subcmd == "select":
                    if len(parts) < 3:
                        console.print("[red]Usage: /provider select <provider_id|auto>[/red]")
                        continue
                    pid = parts[2].strip().lower()
                    if pid in ("auto", "dynamic"):
                        mgr.active_provider_id = None
                        mgr.active_model_override = None
                        mgr.save_providers()
                        console.print("[bold green]Switched active provider selection to Auto (Dynamic Health).[/bold green]")
                    elif pid in mgr.providers:
                        mgr.active_provider_id = pid
                        mgr.active_model_override = None
                        mgr.save_providers()
                        p = mgr.providers[pid]
                        console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model}).[/bold green]")
                    else:
                        import providers
                        mapped = providers.map_legacy_provider_id(pid)
                        if mapped in mgr.providers:
                            mgr.active_provider_id = mapped
                            mgr.active_model_override = None
                            mgr.save_providers()
                            p = mgr.providers[mapped]
                            console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model}).[/bold green]")
                        else:
                            console.print(f"[bold red]Provider '{pid}' not found.[/bold red]")
                    continue
                    
                else:
                    pid = parts[1].strip().lower()
                    if pid in ("auto", "dynamic"):
                        mgr.active_provider_id = None
                        mgr.active_model_override = None
                        mgr.save_providers()
                        console.print("[bold green]Switched active provider selection to Auto (Dynamic Health).[/bold green]")
                    elif pid in mgr.providers:
                        mgr.active_provider_id = pid
                        mgr.active_model_override = None
                        mgr.save_providers()
                        p = mgr.providers[pid]
                        console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model}).[/bold green]")
                    else:
                        import providers
                        mapped = providers.map_legacy_provider_id(pid)
                        if mapped in mgr.providers:
                            mgr.active_provider_id = mapped
                            mgr.active_model_override = None
                            mgr.save_providers()
                            p = mgr.providers[mapped]
                            console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model}).[/bold green]")
                        else:
                            console.print(f"[bold red]Unknown provider sub-command or ID: {pid}[/bold red]")
                    continue
            
            if cmd_lower.startswith("/model"):
                mgr = get_manager()
                parts = stripped_input.split()
                
                if len(parts) == 1:
                    active_p_id = mgr.active_provider_id
                    active_p = mgr.providers.get(active_p_id) if active_p_id else mgr.get_healthiest_provider()
                    if active_p:
                        model_name = mgr.active_model_override or active_p.default_model
                        console.print(f"Current active model: [green]{model_name}[/green]")
                    else:
                        console.print("[yellow]No active provider available.[/yellow]")
                    continue
                    
                subcmd = parts[1].lower()
                if subcmd == "select":
                    if len(parts) < 3:
                        console.print("[red]Usage: /model select <model_id|default>[/red]")
                        continue
                    model_id = parts[2].strip()
                    if model_id.lower() in ("default", "auto", "reset"):
                        mgr.active_model_override = None
                        mgr.save_providers()
                        console.print("[bold green]Cleared model override. Using provider's default model.[/bold green]")
                    else:
                        mgr.active_model_override = model_id
                        mgr.save_providers()
                        console.print(f"[bold green]Model override set to '{model_id}'.[/bold green]")
                    continue
                else:
                    model_id = parts[1].strip()
                    if model_id.lower() in ("default", "auto", "reset"):
                        mgr.active_model_override = None
                        mgr.save_providers()
                        console.print("[bold green]Cleared model override. Using provider's default model.[/bold green]")
                    else:
                        mgr.active_model_override = model_id
                        mgr.save_providers()
                        console.print(f"[bold green]Model override set to '{model_id}'.[/bold green]")
                    continue
                    
            if cmd_lower == "/rollback":
                import learning_engine
                learning_engine.reset_skip_marks()
                learning_engine.reset_query_statistics()
                console.print("[bold green]Successfully reset all retrieval skip marks and query category statistics.[/bold green]")
                continue

            if cmd_lower == "/topics":
                from rich.table import Table
                topics = agent.scl.get_active_topics(top_n=10)
                if not topics:
                    console.print("[dim]No active topics being tracked.[/dim]")
                else:
                    t_table = Table(title="Active Session Topics", title_style="bold cyan")
                    t_table.add_column("Topic", style="white")
                    t_table.add_column("Score", justify="right", style="green")
                    t_table.add_column("Status", style="yellow")
                    t_table.add_column("Priority", style="magenta")
                    t_table.add_column("Mentions", justify="right", style="cyan")
                    for t in topics:
                        t_table.add_row(t["topic"], f"{t['score']:.2f}", t["status"], t["priority"], str(t["mention_count"]))
                    console.print(t_table)
                continue

            if cmd_lower.startswith("/pin"):
                parts = stripped_input.split(maxsplit=1)
                if len(parts) < 2:
                    console.print("[red]Usage: /pin <topic_name>[/red]")
                else:
                    t_name = parts[1].strip()
                    agent.scl.pin_topic(t_name)
                    console.print(f"[bold green]Topic '{t_name}' is now PINNED.[/bold green]")
                continue

            if cmd_lower.startswith("/unpin"):
                parts = stripped_input.split(maxsplit=1)
                if len(parts) < 2:
                    console.print("[red]Usage: /unpin <topic_name>[/red]")
                else:
                    t_name = parts[1].strip()
                    agent.scl.unpin_topic(t_name)
                    console.print(f"[bold green]Topic '{t_name}' unpinned (reset to NORMAL).[/bold green]")
                continue

            if cmd_lower == "/newchat":
                handoff_data = agent.scl.export_for_new_chat()
                agent.scl.archive_session()
                new_session_id = f"session_{int(time.time())}"
                console.print(f"[bold green]Session archived. Starting new chat session '{new_session_id}'...[/bold green]")
                agent = AthenaAgent(project_id=project_id, session_id=new_session_id)
                agent.scl._write_session(
                    summary=handoff_data["summary"],
                    summary_version=handoff_data["summary_version"],
                    summary_marker=handoff_data["summary_marker"],
                    context_pressure=0.0
                )
                console.print("[bold green]✓ Context carried over seamlessly into new chat.[/bold green]")
                continue
                
            if cmd_lower == "/learning":
                from rich.table import Table
                import learning_engine
                
                conn = memory_engine.get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT query_type, total_queries, corrected_queries, accuracy FROM query_statistics")
                    rows = cursor.fetchall()
                    if not rows:
                        console.print("[dim]No query statistics logged yet.[/dim]")
                    else:
                        table = Table(title="Athena Adaptive Retrieval Statistics", title_style="bold cyan")
                        table.add_column("Query Type", style="yellow")
                        table.add_column("Total Queries", justify="right", style="white")
                        table.add_column("Corrected Queries", justify="right", style="white")
                        table.add_column("Accuracy", justify="right")
                        
                        for row in rows:
                            qtype, total, corrected, acc = row
                            acc_color = "green" if acc >= 0.8 else "yellow" if acc >= 0.5 else "red"
                            table.add_row(qtype, str(total), str(corrected), f"[bold {acc_color}]{acc * 100:.1f}%[/bold {acc_color}]")
                        console.print(table)
                        
                    cursor.execute("SELECT COUNT(*) FROM skip_marks")
                    skip_count = cursor.fetchone()[0]
                    console.print(f"[dim]Total active retrieval skip marks (penalized chunks): [bold white]{skip_count}[/bold white][/dim]")
                    
                except Exception as exc:
                    console.print(f"[red]Failed to fetch statistics: {exc}[/red]")
                finally:
                    conn.close()
                continue

            if cmd_lower == "/trace":
                from rich.table import Table
                tr = getattr(agent, "last_retrieval_trace", None)
                if tr is None:
                    console.print("[dim]No retrieval trace available yet. Ask a question first.[/dim]")
                else:
                    # --- Header panel ---
                    threshold_note = ""
                    if tr.threshold_adjusted and tr.adjusted_threshold is not None:
                        threshold_note = f" [dim](adjusted from higher — low accuracy for intent)[/dim]"
                    forced_note = "[bold red]Yes[/bold red]" if tr.force_desperation else "[dim]No[/dim]"
                    header_lines = [
                        f"[bold]Query   :[/bold] {tr.query}",
                        f"[bold]Intent  :[/bold] [yellow]{tr.intent}[/yellow]",
                        f"[bold]Threshold:[/bold] {tr.threshold:.2f}{threshold_note}",
                        f"[bold]Forced Desperation:[/bold] {forced_note}",
                    ]
                    console.print(Panel("\n".join(header_lines), title="[bold cyan]Retrieval Trace[/bold cyan]", border_style="cyan"))

                    # --- Stage execution table ---
                    stage_table = Table(title="Stage Execution", title_style="bold white", border_style="dim")
                    stage_table.add_column("Stage", style="white", min_width=18)
                    stage_table.add_column("Candidates", justify="right", style="white")
                    stage_table.add_column("Time (ms)", justify="right", style="green")

                    stage_display_map = {
                        "classification": "Classification",
                        "active_search": "Active Search",
                        "passive_search": "Passive Search",
                        "semantic_search": "Semantic Search",
                        "desperation": "Desperation",
                    }
                    for stage_key, stage_label in stage_display_map.items():
                        timing = tr.stage_timings.get(stage_key, "skipped")
                        count = tr.candidate_counts.get(stage_key, "—")
                        if timing == "skipped":
                            stage_table.add_row(
                                f"[dim]{stage_label}[/dim]",
                                "[dim]skipped[/dim]",
                                "[dim]skipped[/dim]",
                            )
                        else:
                            fired = stage_key == tr.stage_fired or (stage_key == "desperation" and tr.stage_fired == "desperation_mode")
                            label = f"[bold green]{stage_label} ✓[/bold green]" if fired else stage_label
                            stage_table.add_row(label, str(count), str(timing))

                    console.print(stage_table)
                    console.print(
                        f"  [bold]Stage Fired:[/bold] [bold green]{tr.stage_fired}[/bold green]  |  "
                        f"[bold]Total:[/bold] [bold white]{tr.total_duration_ms} ms[/bold white]"
                    )

                    # --- Top chunks table ---
                    if tr.top_chunk_ids:
                        chunk_table = Table(title="Top Chunks Returned", title_style="bold white", border_style="dim")
                        chunk_table.add_column("Chunk ID", style="white")
                        chunk_table.add_column("Score", justify="right", style="cyan")
                        chunk_table.add_column("Skip Mark", justify="right", style="yellow")
                        for cid in tr.top_chunk_ids:
                            score = tr.top_chunk_scores.get(cid, 0.0)
                            skip = tr.skip_marks_applied.get(cid, 0.0)
                            skip_str = f"{skip:.4f}" if skip > 0.0 else "[dim]0.00[/dim]"
                            chunk_table.add_row(cid, f"{score:.4f}", skip_str)
                        console.print(chunk_table)
                    else:
                        console.print("[dim]No chunks were returned by retrieval.[/dim]")
                continue

            if cmd_lower == "/subagent":
                from rich.table import Table
                
                result = getattr(agent, "last_subagent_result", None)
                gating = getattr(agent, "last_subagent_gating", None)
                
                if result is None:
                    console.print("[dim]No subagent execution history available yet. Run a subagent task first.[/dim]")
                else:
                    aal = result.aal_summary
                    
                    # Header Panel
                    outcome = aal.get("outcome", "failed")
                    outcome_style = "green" if outcome == "success" else "yellow" if outcome == "partial" else "red"
                    header_lines = [
                        f"[bold]Task        :[/bold] {aal.get('task')}",
                        f"[bold]Skill Used  :[/bold] [yellow]{aal.get('skill_used')}[/yellow]",
                        f"[bold]Outcome     :[/bold] [{outcome_style}]{outcome}[/{outcome_style}]",
                        f"[bold]Confidence  :[/bold] {aal.get('confidence', 0.0):.2f}",
                        f"[bold]Notes       :[/bold] {aal.get('notes', 'None')}",
                    ]
                    console.print(Panel("\n".join(header_lines), title="[bold cyan]Last Subagent Execution Summary[/bold cyan]", border_style="cyan"))
                    
                    # Memory Gating Summary
                    if gating:
                        gate_table = Table(title="Memory Gating Summary", title_style="bold white", border_style="dim")
                        gate_table.add_column("Status", style="white")
                        gate_table.add_column("Count", justify="right", style="cyan")
                        gate_table.add_column("Items / Details", style="white")
                        
                        accepted_count = len(gating.get("accepted", []))
                        rejected_count = len(gating.get("rejected", []))
                        
                        accepted_details = ", ".join(gating.get("accepted", []))
                        rejected_details = ", ".join(gating.get("rejected", []))
                        
                        accepted_short = accepted_details[:80] + ("..." if len(accepted_details) > 80 else "")
                        rejected_short = rejected_details[:80] + ("..." if len(rejected_details) > 80 else "")
                        
                        gate_table.add_row("[green]Accepted[/green]", str(accepted_count), accepted_short if accepted_count > 0 else "[dim]—[/dim]")
                        gate_table.add_row("[red]Rejected[/red]", str(rejected_count), rejected_short if rejected_count > 0 else "[dim]—[/dim]")
                        console.print(gate_table)
                        console.print(f"  [bold]Gating Decision Reason:[/bold] [dim]{gating.get('reason')}[/dim]")
                    else:
                        console.print("[dim]No memory gating info available for the last run.[/dim]")
                        
                    # Artifacts Table
                    artifacts = result.artifacts
                    if artifacts:
                        art_table = Table(title="Artifacts Produced", title_style="bold white", border_style="dim")
                        art_table.add_column("Filename", style="white")
                        art_table.add_column("Path", style="cyan")
                        art_table.add_column("Type", style="yellow")
                        art_table.add_column("Description", style="white")
                        for art in artifacts:
                            art_table.add_row(
                                art.get("filename", "unknown"),
                                art.get("path", "unknown"),
                                art.get("type", "other"),
                                art.get("description", "")
                            )
                        console.print(art_table)
                    else:
                        console.print("[dim]No artifacts were produced by the subagent.[/dim]")
                continue

            if cmd_lower.startswith("/report"):
                import monthly_report
                parts = stripped_input.split()
                ym = parts[1] if len(parts) > 1 and parts[1].lower() not in ("month", "current") else None
                rep_content = monthly_report.generate_monthly_report(year_month=ym)
                ym_str = ym if ym else datetime.now().strftime("%Y-%m")
                rep_path = monthly_report.get_report_path(ym_str)
                console.print(f"[bold green]✓ Monthly Report generated successfully at [cyan]{rep_path}[/cyan][/bold green]\n")
                console.print(Panel(rep_content[:500] + "\n\n[dim]... (Full report saved to file)[/dim]", title=f"Monthly Report ({ym_str})", border_style="gold3"))
                continue

            if cmd_lower.startswith("/daily"):
                import daily_manager
                parts = stripped_input.split()
                dt = parts[1] if len(parts) > 1 else None
                daily_content = daily_manager.generate_daily_note(date_str=dt)
                dt_str = dt if dt else datetime.now().strftime("%Y-%m-%d")
                daily_path = daily_manager.get_daily_dir() / f"{dt_str}.md"
                console.print(f"[bold green]✓ Daily Note generated successfully at [cyan]{daily_path}[/cyan][/bold green]\n")
                console.print(Panel(daily_content, title=f"Daily Note ({dt_str})", border_style="cyan"))
                continue




            # Process turn with graceful Ctrl+C interrupt handler
            console.print("[dim]Athena is thinking... (Press Ctrl+C to stop)[/dim]", end="\r")
            try:
                response = agent.run_one_turn(stripped_input)
                console.print("\n[bold gold3]Athena[/bold gold3]")
                console.print(response)
            except KeyboardInterrupt:
                console.print("\n[bold yellow]^C Generation interrupted by user.[/bold yellow]\n")
                continue

        except KeyboardInterrupt:
            console.print("\n[bold red]Session interrupted. Exiting Athena. Goodbye.[/bold red]\n")
            break
        except Exception as exc:
            console.print(f"\n[bold red]Error during conversation: {exc}[/bold red]")

def show_logs(lines: int = 40):
    log_file = config.get_athena_home() / "logs" / "agent.log"
    if not log_file.exists():
        console.print("[yellow]No log files found.[/yellow]")
        return
        
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.readlines()
            # print the last N lines
            recent = content[-lines:]
            console.print("".join(recent))
    except Exception as exc:
        console.print(f"[red]Failed to read log files: {exc}[/red]")

def main():
    parser = argparse.ArgumentParser(description="Athena v1: The Memory-First AI Agent.")
    parser.add_argument("command", choices=["chat", "doctor", "onboard", "logs", "sweep", "rollback", "server"], help="Command to run")
    parser.add_argument("--port", type=int, default=8080, help="Port for API server (default: 8080)")
    parser.add_argument("--project", default="default", help="Project namespace scope (default: default)")
    parser.add_argument("--session", default="session_1", help="Session ID (default: session_1)")
    parser.add_argument("--no-tui", action="store_true", help="Use legacy plain-text chat loop instead of TUI")
    parser.add_argument("--lines", type=int, default=40, help="Number of log lines to show (default: 40)")
    parser.add_argument("--provider", help="Active model provider to use (switches default in config)")
    parser.add_argument("--skip", action="store_true", help="Reset skip marks (learning engine)")
    parser.add_argument("--stats", action="store_true", help="Reset query statistics (learning engine)")
    parser.add_argument("--all", action="store_true", help="Reset all learning statistics and skip marks")
    
    # If no arguments provided, default to show help
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
        
    args = parser.parse_args()
    
    setup_logger()
    
    if args.provider:
        from providers_manager import get_manager
        mgr = get_manager()
        prov_name = args.provider.strip().lower()
        if prov_name in ("auto", "dynamic"):
            mgr.active_provider_id = None
            mgr.active_model_override = None
            mgr.save_providers()
            console.print("[bold green]Switched active provider selection to Auto (Dynamic Health).[/bold green]\n")
        elif prov_name in mgr.providers:
            mgr.active_provider_id = prov_name
            mgr.active_model_override = None
            mgr.save_providers()
            p = mgr.providers[prov_name]
            console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model})[/bold green]\n")
        else:
            import providers
            mapped = providers.map_legacy_provider_id(prov_name)
            if mapped in mgr.providers:
                mgr.active_provider_id = mapped
                mgr.active_model_override = None
                mgr.save_providers()
                p = mgr.providers[mapped]
                console.print(f"[bold green]Switched active provider to {p.name} (model: {p.default_model})[/bold green]\n")
            else:
                console.print(f"[bold red]Unknown provider: {prov_name}. Available providers: {', '.join(mgr.providers.keys())}[/bold red]\n")
            
    if args.command == "doctor":
        diagnostics.run_diagnostics()
    elif args.command == "onboard":
        run_onboarding()
    elif args.command == "logs":
        show_logs(args.lines)
    elif args.command == "sweep":
        import memory_sweep
        console.print("[bold cyan]Running memory sweep...[/bold cyan]")
        try:
            memory_sweep.run_memory_sweep()
            console.print("[bold green][OK] Memory sweep completed successfully.[/bold green]")
        except Exception as exc:
            console.print(f"[bold red][FAIL] Memory sweep failed: {exc}[/bold red]")
    elif args.command == "rollback":
        import learning_engine
        if args.skip or args.all or (not args.stats and not args.skip):
            learning_engine.reset_skip_marks()
            console.print("[bold green][OK] Skip marks reset completed.[/bold green]")
        if args.stats or args.all or (not args.stats and not args.skip):
            learning_engine.reset_query_statistics()
            console.print("[bold green][OK] Query statistics reset completed.[/bold green]")
    elif args.command == "server":
        import api_server
        api_server.start_api_server(port=args.port)
    elif args.command == "chat":
        run_chat_loop(args.project, args.session)

if __name__ == "__main__":
    main()
