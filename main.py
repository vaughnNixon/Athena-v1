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
    console.print("\n[bold gold3]Athena v1 — Interactive Setup Wizard[/bold gold3]\n")
    console.print("This wizard will configure your model providers and credentials.\n")
    
    cfg = config.load_config()
    env = config.load_env()
    
    # 1. Choose default provider
    providers_list = ["gemini", "openrouter", "openai", "groq", "nvidia", "github-copilot"]
    current_prov = cfg.get("provider", "gemini")
    
    prov_choice = Prompt.ask(
        "Select your active model provider",
        choices=providers_list,
        default=current_prov
    )
    cfg["provider"] = prov_choice
    
    # 2. Keyless GitHub Copilot Authentication
    if prov_choice == "github-copilot" or Confirm.ask("Would you like to log in to GitHub Copilot for keyless access?"):
        console.print("\n[bold cyan]Starting GitHub OAuth Device Code Flow...[/bold cyan]")
        token = copilot_auth.copilot_device_code_login()
        if token:
            env_file = config.get_athena_home() / ".env"
            # Read current env content
            env_lines = []
            if env_file.exists():
                env_lines = env_file.read_text(encoding="utf-8").splitlines()
            
            # Remove existing token
            env_lines = [l for l in env_lines if not l.strip().startswith("COPILOT_GITHUB_TOKEN=")]
            env_lines.append(f"COPILOT_GITHUB_TOKEN={token}")
            env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            
            console.print("\n[bold green][OK] GitHub Copilot login successful. Token saved to .env.[/bold green]\n")
        else:
            console.print("\n[bold red][FAIL] GitHub Copilot login failed or timed out.[/bold red]\n")
            
    # 3. Configure API Keys
    providers_keys = {
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "nvidia": "NVIDIA_API_KEY"
    }
    
    # Only ask to configure keys for standard providers
    for prov_name, env_name in providers_keys.items():
        if prov_choice == prov_name or Confirm.ask(f"Configure credentials for provider '{prov_name}'?"):
            prov_cfg = cfg.get("providers", {}).setdefault(prov_name, {})
            
            if prov_name == "openai":
                # Manually enter API Key
                current_key = prov_cfg.get("api_key", "") or env.get(env_name, "")
                masked_display = "****" if current_key else "None"
                new_key = Prompt.ask(
                    f"Enter API key for {prov_name} (Current: {masked_display})",
                    password=True,
                    default=current_key
                )
                if new_key:
                    prov_cfg["api_key"] = new_key
                prov_cfg["auth_type"] = "api"
                
                current_model = prov_cfg.get("model", "") or "gpt-4o-mini"
                model_choice = Prompt.ask(f"Enter model ID for {prov_name}", default=current_model)
                prov_cfg["model"] = model_choice
                if prov_choice == prov_name:
                    cfg["model"] = model_choice

            else:
                current_key = prov_cfg.get("api_key", "") or env.get(env_name, "")
                masked_display = "****" if current_key else "None"
                new_key = Prompt.ask(
                    f"Enter API key for {prov_name} (Current: {masked_display})",
                    password=True,
                    default=current_key
                )
                if new_key:
                    prov_cfg["api_key"] = new_key
                    
                current_model = prov_cfg.get("model", "")
                if not current_model:
                    if prov_name == "gemini": current_model = "gemini-1.5-flash"
                    elif prov_name == "openrouter": current_model = "google/gemini-flash-1.5-8b"
                    elif prov_name == "groq": current_model = "llama-3.1-8b-instant"
                    elif prov_name == "nvidia": current_model = "meta/llama3-70b-instruct"
                    
                model_choice = Prompt.ask(f"Enter model ID for {prov_name}", default=current_model)
                prov_cfg["model"] = model_choice
                if prov_choice == prov_name:
                    cfg["model"] = model_choice
                
    config.save_config(cfg)
    console.print("\n[bold green][OK] Onboarding complete. Settings successfully saved.[/bold green]\n")

def run_chat_loop(project_id: str, session_id: str):
    from rich.table import Table
    from providers_manager import get_manager
    setup_logger()
    memory_engine.initialize_db()
    
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
            
            # Process turn
            console.print("[dim]Athena is recalling & thinking...[/dim]", end="\r")
            response = agent.run_one_turn(stripped_input)
            
            console.print("\n[bold gold3]Athena[/bold gold3]")
            console.print(response)
            
        except KeyboardInterrupt:
            console.print("\n[bold red]Interrupt received. Exiting.[/bold red]\n")
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
    parser.add_argument("command", choices=["chat", "doctor", "onboard", "logs"], help="Command to run")
    parser.add_argument("--project", default="default", help="Project namespace scope (default: default)")
    parser.add_argument("--session", default="session_1", help="Session ID (default: session_1)")
    parser.add_argument("--lines", type=int, default=40, help="Number of log lines to show (default: 40)")
    parser.add_argument("--provider", help="Active model provider to use (switches default in config)")
    
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
    elif args.command == "chat":
        run_chat_loop(args.project, args.session)

if __name__ == "__main__":
    main()
