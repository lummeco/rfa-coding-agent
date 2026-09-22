"""`rfa up`: check everything the pipeline needs, make what can be made, and open the board.

Each check answers one question and, when the answer is no, says the one command that fixes it.
Nothing here is a daemon or a supervisor: Docker and Ollama are already services on this Mac, so
`up` makes sure they are reachable and that the image and the model variant exist, and stops there.
"""

import shutil
import subprocess
from dataclasses import dataclass, field

from rfa import settings, tasks
from rfa.sentry import SentryConfig, SentryError, token


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    """The command that would make this pass. Shown only when it did not."""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


def sh(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def workspace() -> Step:
    tasks.init()
    counts = ", ".join(f"{len(tasks.tasks(s))} {s}" for s in tasks.STAGES if tasks.tasks(s))
    return Step("workspace", True, f"{tasks.home()}" + (f" — {counts}" if counts else " — empty"))


def repos(config: dict) -> Step:
    names = list(config.get("repos") or {})
    if not names:
        return Step("repos", False, "none configured", f"add `repos:` to {settings.path()}")
    try:
        resolved = settings.repo_paths(config, names)
    except KeyError as e:
        return Step("repos", False, str(e), f"fix `repos:` in {settings.path()}")
    missing = [f"{n} ({p})" for n, (p, _) in resolved.items() if not (p / ".git").is_dir()]
    if missing:
        return Step("repos", False, f"not checked out: {', '.join(missing)}", "clone them, or remove them")
    return Step("repos", True, f"{len(resolved)} available")


def docker(images: list[str]) -> Step:
    if not shutil.which("docker"):
        return Step("docker", False, "not installed", "install Docker Desktop, OrbStack or colima")
    if sh("docker", "info").returncode != 0:
        return Step("docker", False, "not running", "start Docker and run `rfa up` again")
    for image in images:
        if sh("docker", "image", "inspect", image).returncode != 0:
            if (pull := sh("docker", "pull", image, timeout=900)).returncode != 0:
                return Step("docker", False, f"cannot pull {image}", pull.stderr.strip()[:200])
    return Step("docker", True, f"running — {', '.join(images)}")


def ollama(config: dict) -> Step:
    """Every model under `models:` must be served, and served as the variant with the big context.

    Ollama gives a model a small default context unless told otherwise and silently drops the start
    of anything longer -- and mini's instructions and tools are ~17k tokens before any work. So each
    preset names a pulled base and the context to re-serve it with, and `up` creates what is missing.
    """
    wanted = {name: preset["ollama"] for name, preset in (config.get("models") or {}).items() if preset.get("ollama")}
    if not wanted:
        return Step("ollama", True, "no variants configured; using the model names as given")
    if not shutil.which("ollama"):
        return Step("ollama", False, "not installed", "https://ollama.com/download")
    if (listed := sh("ollama", "list")).returncode != 0:
        return Step("ollama", False, "not running", "start Ollama and run `rfa up` again")
    have = {line.split()[0].split(":")[0] for line in listed.stdout.splitlines()[1:] if line.split()}
    made = []
    for preset, variant in wanted.items():
        name, base = variant["name"], variant.get("from", "")
        if name.split(":")[0] in have:
            continue
        if base.split(":")[0] not in have:
            return Step("ollama", False, f"{base} not pulled (for {preset})", f"ollama pull {base}")
        modelfile = tasks.home() / "var" / f"Modelfile.{name}"
        modelfile.parent.mkdir(parents=True, exist_ok=True)
        modelfile.write_text(f"FROM {base}\nPARAMETER num_ctx {variant.get('num_ctx', 131072)}\n")
        if (result := sh("ollama", "create", name, "-f", str(modelfile), timeout=600)).returncode != 0:
            return Step("ollama", False, f"could not create {name}", result.stderr.strip()[:200])
        made.append(f"{name} from {base}")
    ready = ", ".join(v["name"] for v in wanted.values())
    return Step("ollama", True, f"{ready} ready" + (f" (created {', '.join(made)})" if made else ""))


def sentry(config: dict) -> Step:
    """Only with a `sentry:` block: a token to ask with, and a repository in `repos:` for every project.

    Checked here, with the fix, rather than found out from a line in the daemon's log every half hour.
    """
    if (polling := SentryConfig.load(config)) is None:
        return Step("sentry", True, "not configured")
    if unknown := [r for r in polling.projects.values() if r not in (config.get("repos") or {})]:
        return Step(
            "sentry", False, f"not under `repos:`: {', '.join(unknown)}", f"fix `sentry.projects:` in {settings.path()}"
        )
    try:
        token(polling)
    except SentryError as e:
        return Step("sentry", False, str(e))
    return Step("sentry", True, f"{', '.join(polling.projects) or 'no projects'} every {polling.interval // 60} min")


def menubar() -> Step:
    """Open the menu bar app, if it has been built.

    Not part of `check()`: that only looks, and this starts something. `open -a` on an app that is
    already running just brings it forward, so `rfa up` twice leaves one of it.
    """
    bundle = tasks.home() / "build" / "RFA.app"
    if not bundle.is_dir():
        return Step("menubar", True, "not built — run `./ui/build.sh` for the ⌥Space capture box")
    if (opened := sh("open", "-a", str(bundle))).returncode != 0:
        return Step("menubar", False, "would not open", opened.stderr.strip()[:200] or "see Console.app")
    return Step("menubar", True, "running — ⌥Space for a new idea")


def images(configs: list[dict]) -> list[str]:
    """The images a run could need. The reviewer's carries a browser and is the size of one, so it
    is only worth pulling once something is configured for it to drive."""
    return list(dict.fromkeys(c["environment"]["image"] for c in configs if c is not None))


def check() -> Report:
    """Everything `rfa up` verifies, without starting anything."""
    planner, coder = settings.load("planner"), settings.load("coder")
    reviewer = settings.load("reviewer") if coder.get("apps") else None
    report = Report()
    report.add(workspace())
    report.add(repos(coder))
    report.add(docker(images([planner, coder, reviewer])))
    report.add(ollama(coder))
    report.add(sentry(coder))
    return report
