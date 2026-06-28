"""
tui.py  --  Athena v1 Terminal UI
OpenCode-inspired terminal interface built with Textual.
All core logic (agent_loop, memory, SCL) is UNTOUCHED.
The sync run_one_turn() is bridged via a @work(thread=True) worker.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from rich.table import Table as RTable
from rich.panel import Panel as RPanel
from rich.text import Text as RText

from textual import on, work
from textual.message import Message
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, ScrollableContainer, Vertical, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, Static, RichLog, Footer

import config
import memory_engine
from agent_loop import AthenaAgent


VERSION = "v1.3"

SLASH_COMMANDS: List[Tuple[str, str]] = [
    ("/caveman",   "Toggle caveman compressed response mode"),
    ("/topics",    "Show active session topics"),
    ("/pin",       "Pin a topic -- prevents auto-deactivation"),
    ("/unpin",     "Unpin a topic back to NORMAL priority"),
    ("/newchat",   "Archive session and carry context to fresh chat"),
    ("/trace",     "Show last staged retrieval trace"),
    ("/learning",  "Show adaptive retrieval accuracy stats"),
    ("/subagent",  "Show last subagent execution summary"),
    ("/rollback",  "Reset all skip marks and query statistics"),
    ("/providers", "View all configured LLM providers"),
    ("/provider",  "Switch / add / manage providers"),
    ("/model",     "Override the active model"),
    ("/exit",      "End session"),
]

PALETTE_SECTIONS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    ("Suggested", [
        ("New Chat Session",   "ctrl+n",    "/newchat"),
        ("Switch Provider",    "ctrl+k",    "/providers"),
    ]),
    ("Session", [
        ("View Active Topics", "/topics",   "/topics"),
        ("Pin a Topic",        "/pin",      "/pin"),
        ("Unpin a Topic",      "/unpin",    "/unpin"),
        ("Archive and New Chat", "/newchat",  "/newchat"),
    ]),
    ("Diagnostics", [
        ("Retrieval Trace",    "/trace",    "/trace"),
        ("Learning Stats",     "/learning", "/learning"),
        ("Subagent Summary",   "/subagent", "/subagent"),
    ]),
    ("Providers and Models", [
        ("View All Providers", "/providers", "/providers"),
        ("Switch Model",       "/model",     "/model"),
        ("Reset Learning",     "/rollback",  "/rollback"),
    ]),
]

CSS = """
Screen {
    background: #0d0d0d;
}
#header {
    height: 3;
    background: #111111;
    border-bottom: solid #1e1e1e;
    layout: horizontal;
    align: left middle;
    padding: 0 2;
}
#header-title {
    color: #d4a843;
    width: auto;
    text-style: bold;
}
#header-sep {
    color: #2d2d2d;
    width: auto;
    padding: 0 1;
}
#header-info {
    color: #374151;
    width: 1fr;
}
#header-shortcuts {
    color: #2d2d2d;
    width: auto;
    text-align: right;
}
#chat-log {
    height: 1fr;
    padding: 1 2;
    scrollbar-color: #1e1e1e;
}
#slash-menu {
    height: auto;
    max-height: 15;
    background: #111111;
    border-top: solid #1e1e1e;
    display: none;
}
.slash-row {
    height: 1;
    padding: 0 2;
    layout: horizontal;
}
.slash-row.--selected {
    background: #1e1a0f;
}
.slash-cmd {
    width: 14;
    color: #d4a843;
}
.slash-desc {
    width: 1fr;
    color: #4b5563;
}
#status-bar {
    height: 1;
    background: #0a0a0a;
    border-top: solid #1a1a1a;
    layout: horizontal;
    align: left middle;
    padding: 0 2;
}
#status-left {
    width: 1fr;
    color: #374151;
}
#status-right {
    width: auto;
    color: #2d2d2d;
    text-align: right;
}
#input-row {
    height: 3;
    layout: horizontal;
    border-top: solid #1a1a1a;
    background: #0d0d0d;
    align: left middle;
}
#input-prefix {
    width: 6;
    color: #d4a843;
    content-align: center middle;
    padding: 0 1;
}
#chat-input {
    width: 1fr;
    background: #0d0d0d;
    border: none;
    color: #e5e7eb;
    padding: 0;
}
#chat-input:focus {
    border: none;
    background: #0d0d0d;
}
#thinking-row {
    height: 1;
    layout: horizontal;
    padding: 0 2;
    display: none;
}
#thinking-label {
    color: #374151;
}
CommandPaletteScreen {
    align: center middle;
    background: #0d0d0d 80%;
}
#palette-box {
    width: 72;
    height: auto;
    max-height: 38;
    background: #111111;
    border: solid #2d2d2d;
    padding: 1 2;
}
#palette-header {
    layout: horizontal;
    height: 1;
    margin-bottom: 1;
}
#palette-title {
    width: 1fr;
    color: #e5e7eb;
    text-style: bold;
}
#palette-esc {
    width: auto;
    color: #374151;
}
#palette-search {
    height: 3;
    background: #161616;
    border: solid #2d2d2d;
    color: #e5e7eb;
    margin-bottom: 1;
}
#palette-search:focus {
    border: solid #d4a843;
}
#palette-scroll {
    height: auto;
    max-height: 26;
}
.pal-section {
    color: #d4a843;
    padding: 1 0 0 0;
    text-style: bold;
}
.pal-item {
    layout: horizontal;
    height: 1;
    padding: 0 1;
}
.pal-item.--selected {
    background: #1e1a0f;
}
.pal-name {
    width: 1fr;
    color: #d1d5db;
}
.pal-hint {
    width: 14;
    color: #374151;
    text-align: right;
}
"""


class PaletteItem(Widget):
    can_focus = False

    class Selected(Message):
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    def __init__(self, name: str, hint: str, command: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._label = name
        self._hint = hint
        self._command = command
        self.add_class("pal-item")

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="pal-name")
        yield Label(self._hint, classes="pal-hint")

    def on_click(self) -> None:
        self.post_message(self.Selected(self._command))


class CommandPaletteScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss_palette", "Close"),
        Binding("up",     "move_up",         "Up",     show=False),
        Binding("down",   "move_down",        "Down",   show=False),
        Binding("enter",  "select_item",      "Select", show=False),
    ]

    selected_index: reactive[int] = reactive(0)

    def __init__(self, chat_screen: "ChatScreen") -> None:
        super().__init__()
        self._chat = chat_screen
        self._all_items: List[Tuple[str, str, str]] = []
        for _, items in PALETTE_SECTIONS:
            self._all_items.extend(items)
        self._filtered: List[Tuple[str, str, str]] = list(self._all_items)

    def compose(self) -> ComposeResult:
        with Container(id="palette-box"):
            with Horizontal(id="palette-header"):
                yield Label("Commands", id="palette-title")
                yield Label("esc", id="palette-esc")
            yield Input(placeholder="Search...", id="palette-search")
            with ScrollableContainer(id="palette-scroll"):
                yield Container(id="palette-list")

    def on_mount(self) -> None:
        self.query_one("#palette-search", Input).focus()
        self._rebuild_list()

    def _rebuild_list(self, query: str = "") -> None:
        q = query.lower().strip()
        container = self.query_one("#palette-list", Container)
        container.remove_children()
        self._filtered = []
        idx = 0
        for section_name, items in PALETTE_SECTIONS:
            matched = [it for it in items if not q or q in it[0].lower() or q in it[2].lower()]
            if not matched:
                continue
            container.mount(Label(section_name, classes="pal-section"))
            for name, hint, cmd in matched:
                self._filtered.append((name, hint, cmd))
                item = PaletteItem(name, hint, cmd)
                if idx == self.selected_index:
                    item.add_class("--selected")
                container.mount(item)
                idx += 1
        if self._filtered:
            self.selected_index = min(self.selected_index, len(self._filtered) - 1)

    @on(Input.Changed, "#palette-search")
    def _on_search(self, event: Input.Changed) -> None:
        self.selected_index = 0
        self._rebuild_list(event.value)

    def _update_highlight(self) -> None:
        for i, item in enumerate(self.query(".pal-item").results(PaletteItem)):
            if i == self.selected_index:
                item.add_class("--selected")
            else:
                item.remove_class("--selected")

    def action_move_up(self) -> None:
        if self._filtered and self.selected_index > 0:
            self.selected_index -= 1
            self._update_highlight()

    def action_move_down(self) -> None:
        if self._filtered and self.selected_index < len(self._filtered) - 1:
            self.selected_index += 1
            self._update_highlight()

    def action_select_item(self) -> None:
        if self._filtered:
            _, _, cmd = self._filtered[self.selected_index]
            self.dismiss()
            self._chat.execute_command(cmd)

    def action_dismiss_palette(self) -> None:
        self.dismiss()

    def on_palette_item_selected(self, event: PaletteItem.Selected) -> None:
        self.dismiss()
        self._chat.execute_command(event.command)


class SlashRow(Widget):
    can_focus = False

    def __init__(self, cmd: str, desc: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cmd = cmd
        self._desc = desc
        self.add_class("slash-row")

    def compose(self) -> ComposeResult:
        yield Label(self._cmd,  classes="slash-cmd")
        yield Label(self._desc, classes="slash-desc")


class SlashMenu(Widget):
    selected_index: reactive[int] = reactive(0)

    def __init__(self) -> None:
        super().__init__(id="slash-menu")
        self._matches: List[Tuple[str, str]] = []

    def update(self, query: str) -> None:
        q = query.lower()
        self._matches = [(c, d) for c, d in SLASH_COMMANDS if c.startswith(q)]
        self.selected_index = 0
        self._rebuild()

    def _rebuild(self) -> None:
        self.remove_children()
        for i, (cmd, desc) in enumerate(self._matches):
            row = SlashRow(cmd, desc)
            if i == self.selected_index:
                row.add_class("--selected")
            self.mount(row)

    def _update_highlight(self) -> None:
        for i, row in enumerate(self.query(".slash-row").results(SlashRow)):
            if i == self.selected_index:
                row.add_class("--selected")
            else:
                row.remove_class("--selected")

    def move_up(self) -> None:
        if self._matches and self.selected_index > 0:
            self.selected_index -= 1
            self._update_highlight()

    def move_down(self) -> None:
        if self._matches and self.selected_index < len(self._matches) - 1:
            self.selected_index += 1
            self._update_highlight()

    def get_selected(self) -> Optional[str]:
        if self._matches and 0 <= self.selected_index < len(self._matches):
            return self._matches[self.selected_index][0]
        return None

    def has_matches(self) -> bool:
        return bool(self._matches)


class ChatScreen(Widget):
    def __init__(self, app_ref: "AthenaApp") -> None:
        super().__init__()
        self._app = app_ref

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Label(f"athena {VERSION}", id="header-title")
            yield Label(" . ", id="header-sep")
            yield Label("", id="header-info")
            yield Label("/commands   ctrl+p palette", id="header-shortcuts")
        yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
        with Horizontal(id="thinking-row"):
            yield Label("Athena is thinking...  (Ctrl+C to stop)", id="thinking-label")
        yield SlashMenu()
        with Horizontal(id="status-bar"):
            yield Label("", id="status-left")
            yield Label("ctrl+p  commands", id="status-right")
        with Horizontal(id="input-row"):
            yield Label("> ", id="input-prefix")
            yield Input(placeholder='Ask anything...  type / for commands', id="chat-input")

    def on_mount(self) -> None:
        self._refresh_header()
        self._refresh_status()
        self.query_one("#chat-input", Input).focus()
        log = self.query_one("#chat-log", RichLog)
        log.write(RText.from_markup(
            f"[dim]Athena {VERSION} ready. Type a message or [bold #d4a843]/[/bold #d4a843] to see commands.[/dim]\n"
        ))

    def _refresh_header(self) -> None:
        try:
            from providers_manager import get_manager
            mgr = get_manager()
            active_id = mgr.active_provider_id
            if active_id:
                p = mgr.providers.get(active_id)
                pname = p.name if p else active_id
                mname = mgr.active_model_override or (p.default_model if p else "")
            else:
                h = mgr.get_healthiest_provider()
                pname = f"auto ({h.name})" if h else "auto"
                mname = mgr.active_model_override or (h.default_model if h else "")
            info = f"[dim]{pname}[/dim]  [#2d2d2d].[/#2d2d2d]  [dim]{mname}[/dim]  [#2d2d2d].[/#2d2d2d]  [dim]{self._app.project_id}/{self._app.session_id}[/dim]"
            self.query_one("#header-info", Label).update(info)
        except Exception:
            pass

    def _refresh_status(self) -> None:
        try:
            agent = self._app.agent
            if agent and agent.scl:
                scl = agent.scl
                pressure = scl.get_context_pressure()
                topics = scl.get_active_topics(top_n=20)
                active_count = sum(1 for t in topics if t["status"] == "ACTIVE")
                pct = int(pressure * 100)
                color = "#22c55e" if pct < 70 else "#f59e0b" if pct < 85 else "#ef4444"
                left = (
                    f"[{color}]{pct}% pressure[/{color}]"
                    f"  [#2d2d2d].[/#2d2d2d]"
                    f"  [dim]{active_count} active topics[/dim]"
                )
                self.query_one("#status-left", Label).update(left)
        except Exception:
            pass

    @on(Input.Changed, "#chat-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        val = event.value
        menu = self.query_one(SlashMenu)
        if val.startswith("/") and val != "/":
            menu.update(val)
            if menu.has_matches():
                menu.display = True
            else:
                menu.display = False
        else:
            menu.display = False

    @on(Input.Submitted, "#chat-input")
    def _on_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        inp = self.query_one("#chat-input", Input)
        inp.value = ""
        menu = self.query_one(SlashMenu)
        if menu.display and menu.has_matches():
            selected = menu.get_selected()
            menu.display = False
            if selected:
                self.execute_command(selected)
            return
        menu.display = False
        self.execute_command(text)

    def on_key(self, event) -> None:
        menu = self.query_one(SlashMenu)
        if menu.display:
            if event.key == "up":
                menu.move_up()
                event.stop()
            elif event.key == "down":
                menu.move_down()
                event.stop()
            elif event.key == "escape":
                menu.display = False
                event.stop()

    def execute_command(self, text: str) -> None:
        stripped = text.strip()
        cmd = stripped.lower()
        if cmd in {"/exit", "/quit", "exit", "quit"}:
            self._app.exit()
            return
        if cmd.startswith("/"):
            self._dispatch_slash(stripped, cmd)
        else:
            self._emit_user_message(stripped)
            self._run_agent(stripped)

    def _dispatch_slash(self, raw: str, cmd: str) -> None:
        parts = raw.split(maxsplit=1)
        agent = self._app.agent
        log = self.query_one("#chat-log", RichLog)

        if cmd == "/caveman":
            agent.caveman_mode = not agent.caveman_mode
            status = "ON" if agent.caveman_mode else "OFF"
            color = "#22c55e" if agent.caveman_mode else "#f59e0b"
            log.write(RText.from_markup(f"[{color}]Caveman mode {status}[/{color}]\n"))

        elif cmd == "/topics":
            topics = agent.scl.get_active_topics(top_n=15)
            if not topics:
                log.write(RText.from_markup("[dim]No active topics being tracked.[/dim]\n"))
            else:
                t = RTable(title="Active Session Topics", border_style="dim", title_style="bold cyan")
                t.add_column("Topic", style="white")
                t.add_column("Score", justify="right", style="#22c55e")
                t.add_column("Status", style="#f59e0b")
                t.add_column("Priority", style="#a78bfa")
                t.add_column("Mentions", justify="right", style="cyan")
                for tp in topics:
                    t.add_row(tp["topic"], f"{tp['score']:.2f}", tp["status"], tp["priority"], str(tp["mention_count"]))
                log.write(t)
                log.write("")

        elif cmd.startswith("/pin") and not cmd.startswith("/unpin"):
            if len(parts) < 2:
                log.write(RText.from_markup("[red]Usage: /pin <topic_name>[/red]\n"))
            else:
                tname = parts[1].strip()
                agent.scl.pin_topic(tname)
                log.write(RText.from_markup(f"[#22c55e]Topic '{tname}' is now PINNED.[/#22c55e]\n"))

        elif cmd.startswith("/unpin"):
            if len(parts) < 2:
                log.write(RText.from_markup("[red]Usage: /unpin <topic_name>[/red]\n"))
            else:
                tname = parts[1].strip()
                agent.scl.unpin_topic(tname)
                log.write(RText.from_markup(f"[#22c55e]Topic '{tname}' unpinned (NORMAL).[/#22c55e]\n"))

        elif cmd == "/newchat":
            handoff = agent.scl.export_for_new_chat()
            agent.scl.archive_session()
            new_sid = f"session_{int(time.time())}"
            self._app.session_id = new_sid
            self._app.agent = AthenaAgent(project_id=self._app.project_id, session_id=new_sid)
            self._app.agent.scl._write_session(
                summary=handoff["summary"],
                summary_version=handoff["summary_version"],
                summary_marker=handoff["summary_marker"],
                context_pressure=0.0,
            )
            log.write(RText.from_markup(
                f"[#22c55e]Session archived. New session [bold]{new_sid}[/bold] started.[/#22c55e]\n"
                "[dim]Context carried over seamlessly.[/dim]\n"
            ))
            self._refresh_header()
            self._refresh_status()

        elif cmd == "/rollback":
            import learning_engine
            learning_engine.reset_skip_marks()
            learning_engine.reset_query_statistics()
            log.write(RText.from_markup("[#22c55e]Skip marks and query statistics reset.[/#22c55e]\n"))

        elif cmd == "/learning":
            conn = memory_engine.get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT query_type, total_queries, corrected_queries, accuracy FROM query_statistics")
                rows = cur.fetchall()
                if not rows:
                    log.write(RText.from_markup("[dim]No query statistics logged yet.[/dim]\n"))
                else:
                    t = RTable(title="Adaptive Retrieval Statistics", border_style="dim", title_style="bold cyan")
                    t.add_column("Query Type", style="#f59e0b")
                    t.add_column("Total", justify="right")
                    t.add_column("Corrected", justify="right")
                    t.add_column("Accuracy", justify="right")
                    for qtype, total, corrected, acc in rows:
                        color = "#22c55e" if acc >= 0.8 else "#f59e0b" if acc >= 0.5 else "#ef4444"
                        t.add_row(qtype, str(total), str(corrected), f"[{color}]{acc*100:.1f}%[/{color}]")
                    log.write(t)
                cur.execute("SELECT COUNT(*) FROM skip_marks")
                skip_n = cur.fetchone()[0]
                log.write(RText.from_markup(f"[dim]Active skip marks: [white]{skip_n}[/white][/dim]\n"))
            except Exception as exc:
                log.write(RText.from_markup(f"[red]Stats error: {exc}[/red]\n"))
            finally:
                conn.close()

        elif cmd == "/trace":
            tr = getattr(agent, "last_retrieval_trace", None)
            if tr is None:
                log.write(RText.from_markup("[dim]No retrieval trace yet. Ask a question first.[/dim]\n"))
            else:
                header = RText.from_markup(
                    f"[bold]Query:[/bold]  {tr.query}\n"
                    f"[bold]Intent:[/bold] [#f59e0b]{tr.intent}[/#f59e0b]  "
                    f"[bold]Threshold:[/bold] {tr.threshold:.2f}  "
                    f"[bold]Desperation:[/bold] {'[red]Yes[/red]' if tr.force_desperation else '[dim]No[/dim]'}"
                )
                log.write(RPanel(header, title="Retrieval Trace", border_style="cyan"))
                stage_map = {
                    "classification":  "Classification",
                    "active_search":   "Active Search",
                    "passive_search":  "Passive Search",
                    "semantic_search": "Semantic Search",
                    "desperation":     "Desperation",
                }
                t = RTable(title="Stage Execution", border_style="dim")
                t.add_column("Stage", min_width=16)
                t.add_column("Candidates", justify="right")
                t.add_column("Time (ms)", justify="right", style="#22c55e")
                for key, label in stage_map.items():
                    timing = tr.stage_timings.get(key, "skipped")
                    count = tr.candidate_counts.get(key, "-")
                    fired = key == tr.stage_fired
                    if timing == "skipped":
                        t.add_row(f"[dim]{label}[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
                    else:
                        lbl = f"[bold #22c55e]{label} OK[/bold #22c55e]" if fired else label
                        t.add_row(lbl, str(count), str(timing))
                log.write(t)
                log.write(RText.from_markup(
                    f"  [bold]Fired:[/bold] [#22c55e]{tr.stage_fired}[/#22c55e]  "
                    f"[bold]Total:[/bold] {tr.total_duration_ms} ms\n"
                ))

        elif cmd == "/subagent":
            result = getattr(agent, "last_subagent_result", None)
            gating = getattr(agent, "last_subagent_gating", None)
            if result is None:
                log.write(RText.from_markup("[dim]No subagent execution yet.[/dim]\n"))
            else:
                aal = result.aal_summary
                outcome = aal.get("outcome", "failed")
                oc = "#22c55e" if outcome == "success" else "#f59e0b" if outcome == "partial" else "#ef4444"
                header = RText.from_markup(
                    f"[bold]Task:[/bold]       {aal.get('task')}\n"
                    f"[bold]Skill:[/bold]      [#f59e0b]{aal.get('skill_used')}[/#f59e0b]\n"
                    f"[bold]Outcome:[/bold]    [{oc}]{outcome}[/{oc}]\n"
                    f"[bold]Confidence:[/bold] {aal.get('confidence', 0.0):.2f}\n"
                    f"[bold]Notes:[/bold]      {aal.get('notes', 'None')}"
                )
                log.write(RPanel(header, title="Last Subagent Execution", border_style="cyan"))
                if gating:
                    gt = RTable(title="Memory Gating", border_style="dim")
                    gt.add_column("Status")
                    gt.add_column("Count", justify="right")
                    gt.add_column("Details")
                    acc = gating.get("accepted", [])
                    rej = gating.get("rejected", [])
                    gt.add_row("[#22c55e]Accepted[/#22c55e]", str(len(acc)), (", ".join(acc)[:60] or "-"))
                    gt.add_row("[#ef4444]Rejected[/#ef4444]", str(len(rej)), (", ".join(rej)[:60] or "-"))
                    log.write(gt)
                log.write("")

        elif cmd == "/providers" or cmd.startswith("/provider"):
            from providers_manager import get_manager
            mgr = get_manager()
            t = RTable(title="Athena Providers", border_style="dim", title_style="bold #d4a843")
            t.add_column("ID", style="cyan")
            t.add_column("Name", style="bold white")
            t.add_column("Model", style="#f59e0b")
            t.add_column("Keys", justify="right")
            t.add_column("Enabled")
            t.add_column("Active", style="#a78bfa")
            for pid, p in mgr.providers.items():
                active_mark = ""
                if mgr.active_provider_id == pid:
                    active_mark = "star"
                elif not mgr.active_provider_id:
                    h = mgr.get_healthiest_provider()
                    if h and h.id == pid:
                        active_mark = "star auto"
                enabled_str = "[#22c55e]yes[/#22c55e]" if p.enabled else "[#ef4444]no[/#ef4444]"
                t.add_row(p.id, p.name, p.default_model, str(len(p.api_keys or [])), enabled_str, active_mark)
            log.write(t)
            if mgr.active_model_override:
                log.write(RText.from_markup(
                    f"[dim]Model override: [#22c55e]{mgr.active_model_override}[/#22c55e] (use /model reset to clear)[/dim]\n"
                ))
            log.write("")

        elif cmd.startswith("/model"):
            from providers_manager import get_manager
            mgr = get_manager()
            mparts = raw.split(maxsplit=2)
            if len(mparts) == 1:
                h = mgr.providers.get(mgr.active_provider_id) if mgr.active_provider_id else mgr.get_healthiest_provider()
                current = mgr.active_model_override or (h.default_model if h else "unknown")
                log.write(RText.from_markup(f"[dim]Current model:[/dim] [#22c55e]{current}[/#22c55e]\n"))
            else:
                target = mparts[-1].strip()
                if target.lower() in ("default", "auto", "reset"):
                    mgr.active_model_override = None
                    mgr.save_providers()
                    log.write(RText.from_markup("[#22c55e]Model override cleared.[/#22c55e]\n"))
                else:
                    mgr.active_model_override = target
                    mgr.save_providers()
                    log.write(RText.from_markup(f"[#22c55e]Model set to '{target}'.[/#22c55e]\n"))
                self._refresh_header()

        else:
            log.write(RText.from_markup(f"[#374151]Unknown command: {raw}[/#374151]\n"))

        self._refresh_status()

    def _emit_user_message(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(RText.from_markup(f"\n[bold #22c55e]You[/bold #22c55e]  [dim]--[/dim]  {text}\n"))

    def _emit_athena_message(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(RText.from_markup(f"[bold #d4a843]Athena[/bold #d4a843]  [dim]--[/dim]\n"))
        log.write(text + "\n")

    def _emit_error(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(RText.from_markup(f"[bold #ef4444]Error[/bold #ef4444]  {text}\n"))

    def _set_thinking(self, thinking: bool) -> None:
        row = self.query_one("#thinking-row")
        row.display = thinking
        inp = self.query_one("#chat-input", Input)
        inp.disabled = thinking

    @work(thread=True)
    def _run_agent(self, user_input: str) -> None:
        self.call_from_thread(self._set_thinking, True)
        try:
            response = self._app.agent.run_one_turn(user_input)
            self.call_from_thread(self._set_thinking, False)
            self.call_from_thread(self._emit_athena_message, response)
            self.call_from_thread(self._refresh_status)
        except KeyboardInterrupt:
            self.call_from_thread(self._set_thinking, False)
            self.call_from_thread(
                lambda: self.query_one("#chat-log", RichLog).write(
                    RText.from_markup("[#f59e0b]Generation interrupted.[/#f59e0b]\n")
                )
            )
        except Exception as exc:
            self.call_from_thread(self._set_thinking, False)
            self.call_from_thread(self._emit_error, str(exc))
        finally:
            self.call_from_thread(lambda: self.query_one("#chat-input", Input).focus())


class AthenaApp(App):
    CSS = CSS
    TITLE = f"Athena {VERSION}"

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("ctrl+n", "new_chat",         "New Chat",  show=False),
        Binding("ctrl+k", "providers",        "Providers", show=False),
        Binding("ctrl+c", "interrupt_agent",  "Interrupt", show=False),
    ]

    def __init__(self, project_id: str = "default", session_id: str = "session_1") -> None:
        super().__init__()
        self.project_id = project_id
        self.session_id = session_id
        self.agent: Optional[AthenaAgent] = None

    def compose(self) -> ComposeResult:
        yield ChatScreen(self)

    def on_mount(self) -> None:
        try:
            config.ensure_athena_dirs()
            memory_engine.initialize_db()
        except Exception:
            pass
        try:
            import memory_sweep
            memory_sweep.run_memory_sweep()
        except Exception:
            pass
        try:
            config.fetch_caveman_skills(force=False)
        except Exception:
            pass
        self.agent = AthenaAgent(project_id=self.project_id, session_id=self.session_id)
        try:
            chat = self.query_one(ChatScreen)
            chat._refresh_header()
            chat._refresh_status()
        except Exception:
            pass

    def action_command_palette(self) -> None:
        chat = self.query_one(ChatScreen)
        self.push_screen(CommandPaletteScreen(chat))

    def action_new_chat(self) -> None:
        self.query_one(ChatScreen).execute_command("/newchat")

    def action_providers(self) -> None:
        self.query_one(ChatScreen).execute_command("/providers")

    def action_interrupt_agent(self) -> None:
        self.query_one(ChatScreen)._set_thinking(False)


def run(project_id: str = "default", session_id: str = "session_1") -> None:
    AthenaApp(project_id=project_id, session_id=session_id).run()
