import os
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
import config
import memory_engine
import copilot_auth
import openai_auth

console = Console()

def run_diagnostics():
    console.print("\n[bold gold3]Athena v1 — System Diagnostic Audit[/bold gold3]\n")
    
    # 1. Environment & Directories
    home = config.get_athena_home()
    config.ensure_athena_dirs()
    
    table_dirs = Table(title="Directory Structure Checks", show_header=True, header_style="bold magenta")
    table_dirs.add_column("Path Name", style="cyan")
    table_dirs.add_column("Actual Path", style="dim")
    table_dirs.add_column("Status", justify="center")
    
    paths_to_check = {
        "Athena Home": home,
        "Configuration": home / "config.yaml",
        "Environment (.env)": home / ".env",
        "Knowledge Folder": home / "knowledge",
        "Skills Folder": home / "skills" / "caveman",
        "Logs Folder": home / "logs",
        "SQLite Database": memory_engine.get_db_path()
    }
    
    for name, path in paths_to_check.items():
        exists = path.exists()
        status_text = "[green]OK[/green]" if exists else "[red]Missing[/red]"
        table_dirs.add_row(name, str(path), status_text)
        
    console.print(table_dirs)
    console.print()
    
    # 2. Database Stats
    stats = memory_engine.get_diagnostics_stats()
    
    table_db = Table(title="Database Health Statistics", show_header=True, header_style="bold magenta")
    table_db.add_column("Metric", style="cyan")
    table_db.add_column("Value", style="green")
    
    db_size_kb = stats["db_size_bytes"] / 1024.0
    table_db.add_row("Total Facts", str(stats["total_facts"]))
    table_db.add_row("Active Facts", str(stats["active_facts"]))
    table_db.add_row("Archived Facts (Decayed)", str(stats["archived_facts"]))
    table_db.add_row("Database File Size", f"{db_size_kb:.2f} KB")
    
    console.print(table_db)
    console.print()
    
    # 3. Model Providers & Credentials
    cfg = config.load_config()
    env = config.load_env()
    
    active_provider = cfg.get("provider", "gemini")
    active_model = cfg.get("model", "gemini-3-flash")
    
    console.print(Panel(
        f"[bold cyan]Active Configured Provider:[/bold cyan] [green]{active_provider}[/green]\n"
        f"[bold cyan]Active Configured Model:[/bold cyan] [green]{active_model}[/green]",
        title="Active Model Routing"
    ))
    console.print()
    
    table_prov = Table(title="API Keys and Credentials Check", show_header=True, header_style="bold magenta")
    table_prov.add_column("Provider", style="cyan")
    table_prov.add_column("Source", style="dim")
    table_prov.add_column("Status", justify="center")
    
    # Check standard API keys in config or env
    providers_keys = {
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "nvidia": "NVIDIA_API_KEY"
    }
    
    for prov_name, env_name in providers_keys.items():
        prov_cfg = cfg.get("providers", {}).get(prov_name, {})
        cfg_key = prov_cfg.get("api_key", "")
        env_key = env.get(env_name, "") or os.environ.get(env_name, "")
        
        if prov_name == "openai" and prov_cfg.get("auth_type") == "oauth":
            creds = openai_auth.load_chatgpt_credentials()
            if creds.get("refresh_token"):
                source = "keyless (OAuth)"
                import time
                expires_diff = creds["expires_at"] - time.time()
                if expires_diff > 0:
                    status = f"[green]Available (Expires in {int(expires_diff/60)}m)[/green]"
                else:
                    status = "[yellow]Expired (Will Refresh)[/yellow]"
            else:
                source = "keyless (OAuth)"
                status = "[yellow]Not Authenticated[/yellow]"
        else:
            has_key = bool(cfg_key or env_key)
            if has_key:
                source = "config.yaml" if cfg_key else f"env ({env_name})"
                status = "[green]Available (Masked)[/green]"
            else:
                source = "n/a"
                status = "[yellow]Not Configured[/yellow]"
            
        table_prov.add_row(prov_name, source, status)
        
    # 4. GitHub Copilot Token Resolution
    copilot_token, copilot_source = copilot_auth.resolve_copilot_token()
    if copilot_token:
        table_prov.add_row("github-copilot", f"keyless ({copilot_source})", "[green]Available (Keyless)[/green]")
    else:
        table_prov.add_row("github-copilot", "n/a", "[yellow]Not Authenticated[/yellow]")
        
    console.print(table_prov)
    console.print()
    
    # 5. Caveman Skills Cache Check
    skills_dir = home / "skills" / "caveman"
    skills_cache_status = []
    for skill_file in ["SKILL.md", "caveman-commit.md", "caveman-review.md"]:
        if (skills_dir / skill_file).exists():
            skills_cache_status.append(f"[green][+] {skill_file}[/green]")
        else:
            skills_cache_status.append(f"[red][-] {skill_file}[/red]")
            
    console.print(Panel(
        " ".join(skills_cache_status),
        title="JuliusBrussee/caveman Skills Cache"
    ))
    console.print()
    
    # Overall summary health statement
    if active_provider == "github-copilot" and not copilot_token:
        console.print("[bold red][WARNING] Critical Warning: Active provider is set to 'github-copilot' but no GitHub token was resolved. Run 'athena onboard' or install GitHub CLI to authenticate.[/bold red]\n")
    elif active_provider == "openai" and cfg.get("providers", {}).get("openai", {}).get("auth_type") == "oauth":
        creds = openai_auth.load_chatgpt_credentials()
        if not creds.get("refresh_token"):
            console.print("[bold red][WARNING] Critical Warning: Active provider is set to 'openai' with OAuth, but no ChatGPT Pro/Plus token was found. Run 'athena onboard' to authenticate.[/bold red]\n")
        else:
            console.print("[bold green][OK] System Health Check Passed. Athena v1 is ready to operate.[/bold green]\n")
    elif active_provider != "github-copilot" and not (cfg.get("providers", {}).get(active_provider, {}).get("api_key") or env.get(providers_keys.get(active_provider, "")) or os.environ.get(providers_keys.get(active_provider, ""))):
         console.print(f"[bold red][WARNING] Critical Warning: Active provider '{active_provider}' is selected but no API key was found in config.yaml or environment.[/bold red]\n")
    else:
        console.print("[bold green][OK] System Health Check Passed. Athena v1 is ready to operate.[/bold green]\n")
