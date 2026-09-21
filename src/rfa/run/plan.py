#!/usr/bin/env python3

"""Turn a draft into an execution packet: `rfa-plan -r ../invoicing -t "let users duplicate a line"`."""

from pathlib import Path

import typer
import yaml
from rich.console import Console

from minisweagent.environments import get_environment
from minisweagent.models import get_model
from rfa.planner import plan, write_packet

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "planner.yaml"

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich")


def parse_repo(spec: str) -> tuple[str, tuple[Path, str]]:
    """`path`, `path@ref` or `name=path@ref`. The name is what packet paths must start with."""
    name, _, rest = spec.rpartition("=")
    path, _, ref = rest.partition("@")
    return name or Path(path).resolve().name, (Path(path).resolve(), ref or "HEAD")


# fmt: off
@app.command(help=__doc__)
def main(
    task: str = typer.Option(..., "-t", "--task", help="The draft: a rough idea, in plain words", prompt="What is the idea?"),
    repos: list[str] = typer.Option(..., "-r", "--repo", help="Repository to read: [bold green]path[/], [bold green]path@ref[/] or [bold green]name=path@ref[/]"),
    model_name: str | None = typer.Option(None, "-m", "--model", help="Model to plan with"),
    output: Path = typer.Option(Path("packet.md"), "-o", "--output", help="Where to write the rendered packet"),
    config_path: Path = typer.Option(DEFAULT_CONFIG, "-c", "--config", help="Planner config"),
):
    # fmt: on
    config = yaml.safe_load(config_path.read_text())
    env = get_environment(config.get("environment", {}), default_type="docker")
    agent = plan(
        task,
        dict(parse_repo(spec) for spec in repos),
        get_model(model_name, config.get("model", {})),
        env,
        **config.get("agent", {}),
    )
    if agent.packet is None:
        console.print(f"[bold red]No packet.[/] {agent.messages[-1].get('content', '')}")
        raise typer.Exit(1)
    write_packet(agent, output)
    console.print(
        f"\n[bold green]Packet written to[/] [bold]{output}[/] "
        f"(complexity {agent.packet.complexity}/5, {agent.n_actions} tool calls, ${agent.cost:.2f})"
    )
    if agent.packet.open_questions:
        console.print("\n[bold yellow]Open questions:[/]")
        for question in agent.packet.open_questions:
            console.print(f"  - {question}")


if __name__ == "__main__":
    app()
