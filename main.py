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
                auth_map = {
                    "browser": "ChatGPT Pro/Plus (browser)",
                    "headless": "ChatGPT Pro/Plus (headless)",
                    "api": "Manually enter API Key"
                }
                auth_choices = list(auth_map.keys())
                current_auth_type = prov_cfg.get("auth_type", "api")
                default_auth = "api"
                if current_auth_type == "oauth":
                    default_auth = "browser"
                
                console.print("\nOpenAI Authentication Options:")
                for k, v in auth_map.items():
                    console.print(f"  [bold cyan]{k}[/bold cyan]: {v}")
                
                auth_key = Prompt.ask(
                    "Select authentication method for OpenAI",
                    choices=auth_choices,
                    default=default_auth
                )
                auth_choice = auth_map[auth_key]
                
                if auth_choice == "ChatGPT Pro/Plus (browser)":
                    import openai_auth
                    import webbrowser
                    
                    state = openai_auth.generate_pkce()[1]
                    verifier, challenge = openai_auth.generate_pkce()
                    redirect_uri = f"http://localhost:{openai_auth.PORT}/auth/callback"
                    auth_url = openai_auth.build_authorize_url(redirect_uri, challenge, state)
                    
                    console.print(f"\n[bold cyan]Complete authorization in your browser...[/bold cyan]")
                    console.print(f"Opening browser to: {auth_url}")
                    webbrowser.open(auth_url)
                    
                    code, err = openai_auth.run_callback_server(state, openai_auth.PORT)
                    if code:
                        tokens = openai_auth.exchange_code_for_tokens(code, redirect_uri, verifier)
                        access = tokens["access_token"]
                        refresh = tokens["refresh_token"]
                        expires_in = tokens.get("expires_in", 3600)
                        account_id = openai_auth.extract_account_id(tokens)
                        
                        openai_auth.save_chatgpt_credentials(access, refresh, expires_in, account_id)
                        prov_cfg["auth_type"] = "oauth"
                        prov_cfg["model"] = "gpt-5.5"
                        if prov_choice == prov_name:
                            cfg["model"] = "gpt-5.5"
                        console.print("\n[bold green][OK] ChatGPT Pro/Plus authorization successful.[/bold green]\n")
                    else:
                        console.print(f"\n[bold red][FAIL] Authorization failed: {err}[/bold red]\n")
                        
                elif auth_choice == "ChatGPT Pro/Plus (headless)":
                    import openai_auth
                    try:
                        device_data = openai_auth.initiate_headless_flow()
                        device_auth_id = device_data["device_auth_id"]
                        user_code = device_data["user_code"]
                        interval = max(int(device_data.get("interval", 5)), 1)
                        
                        console.print(f"\n  Open this URL in your browser: [bold cyan]https://auth.openai.com/codex/device[/bold cyan]")
                        console.print(f"  Enter this code: [bold gold3]{user_code}[/bold gold3]")
                        console.print("\n  Waiting for authorization...", end="", flush=True)
                        
                        token_data = openai_auth.poll_headless_token(device_auth_id, user_code, interval)
                        
                        tokens = openai_auth.exchange_code_for_tokens(
                            token_data["authorization_code"],
                            "https://auth.openai.com/deviceauth/callback",
                            token_data["code_verifier"]
                        )
                        access = tokens["access_token"]
                        refresh = tokens["refresh_token"]
                        expires_in = tokens.get("expires_in", 3600)
                        account_id = openai_auth.extract_account_id(tokens)
                        
                        openai_auth.save_chatgpt_credentials(access, refresh, expires_in, account_id)
                        prov_cfg["auth_type"] = "oauth"
                        prov_cfg["model"] = "gpt-5.5"
                        if prov_choice == prov_name:
                            cfg["model"] = "gpt-5.5"
                        console.print(" [bold green]✓[/bold green]")
                        console.print("\n[bold green][OK] ChatGPT Pro/Plus authorization successful.[/bold green]\n")
                    except Exception as exc:
                        console.print(f"\n[bold red][FAIL] Headless authorization failed: {exc}[/bold red]\n")
                else:
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
    setup_logger()
    memory_engine.initialize_db()
    
    # Try fetching Caveman skills in background if they don't exist
    config.fetch_caveman_skills(force=False)
    
    cfg = config.load_config()
    active_prov = cfg.get("provider", "gemini")
    active_model = cfg.get("model", "")
    
    console.print(Panel(
        f"[bold gold3]Athena v1 Interactive Chat Shell[/bold gold3]\n"
        f"[dim]Project Namespace: [cyan]{project_id}[/cyan] | Session: [cyan]{session_id}[/cyan][/dim]\n"
        f"[dim]Active Provider: [green]{active_prov}[/green] | Model: [green]{active_model}[/green][/dim]\n"
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
                
            if stripped_input.lower() == "/provider":
                cfg = config.load_config()
                console.print(f"Current provider: [green]{cfg.get('provider')}[/green] (model: [green]{cfg.get('model')}[/green])")
                continue
                
            if stripped_input.lower().startswith("/provider "):
                new_prov = stripped_input.split(" ", 1)[1].strip().lower()
                cfg = config.load_config()
                if new_prov in ["gemini", "openrouter", "openai", "groq", "nvidia", "github-copilot"]:
                    cfg["provider"] = new_prov
                    prov_cfg = cfg.get("providers", {}).get(new_prov, {})
                    if prov_cfg.get("model"):
                        cfg["model"] = prov_cfg.get("model")
                    config.save_config(cfg)
                    console.print(f"[bold green]Switched active provider to {new_prov} (model: {cfg['model']}).[/bold green]")
                else:
                    console.print(f"[bold red]Unknown provider: {new_prov}. Use gemini, openrouter, openai, groq, nvidia, or github-copilot.[/bold red]")
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
        cfg = config.load_config()
        prov_name = args.provider.strip().lower()
        if prov_name in ["gemini", "openrouter", "openai", "groq", "nvidia", "github-copilot"]:
            cfg["provider"] = prov_name
            prov_cfg = cfg.get("providers", {}).get(prov_name, {})
            if prov_cfg.get("model"):
                cfg["model"] = prov_cfg.get("model")
            config.save_config(cfg)
            console.print(f"[bold green]Switched active provider to {prov_name} (model: {cfg['model']})[/bold green]\n")
        else:
            console.print(f"[bold red]Unknown provider: {prov_name}.[/bold red]\n")
            
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
