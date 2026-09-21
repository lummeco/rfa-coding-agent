"""`rfa up`: check everything the pipeline needs, make what can be made, and open the board.

Each check answers one question and, when the answer is no, says the one command that fixes it.
Nothing here is a daemon or a supervisor: Docker and Ollama are already services on this Mac, so
`up` makes sure they are reachable and that the image and the model variant exist, and stops there.
"""

import shutil
import subprocess
from dataclasses import dataclass, field

from rfa import settings, tasks


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
    """The served model must exist and must be the variant with the big context window."""
    wanted = config.get("ollama") or {}
    name = wanted.get("name")
    if not name:
        return Step("ollama", True, "no variant configured; using the model name as given")
    if not shutil.which("ollama"):
        return Step("ollama", False, "not installed", "https://ollama.com/download")
    if (listed := sh("ollama", "list")).returncode != 0:
        return Step("ollama", False, "not running", "start Ollama and run `rfa up` again")
    have = {line.split()[0].split(":")[0] for line in listed.stdout.splitlines()[1:] if line.split()}
    if name.split(":")[0] in have:
        return Step("ollama", True, f"{name} ready")
    base = wanted.get("from", "")
    if base.split(":")[0] not in have:
        return Step("ollama", False, f"{base} not pulled", f"ollama pull {base}")
    modelfile = tasks.home() / "var" / f"Modelfile.{name}"
    modelfile.write_text(f"FROM {base}\nPARAMETER num_ctx {wanted.get('num_ctx', 131072)}\n")
    if (made := sh("ollama", "create", name, "-f", str(modelfile), timeout=600)).returncode != 0:
        return Step("ollama", False, f"could not create {name}", made.stderr.strip()[:200])
    return Step("ollama", True, f"{name} created from {base}")


def check() -> Report:
    """Everything `rfa up` verifies, without starting anything."""
    planner, coder = settings.load("planner"), settings.load("coder")
    report = Report()
    report.add(workspace())
    report.add(repos(coder))
    report.add(docker([planner["environment"]["image"], coder["environment"]["image"]]))
    report.add(ollama(coder))
    return report
