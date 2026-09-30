"""A repository's `setup:`, run once and kept as a local image.

Setup is where a run needs the network -- npm, pip, apt -- and it does the same thing every time
until the lockfiles change. So the host runs it once in a throwaway container and saves the result
as an image named after everything that went into it. Every run after that starts from the image:
minutes faster online, and able to start at all offline.

The image holds what setup produced and none of the repository itself. The tracked files are
deleted before it is saved, and every run seeds its own commit over what is left, so a file that
a newer commit deleted cannot linger from the commit the image was built at.

Only the filesystem is saved. A service that setup started is not running in the next container,
which is what `start:` is for: it runs at the top of every run. Setup should stop what it started,
so the image does not hold a database in the middle of a write.
"""

import hashlib
import json
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from minisweagent import Environment
from minisweagent.environments import get_environment
from minisweagent.environments.docker import DockerEnvironmentConfig
from rfa.planner import REPOS_DIR, git, seed, tracked_files

LOCKFILES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "go.sum",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
)
"""What setup reads, anywhere in the tree. A change to any of them, and only to them, builds anew."""

KEEP = 3
"""Images kept per repository. More than one, so that branches with different lockfiles do not
rebuild each other's away."""


def key(config: dict, spec: dict, repos: dict[str, tuple[Path, str]]) -> str:
    """Everything the image depends on: the base image, setup, its variables and the lockfiles'
    blobs. `ls-tree` gives each file's blob sha, so nothing has to be read to notice a change."""
    locks = {
        name: [
            line
            for line in git(path, "ls-tree", "-r", ref).splitlines()
            if any(fnmatch(line.rpartition("\t")[2].rpartition("/")[2], p) for p in LOCKFILES)
        ]
        for name, (path, ref) in sorted(repos.items())
    }
    inputs = {"image": config["image"], "setup": spec["setup"], "env": config.get("env", {}), "locks": locks}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]


def image(config: dict, spec: dict, repos: dict[str, tuple[Path, str]], name: str) -> str:
    """The image a run of `name` starts from, built first if nothing has been built for these inputs.

    `repos` are the run's work repositories, pinned: setup may read a neighbour, so all of them are
    seeded and all of their lockfiles count.
    """
    if not spec.get("setup"):
        return config["image"]
    executable = DockerEnvironmentConfig(**config).executable
    tag = f"rfa-setup/{name.lower()}:{key(config, spec, repos)}"
    if subprocess.run([executable, "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    env = get_environment(config, default_type="docker")
    try:
        seed(env, repos)
        run(env, spec["setup"], f"{REPOS_DIR}/{name}", spec.get("setup_timeout", 900))
        for repo, (path, ref) in repos.items():
            subprocess.run(
                [
                    executable,
                    "exec",
                    "-i",
                    "-w",
                    f"{REPOS_DIR}/{repo}",
                    env.container_id,
                    *"xargs -0 rm -rf --".split(),
                ],
                input="\0".join(tracked_files(path, ref)).encode(),
                check=True,
                capture_output=True,
            )
        subprocess.run([executable, "commit", env.container_id, tag], check=True, capture_output=True, timeout=900)
    finally:
        env.cleanup()
    prune(executable, tag)
    return tag


def prune(executable: str, tag: str) -> None:
    """All but the newest KEEP images of this repository. One still in use by a run stays."""
    repository = tag.rpartition(":")[0]
    listed = subprocess.run([executable, "images", repository, "--format", "{{.Tag}}"], capture_output=True, text=True)
    for old in listed.stdout.split()[KEEP:]:
        subprocess.run([executable, "rmi", f"{repository}:{old}"], capture_output=True)


def run(env: Environment, commands: list[str], cwd: str, timeout: int) -> None:
    """`setup:` or `start:`, one command at a time, stopping at the first that fails."""
    for command in commands:
        result = env.execute({"command": command}, cwd=cwd, timeout=timeout)
        if result["returncode"] != 0:
            raise RuntimeError(f"`{command}` failed setting up the container:\n\n{result['output'][-3000:]}")
