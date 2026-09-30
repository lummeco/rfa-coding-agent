"""Setup run once and kept as an image: rebuilt on what setup reads, and on nothing else."""

import subprocess

import pytest

from rfa import prepare
from rfa.planner import git

CONFIG = {"environment_class": "docker", "image": "python:3.11-bookworm", "cwd": "/work/repos"}
SPEC = {"setup": ["pip install -r lummeco-base/requirements.txt"]}


def commit(path, **files) -> str:
    """Write `files` into the repository at `path` and commit them; the new sha."""
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    git(path, "add", "-A")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "change")
    return git(path, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    return tmp_path


def test_only_what_setup_reads_builds_a_new_image(repo):
    base = commit(repo, **{"lummeco-base/requirements.txt": "httpx\n", "lummeco-base/src/app.py": "a = 1\n"})
    code = commit(repo, **{"lummeco-base/src/app.py": "a = 2\n"})
    locked = commit(repo, **{"lummeco-base/requirements.txt": "httpx\nruff\n"})

    def key(ref: str, spec: dict = SPEC, config: dict = CONFIG) -> str:
        return prepare.key(config, spec, {"base": (repo, ref)})

    assert key(base) == key(code)
    assert key(code) != key(locked)
    assert key(base) != key(base, {"setup": ["pip install -r lummeco-base/requirements.txt", "true"]})
    assert key(base) != key(base, config=CONFIG | {"image": "python:3.12-bookworm"})
    assert key(base) != key(base, config=CONFIG | {"env": {"PIP_INDEX_URL": "http://mirror"}})
    assert prepare.image(CONFIG, {}, {"base": (repo, base)}, "base") == CONFIG["image"]


def docker() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.slow
@pytest.mark.skipif(not docker(), reason="needs a running docker")
def test_the_image_keeps_what_setup_made_and_none_of_the_tree(repo):
    config = {"environment_class": "docker", "image": "debian:bookworm-slim", "cwd": "/work/repos"}
    spec = {"setup": ["mkdir -p deps && cat requirements.txt > deps/installed && echo touched >> gone.txt"]}
    first = commit(repo, **{"requirements.txt": "one\n", "gone.txt": "old\n"})
    tag = prepare.image(config, spec, {"web": (repo, first)}, "Web")
    try:
        listed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                tag,
                "sh",
                "-c",
                "cd /work/repos/web && find . -type f | sort && cat deps/installed",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert listed.stdout.split() == ["./deps/installed", "one"]
        assert tag.startswith("rfa-setup/web:")
        assert prepare.image(config, spec, {"web": (repo, commit(repo, **{"gone.txt": "new\n"}))}, "Web") == tag
    finally:
        subprocess.run(["docker", "rmi", tag], capture_output=True)
