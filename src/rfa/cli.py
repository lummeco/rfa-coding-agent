#!/usr/bin/env python3

"""`rfa` -- the whole pipeline from one command.

rfa up                      check everything, start the daemon and the board
rfa down                    stop them
rfa restart                 stop them and start them again
rfa status                  what is running, what the gates say, what is on the board
rfa init                    make the task folders here
rfa new "let users ..."     capture an idea as a draft
rfa sentry                  capture Sentry's unresolved issues as drafts, now rather than on the daemon's clock
rfa ls                      the board, in the terminal
rfa show <id>               one task, in full
rfa plan [id ...]           write packets for cards waiting in planning
rfa approve <id>            you read the packet; it becomes work
rfa work [id]               code the next approved task (default: the oldest)
rfa review [id]             drive the app the coder changed and judge it
rfa branches [repo ...]     the branches you can start work from
rfa models                  the models you can run with
rfa model [name]            which one new runs use
rfa reasoning [level]     which level new runs use
rfa mv <id> <stage>         move a task by hand
rfa board                   the board, in a browser
rfa daemon                  the loop `rfa up` runs in the background, in the foreground
"""

import json
import subprocess
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from rfa import settings, tasks
from rfa.planner import plan_task

STATUS_STYLE = {
    "ready": "green",
    "shipped": "green",
    "coding": "cyan",
    "reviewing": "cyan",
    "failed": "red",
    "planning": "yellow",
    "under-work": "cyan",
    "done": "green",
}

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich", no_args_is_help=True, help=__doc__)


@app.command()
def up(
    port: int = typer.Option(4380, "-p", "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the board once it is up"),
):
    """Check everything the pipeline needs, then start the daemon and the board."""
    from rfa.service import forget, services, url
    from rfa.up import check, menubar

    console.print("[bold]Bringing rfa up[/]\n")
    report = check()
    for step in report.steps:
        mark = "[green]✓[/]" if step.ok else "[red]✗[/]"
        console.print(f"  {mark} [bold]{step.name:<10}[/] {step.detail}")
        if not step.ok and step.fix:
            console.print(f"    [dim]→ {step.fix}[/]")
    if not report.ok:
        console.print("\n[bold red]Not ready.[/] Fix the above and run [bold]rfa up[/] again.")
        raise typer.Exit(1)
    forget()  # the board mints a fresh token at startup and writes the address back
    for service in services(port):
        detail = "started" if service.start() else "already running"
        if not (pid := service.pid()):
            console.print(f"  [red]✗[/] [bold]{service.name:<10}[/] would not stay up — see {service.log}")
            raise typer.Exit(1)
        console.print(f"  [green]✓[/] [bold]{service.name:<10}[/] {detail} (pid {pid}) — {service.log}")
    app = menubar()
    console.print(f"  {'[green]✓[/]' if app.ok else '[red]✗[/]'} [bold]{app.name:<10}[/] {app.detail}")
    if not (address := url()):
        console.print(f"\n[bold red]The board did not say where it is.[/] See {services(port)[1].log}")
        raise typer.Exit(1)
    console.print(f"\n[bold green]Up.[/] Board on [bold]{address}[/] — the daemon plans and codes from here on.")
    console.print("[dim]The token in that link is this board's; `rfa status --json` prints it again.[/]")
    if open_browser:
        webbrowser.open(address)


@app.command()
def down():
    """Stop the daemon and the board. Nothing in `tasks/` changes."""
    from rfa.service import forget, services

    forget()
    for service in reversed(services()):
        state = "[bold green]stopped[/]" if service.stop() else "[dim]not running[/]"
        console.print(f"  [bold]{service.name:<10}[/] {state}")
    # The menu bar app is left alone on purpose: it is how you bring this back up, and it costs
    # nothing while nothing is running. Quit it from its own menu.
    console.print("  [bold]menubar   [/] [dim]left running — quit it from its own menu[/]")


@app.command()
def restart(
    port: int = typer.Option(4380, "-p", "--port"),
    open_browser: bool = typer.Option(False, "--open/--no-open", help="Open the board once it is up"),
):
    """Stop everything and bring it back up -- what to run after editing `rfa.yaml`."""
    down()
    up(port=port, open_browser=open_browser)


@app.command()
def status(as_json: bool = typer.Option(False, "--json", help="The same thing, for the menu bar")):
    """What is running, what the gates say, and what is on the board."""
    from rfa.daemon import report

    payload = report()
    if as_json:
        print(json.dumps(payload))
        return
    for name, pid in payload["services"].items():
        console.print(f"  [bold]{name:<10}[/] " + (f"[green]running[/] (pid {pid})" if pid else "[dim]stopped[/]"))
    for gate in payload["gates"]:
        mark = "[green]✓[/]" if gate["ok"] else "[red]✗[/]"
        console.print(f"  {mark} [bold]{gate['name']:<10}[/] {gate['detail']}")
    console.print(f"  [bold]{'model':<10}[/] {payload['model'] or '[dim]none configured[/]'}")
    console.print("  " + "  ".join(f"[dim]{stage}[/] {n}" for stage, n in payload["stages"].items()))
    if payload["next"]:
        console.print(f"\n  next: [bold]{payload['next']['job']}[/] {payload['next']['title']}")


@app.command()
def daemon():
    """The loop `rfa up` runs in the background. Run it here to watch it decide."""
    from rfa.daemon import Daemon

    Daemon().serve()


@app.command()
def init():
    """Make the task folders in the current directory (or $RFA_HOME)."""
    tasks.init()
    console.print(f"[bold green]Ready.[/] Tasks live in [bold]{tasks.home() / 'tasks'}[/]")
    if not settings.path().exists():
        console.print(f"Add your repositories to [bold]{settings.path()}[/] under [bold]repos:[/]")


@app.command()
def new(
    idea: str = typer.Argument(..., help="The idea, in plain words"),
    repo: list[str] = typer.Option([], "-r", "--repo", help="Repository this touches, as in rfa.yaml"),
    branch: list[str] = typer.Option([], "-b", "--branch", help="owner/name@branch the work starts from"),
    context: list[str] = typer.Option([], "-c", "--context", help="owner/name@branch to read beside it"),
    model: str = typer.Option("", "-m", "--model", help="Run this card with a particular model"),
    reasoning: str = typer.Option("", "-R", "--reasoning", help="none | low | medium | high"),
):
    """Capture an idea as a draft."""
    settings.validate(settings.load(), model, reasoning)
    task = tasks.create(
        idea,
        list(repo),
        branches=list(branch) or None,
        context_branches=list(context)[: settings.MAX_CONTEXT] or None,
        model=model or None,
        reasoning=reasoning or None,
    )
    console.print(f"[bold green]Draft[/] {task.id}")


@app.command()
def sentry():
    """Ask Sentry now rather than on the daemon's clock: each issue with no card yet becomes a draft."""
    from rfa.sentry import SentryConfig, pull, token

    if (config := SentryConfig.load()) is None:
        console.print(f"[dim]No `sentry:` in {settings.path()}.[/]")
        return
    if not (drafted := pull(config, token(config))):
        console.print("[dim]Nothing new.[/]")
    for task in drafted:
        console.print(f"[bold green]Draft[/] {task.id}")


@app.command("ls")
def list_tasks(stage: str = typer.Argument("", help="Only this stage")):
    """The board, in the terminal."""
    found = tasks.tasks(stage)
    if not found:
        console.print('[dim]Nothing here yet. `rfa new "..."` to start one.[/]')
        return
    for name in [stage] if stage else tasks.STAGES:
        if not (in_stage := [t for t in found if t.stage == name]):
            continue
        table = Table(title=f"{name}  ({len(in_stage)})", title_justify="left", box=None, pad_edge=False)
        table.add_column("", style="dim", no_wrap=True)
        table.add_column("")
        table.add_column("", style="dim")
        for task in in_stage:
            retries = f"  ↻{task.attempts}" if task.attempts else ""
            style = STATUS_STYLE.get(task.status, "dim")
            table.add_row(task.id[:15], task.title, f"[{style}]{task.status}[/]{retries}")
        console.print(table)
        console.print()


@app.command()
def show(id: str):
    """One task, in full."""
    task = tasks.find(id)
    console.print(Panel(task.body.strip(), title=f"{task.stage} · {task.status}", subtitle=task.id))


@app.command()
def plan(
    ids: list[str] = typer.Argument(None, help="Task ids; omit for every card waiting in planning"),
    model: str = typer.Option("", "-m", "--model", help="A name from `models:` in rfa.yaml"),
    reasoning: str = typer.Option("", "-R", "--reasoning", help="none | low | medium | high"),
):
    """Write execution packets for the cards you moved into planning."""
    config = settings.load("planner")
    targets = [tasks.find(i) for i in ids] if ids else [t for t in tasks.tasks("planning") if t.status == "queued"]
    if not targets:
        console.print("[dim]Nothing waiting. Move a draft in first: `rfa mv <id> planning`[/]")
        return
    for task in targets:
        console.print(f"[bold]Planning[/] {task.id}")
        planned = plan_task(task, config, model, reasoning)
        style = "green" if planned.status == "ready" else "red"
        console.print(f"  [{style}]{planned.status}[/] {planned.title}")


@app.command()
def approve(id: str):
    """You read the packet and it is right: make it work the coder can pick up."""
    task = tasks.find(id)
    if task.status == "failed":
        raise typer.BadParameter(f"{id} has no packet to approve ({task.meta.get('error', '')})")
    tasks.move(task, "todo", actor="human", status="todo", approved_at=tasks.now())
    console.print(f"[bold green]Approved[/] {task.title}")


@app.command()
def work(
    id: str = typer.Argument("", help="A todo id; omit for the oldest approved task"),
    model: str = typer.Option("", "-m", "--model", help="A name from `models:` in rfa.yaml"),
    reasoning: str = typer.Option("", "-R", "--reasoning", help="none | low | medium | high"),
):
    """Code an approved task: the packet goes in, a reviewed diff comes out."""
    from rfa.worker import run_task

    queue = tasks.tasks("todo")
    task = tasks.find(id) if id else (queue[0] if queue else None)
    if task is None:
        console.print("[dim]Nothing approved. `rfa approve <id>` first.[/]")
        return
    console.print(f"[bold]Working[/] {task.title}")
    done = run_task(task, settings.load("coder"), model, reasoning)
    if done.status == "shipped":
        console.print(f"[bold green]Shipped[/] — patches in [bold]{done.meta['run']}[/]")
    else:
        console.print(f"[bold red]Failed[/] — {done.meta.get('error', '')}\n[dim]{done.meta['run']}[/]")


@app.command()
def review(
    id: str = typer.Argument("", help="A card in review; omit for the oldest waiting one"),
    model: str = typer.Option("", "-m", "--model", help="A name from `models:` in rfa.yaml"),
    reasoning: str = typer.Option("", "-R", "--reasoning", help="none | low | medium | high"),
):
    """Start the app the coder changed, drive it through a browser, and judge the packet."""
    from rfa.reviewer import review_task

    queue = [t for t in tasks.tasks("review") if t.status == "queued"]
    task = tasks.find(id) if id else (queue[0] if queue else None)
    if task is None:
        console.print("[dim]Nothing to review. The coder puts cards here when the work lands.[/]")
        return
    console.print(f"[bold]Reviewing[/] {task.title}")
    judged = review_task(task, settings.load("reviewer"), model, reasoning)
    if judged.stage == "done":
        console.print(f"[bold green]Passed[/] — screenshots in [bold]{judged.meta['review']}[/]")
    elif judged.stage == "todo":
        console.print(f"[bold yellow]Sent back[/] — {judged.meta.get('error', '')}")
    else:
        console.print(f"[bold red]Could not review[/] — {judged.meta.get('error', '')}")


@app.command("mv")
def move(id: str, stage: str):
    """Move a task by hand."""
    if stage not in tasks.STAGES:
        raise typer.BadParameter(f"stage must be one of {', '.join(tasks.STAGES)}")
    console.print(f"[bold green]{tasks.move(tasks.find(id), stage, actor='human').id}[/] -> {stage}")


@app.command("branches")
def list_branches(
    repos: list[str] = typer.Argument(None, help="Repository ids as in rfa.yaml; omit for all of them"),
    as_json: bool = typer.Option(False, "--json", help="For the capture overlay"),
):
    """The branches you can start work from, live from each repository's own remote."""
    from rfa.planner import options

    found = options(settings.load(), list(repos) or None)
    if as_json:
        print(json.dumps(found))
        return
    for name in dict.fromkeys(b["repo"] for b in found["branches"]):
        mine = [b for b in found["branches"] if b["repo"] == name]
        listed = "  ".join(f"[bold]{b['branch']}[/]" if b["default"] else b["branch"] for b in mine)
        console.print(f"  [dim]{name}[/]  {listed}")
    for problem in found["errors"]:
        console.print(f"  [red]✗[/] {problem}")


@app.command("models")
def list_models():
    """The models you can run with, and what each one is served as."""
    config = settings.load()
    for name, preset in sorted(settings.presets(config).items()):
        mark = "[green]●[/]" if name == settings.pick(config) else "[dim]○[/]"
        served = (preset.get("ollama") or {}).get("from", preset.get("model_name", ""))
        console.print(f"  {mark} [bold]{name:<12}[/] {served}  [dim]reasoning {preset.get('reasoning', 'default')}[/]")


@app.command("model")
def set_model(name: str = typer.Argument("", help="A name from `models:`; omit to see the current one")):
    """Which model new runs use. Overrides `default_model:` until you change it again."""
    config = settings.load()
    if not name:
        console.print(f"[bold]{settings.pick(config)}[/]")
        return
    settings.choose(settings.pick(config, name))
    console.print(f"[bold green]{name}[/] — planning and coding use it from the next run on.")


@app.command("reasoning")
def set_reasoning(level: str = typer.Argument("", help="none | low | medium | high; omit to see the current one")):
    """Which reasoning level new runs use. Overrides each model's own level until you change it again."""
    if not level:
        console.print(f"[bold]{settings.chosen_reasoning() or 'none'}[/]")
        return
    if level not in settings.REASONING:
        raise typer.BadParameter(f"level must be one of {', '.join(settings.REASONING)}")
    settings.choose_reasoning(level)
    console.print(f"[bold green]{level}[/] — planning and coding use it from the next run on.")


@app.command()
def board(
    port: int = typer.Option(4380, "-p", "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open it in your browser"),
):
    """The board, in a browser."""
    from rfa.board import serve

    serve(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()
