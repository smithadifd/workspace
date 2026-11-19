"""Release plumbing: one version kept in two files, the release-please setup that
moves both, and the pull-request title check.

The title tests run the workflow's own `run` step through bash, so the table
exercises the text the workflow runs, not a copy of it.
"""
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

import workspace

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
INIT_FILE = REPO_ROOT / "workspace" / "__init__.py"

# The two spellings release-please's updaters can rewrite: x.y.z, or x.y.z-pre.
# A dotted dev suffix (x.y.z.dev0) is left unchanged by both, so it is refused.
MOVABLE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-\w+)?$")
VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"  # x-release-please-version$', re.M)


def load_workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow):
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def load_json(name):
    return json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))


def test_version_sources_are_equal():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert workspace.__version__ == pyproject["project"]["version"]


def test_version_line_is_one_release_please_can_move():
    matches = VERSION_LINE.findall(INIT_FILE.read_text(encoding="utf-8"))
    assert len(matches) == 1, "workspace/__init__.py needs one marked __version__ line"
    assert MOVABLE_VERSION.match(matches[0]), matches[0]
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert MOVABLE_VERSION.match(pyproject["project"]["version"])


def test_release_config_and_manifest():
    config = load_json("release-please-config.json")
    package = config["packages"]["."]
    assert package["release-type"] == "python"
    assert package["include-component-in-tag"] is False
    assert package["include-v-in-tag"] is True
    assert package["initial-version"] == "0.1.0"
    assert package["changelog-path"] == "CHANGELOG.md"
    assert "workspace/__init__.py" in package["extra-files"]
    for path in package["extra-files"]:
        assert (REPO_ROOT / path).is_file(), path
    manifest = load_json(".release-please-manifest.json")
    assert set(manifest) <= {"."}
    for version in manifest.values():
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), version
    # release-please reads the manifest as the last released version, so it
    # must move with the package version or the next release repeats a tag.
    # v0.1.0 is tagged, so an empty manifest is drift too.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest == {".": pyproject["project"]["version"]}, manifest


def test_release_workflow_is_main_only_pinned_and_least_privilege():
    path = WORKFLOWS / "release-please.yml"
    workflow = load_workflow("release-please.yml")
    assert triggers(workflow) == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["release-please"]
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    uses = re.findall(r"uses:\s*(\S+)(.*)", path.read_text(encoding="utf-8"))
    assert len(uses) == 1
    action, comment = uses[0]
    assert re.fullmatch(r"googleapis/release-please-action@[0-9a-f]{40}", action), action
    assert re.fullmatch(r"\s*# v[0-9]+\.[0-9]+\.[0-9]+", comment), comment


def title_step():
    workflow = load_workflow("pr-title.yml")
    assert list(workflow["jobs"]) == ["title"]
    steps = workflow["jobs"]["title"]["steps"]
    assert len(steps) == 1
    return workflow, steps[0]


def test_title_workflow_reads_the_title_only_through_env():
    workflow, step = title_step()
    assert triggers(workflow) == {
        "pull_request": {"types": ["opened", "edited", "synchronize", "reopened", "ready_for_review"]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["title"]
    assert "uses" not in step
    assert step["env"] == {"PR_TITLE": "${{ github.event.pull_request.title }}"}
    assert "${{" not in step["run"]


def run_title_check(title):
    _, step = title_step()
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env={"PATH": os.environ["PATH"], "PR_TITLE": title},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


GOOD_TITLES = [
    "feat: add a verb",
    "fix(scan): keep a worktree with its parent",
    "feat(cli)!: rename a flag",
    "feat!: drop a flag",
    "docs: explain the exit codes",
    "chore(main): release 0.1.0",
    "refactor: split the walker",
    "test: cover the manifest",
    "ci: pin the actions",
    "build: raise the setuptools floor",
    "perf: stop re-reading the registry",
    "style: wrap a long line",
    'revert: "feat: add a verb"',
]

BAD_TITLES = [
    "",
    "update the readme",
    "Feat: capital type",
    "FIX: upper case type",
    "feature: not a listed type",
    "feat:no space after the colon",
    "feat : space before the colon",
    "feat:  ",
    "feat: ",
    "feat:",
    "feat(): empty scope",
    "feat(a b): space in the scope",
    "feat(a)(b): two scopes",
    "feat!!: two bangs",
    "feat(scan)!x: bang not before the colon",
    " feat: leading space",
    "feat: first line\nfix: second line",
    "WIP: not a type",
]


@pytest.mark.parametrize("title", GOOD_TITLES)
def test_title_check_accepts_a_conventional_title(title):
    assert run_title_check(title) == 0


@pytest.mark.parametrize("title", BAD_TITLES)
def test_title_check_refuses_a_near_miss(title):
    assert run_title_check(title) == 1
