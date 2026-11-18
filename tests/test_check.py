"""`workspace check WORKSPACE`: how the workspace folder's inputs are found.

The check's own properties are proved by tests/check-workspace-registry.test.sh;
these tests cover what moving it behind a workspace path added.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIX = REPO_ROOT / "tests" / "fixtures" / "workspace-registry"


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-P", "-m", "workspace", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def make_workspace(tmp_path, agents=None, claude=None, settings=True, registry=True):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    if registry:
        shutil.copyfile(FIX / "conforming" / "registry.yaml", ws / "registry.yaml")
    if settings:
        shutil.copyfile(FIX / "conforming" / "settings.json", ws / ".claude" / "settings.json")
    if agents is not None:
        shutil.copyfile(agents, ws / "AGENTS.md")
    if claude is not None:
        shutil.copyfile(claude, ws / "CLAUDE.md")
    return ws


def test_agents_md_is_the_map_when_both_exist(tmp_path):
    ws = make_workspace(
        tmp_path,
        agents=FIX / "conforming" / "map.md",
        claude=FIX / "active-missing" / "map.md",
    )
    code, out = run_cli("check", str(ws))
    assert code == 0, "AGENTS.md was not read as the map: " + out
    assert "PASS  registry, map, and settings are consistent" in out, out


def test_claude_md_is_the_map_when_agents_md_is_absent(tmp_path):
    ws = make_workspace(tmp_path, claude=FIX / "conforming" / "map.md")
    code, out = run_cli("check", str(ws))
    assert code == 0, "CLAUDE.md fallback was not read as the map: " + out
    assert "PASS  registry, map, and settings are consistent" in out, out


def test_no_map_is_unknown(tmp_path):
    ws = make_workspace(tmp_path)
    code, out = run_cli("check", str(ws))
    assert code == 2, out
    assert "UNKNOWN  map is missing or not a file: %s" % (ws / "AGENTS.md") in out, out


def test_workspace_that_is_not_a_folder_is_unknown(tmp_path):
    target = tmp_path / "not-a-folder"
    target.write_text("x", encoding="utf-8")
    code, out = run_cli("check", str(target))
    assert code == 2, out
    assert "UNKNOWN  workspace is missing or not a folder: %s" % target in out, out


def test_settings_without_sandbox_block_pass(tmp_path):
    # The committed settings carry no sandbox block; denyRead is then not required.
    ws = make_workspace(tmp_path, agents=FIX / "conforming" / "map.md")
    code, out = run_cli("check", str(ws))
    assert code == 0, out
    assert "denyRead" not in out, out


def test_check_usage_errors_exit_3(tmp_path):
    for args in (("check",), ("check", "a", "b"), ("check", "--bogus")):
        code, out = run_cli(*args)
        assert code == 3, (args, out)
        assert "Usage: workspace check WORKSPACE" in out, out
