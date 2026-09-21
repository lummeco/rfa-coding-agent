#!/usr/bin/env python3

"""`rfa` -- the whole pipeline from one command.

rfa up                      check everything, then open the board
rfa init                    make the task folders here
rfa new "let users ..."     capture an idea as a draft
rfa ls                      the board, in the terminal
rfa show <id>               one task, in full
rfa plan [id ...]           write packets for cards waiting in planning
rfa approve <id>            you read the packet; it becomes work
rfa work [id]               code the next approved task (default: the oldest)
rfa mv <id> <stage>         move a task by hand
rfa board                   the board, in a browser
"""

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
    "failed": "red",
    "planning": "yellow",
    "under-work": "cyan",
    "done": "green",
}

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich", no_args_is_help=True, help=__doc__)


@app.command()
def up(
    board_too: bool = typer.Option(True, "--board/--no-board", help="Open the board when everything checks out"),
    port: int = typer.Option(4380, "-p", "--port"),
):
    """Check everything the pipeline needs, make what can be made, and open the board."""
    from rfa.up import check

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
    console.print('\n[bold green]Ready.[/] Capture an idea on the board, or with `rfa new "..."`.')
    console.print("[dim]Then: plan it, approve it, and `rfa work`.[/]")
    if board_too:
        from rfa.board import serve

        serve(port=port)


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
):
    """Capture an idea as a draft."""
    console.print(f"[bold green]Draft[/] {tasks.create(idea, list(repo)).id}")


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
def plan(ids: list[str] = typer.Argument(None, help="Task ids; omit for every card waiting in planning")):
    """Write execution packets for the cards you moved into planning."""
    config = settings.load("planner")
    targets = [tasks.find(i) for i in ids] if ids else [t for t in tasks.tasks("planning") if t.status == "queued"]
    if not targets:
        console.print("[dim]Nothing waiting. Move a draft in first: `rfa mv <id> planning`[/]")
        return
    for task in targets:
        console.print(f"[bold]Planning[/] {task.id}")
        planned = plan_task(task, config)
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
def work(id: str = typer.Argument("", help="A todo id; omit for the oldest approved task")):
    """Code an approved task: the packet goes in, a reviewed diff comes out."""
    from rfa.worker import run_task

    queue = tasks.tasks("todo")
    task = tasks.find(id) if id else (queue[0] if queue else None)
    if task is None:
        console.print("[dim]Nothing approved. `rfa approve <id>` first.[/]")
        return
    console.print(f"[bold]Working[/] {task.title}")
    done = run_task(task, settings.load("coder"))
    if done.status == "shipped":
        console.print(f"[bold green]Shipped[/] — patches in [bold]{done.meta['run']}[/]")
    else:
        console.print(f"[bold red]Failed[/] — {done.meta.get('error', '')}\n[dim]{done.meta['run']}[/]")


@app.command("mv")
def move(id: str, stage: str):
    """Move a task by hand."""
    if stage not in tasks.STAGES:
        raise typer.BadParameter(f"stage must be one of {', '.join(tasks.STAGES)}")
    console.print(f"[bold green]{tasks.move(tasks.find(id), stage, actor='human').id}[/] -> {stage}")


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
