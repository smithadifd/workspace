"""`workspace prompt`: the opt-in SessionStart line.

Every case runs the real command, `python -m workspace prompt`, with the
workspace as its working directory, the way the hook runs it, and checks the
contract every run keeps: exit 0, empty stderr, and at most one stdout line
that matches LINE. HOME is the temp dir, so `~/src` is the code root. Repos
are synthetic: a folder holding a `.git` folder with a plain config, which is
all `scan` reads. git is the real one, or a recording fake first on PATH.
"""
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import pytest
import yaml

from test_scan import REPO_ROOT
from workspace import prompt

HOST = "desk"
STATE_REL = os.path.join("state", "prompt-%s.json" % HOST)
REAL_GIT = shutil.which("git")
CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n"

# The one fixed regex every stdout must match: one of the two templates, the
# first with or without its `newest` clause, ended by one newline. It is
# written here, apart from prompt.py, so a change to a template shows.
LINE = re.compile(
    r"\A(?:"
    r"[0-9]+ unregistered repos? \(newest: ([A-Za-z0-9._-]{1,64})\)\. "
    r"Say 'register \1' or 'decline \1'"
    r"|[0-9]+ unregistered repos?\. Run workspace scan to name them, then say "
    r"'register NAME' or 'decline NAME'"
    r"|The workspace cannot list its code root from this session; give this app "
    r"read access to it, then run workspace scan"
    r")\n\Z"
)
NO_NAME = ("2 unregistered repos. Run workspace scan to name them, then say "
           "'register NAME' or 'decline NAME'\n")
TRUST = ("The workspace cannot list its code root from this session; give this app "
         "read access to it, then run workspace scan\n")


def today():
    return datetime.date.today()


class WS:
    """A workspace at base/ws with the code root base/src and an empty state/."""

    def __init__(self, base, opt_in="on"):
        self.base = base
        self.path = base / "ws"
        self.src = base / "src"
        self.path.mkdir()
        self.src.mkdir()
        (self.path / "state").mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.log = base / "git-calls.log"
        self.document = {
            "system": "Fixture",
            "code_roots": {HOST: "~/src"},
            "excluded": [{"path": "~/private", "reason": "personal data"}],
            "projects": [{
                "name": "known",
                "remote": None,
                "paths": {HOST: "~/src/known"},
                "lifecycle": "active",
            }],
        }
        self.write_registry(opt_in)

    def write_registry(self, opt_in="on"):
        text = yaml.safe_dump(self.document, sort_keys=False)
        if opt_in is not None:
            text += "session_prompt: %s\n" % opt_in
        (self.path / "registry.yaml").write_text(text, encoding="utf-8")

    def repo(self, name, age=0):
        """A repo folder in the code root; `age` seconds old, entry and marker alike."""
        folder = self.src / name
        folder.mkdir()
        (folder / ".git").mkdir()
        (folder / ".git" / "config").write_bytes(CONFIG)
        stamp = time.time() - 1000 - age
        for path in (folder / ".git", folder):
            os.utime(path, (stamp, stamp))
        return folder

    @property
    def state(self):
        return self.path / STATE_REL

    def write_state(self, **fields):
        data = {"version": 1, "checked": None, "snooze_until": None}
        data.update(fields)
        self.state.write_text(json.dumps(data), encoding="utf-8")

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def recording_git(self, then="exec %s \"$@\"" % REAL_GIT):
        """A fake git first on PATH that logs each call, then does `then`."""
        fake = self.bin / "git"
        fake.write_text("#!/bin/sh\necho \"$@\" >> '%s'\n%s\n" % (self.log, then))
        fake.chmod(0o755)

    def git_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def run_prompt(ws, *args, path=None, timeout=60):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["HOME"] = str(ws.base)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PATH"] = path if path is not None else "%s:%s" % (ws.bin, os.environ.get("PATH", ""))
    return subprocess.run(
        [sys.executable, "-m", "workspace", "prompt", *args],
        cwd=str(ws.path), env=env, capture_output=True, timeout=timeout, check=False,
    )


def kept_contract(result):
    """Invariant 2: exit 0, empty stderr, nothing or one line that matches LINE."""
    assert result.returncode == 0, result
    assert result.stderr == b"", result.stderr
    out = result.stdout.decode("utf-8")
    assert out == "" or LINE.fullmatch(out), out
    return out


def tree(base):
    """Every entry below `base`: its kind, mode, and bytes or link text. Reads no FIFO."""
    seen = {}
    for folder, names, files in os.walk(base, followlinks=False):
        for name in names + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                with open(path, "rb") as handle:
                    body = handle.read()
            elif stat.S_ISLNK(info.st_mode):
                body = os.readlink(path)
            else:
                body = None
            seen[os.path.relpath(path, base)] = (stat.S_IFMT(info.st_mode),
                                                stat.S_IMODE(info.st_mode), body)
    return seen


# --------------------------------------------------------------------------
# The line, the state, the off state
# --------------------------------------------------------------------------

def test_names_the_newest_unregistered_repo_after_recording_the_day(tmp_path):
    ws = WS(tmp_path)
    ws.repo("known", age=50)
    ws.repo("alpha", age=300)
    ws.repo("beta", age=10)
    ws.repo("gamma", age=200)
    result = run_prompt(ws)
    assert kept_contract(result) == (
        "3 unregistered repos (newest: beta). Say 'register beta' or 'decline beta'\n")
    assert ws.read_state() == {"version": 1, "checked": today().isoformat(),
                               "snooze_until": None}


def test_one_repo_is_singular(tmp_path):
    ws = WS(tmp_path)
    ws.repo("solo")
    out = kept_contract(run_prompt(ws))
    assert out == "1 unregistered repo (newest: solo). Say 'register solo' or 'decline solo'\n"


def test_nothing_unregistered_prints_nothing_and_records_the_day(tmp_path):
    ws = WS(tmp_path)
    ws.repo("known")
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.read_state()["checked"] == today().isoformat()


@pytest.mark.parametrize("opt_in", [None, "off", "'yes please'", "'On'", "1"])
def test_off_prints_nothing_reads_no_state_and_runs_no_scan(tmp_path, opt_in):
    ws = WS(tmp_path, opt_in=opt_in)
    ws.repo("stray")
    ws.recording_git()
    before = tree(tmp_path / "ws")
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.git_calls() == []
    assert tree(tmp_path / "ws") == before


def test_quoted_on_is_on(tmp_path):
    ws = WS(tmp_path, opt_in="'on'")
    ws.repo("stray")
    assert kept_contract(run_prompt(ws)) == (
        "1 unregistered repo (newest: stray). Say 'register stray' or 'decline stray'\n")


def test_declined_and_remote_registered_repos_are_not_counted(tmp_path):
    ws = WS(tmp_path)
    ws.document["declined"] = [{"remote": "~/src/old-*", "date": "2026-01-01",
                                "reason": "scratch"}]
    ws.write_registry()
    ws.repo("old-one", age=1)
    ws.repo("fresh", age=100)
    assert kept_contract(run_prompt(ws)) == (
        "1 unregistered repo (newest: fresh). Say 'register fresh' or 'decline fresh'\n")


def test_without_a_state_folder_it_is_quiet_and_makes_none(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    (ws.path / "state").rmdir()
    assert kept_contract(run_prompt(ws)) == ""
    assert not (ws.path / "state").exists()


@pytest.mark.parametrize("args", [["extra"], ["--snooze"], ["--snooze", "0"],
                                  ["--snooze", "366"], ["--snooze", "1x"],
                                  ["--snooze", "3", "more"]])
def test_other_arguments_are_quiet_and_write_nothing(tmp_path, args):
    ws = WS(tmp_path)
    ws.repo("stray")
    before = tree(tmp_path / "ws")
    assert kept_contract(run_prompt(ws, *args)) == ""
    assert tree(tmp_path / "ws") == before


@pytest.mark.parametrize("args", [["--snooze", "3"], ["--snooze=3"]])
def test_snooze_records_its_end_and_prints_nothing(tmp_path, args):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git()
    assert kept_contract(run_prompt(ws, *args)) == ""
    end = (today() + datetime.timedelta(days=3)).isoformat()
    assert ws.read_state() == {"version": 1, "checked": None, "snooze_until": end}
    assert ws.git_calls() == []


def test_unreadable_code_root_prints_the_trust_line(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.src.chmod(0)
    try:
        assert kept_contract(run_prompt(ws)) == TRUST
    finally:
        ws.src.chmod(0o755)
    assert ws.read_state()["checked"] == today().isoformat()


@pytest.mark.parametrize("host", ["bad host", "x/y", "-dash", "tab\tx", "a" * 65])
def test_host_that_is_not_a_plain_file_name_writes_no_state(tmp_path, host):
    ws = WS(tmp_path)
    ws.document["code_roots"] = {host: "~/src"}
    ws.document["projects"][0]["paths"] = {host: "~/src/known"}
    ws.write_registry()
    ws.repo("stray")
    ws.recording_git()
    assert kept_contract(run_prompt(ws)) == ""
    assert os.listdir(ws.path / "state") == []
    assert ws.git_calls() == []


# --------------------------------------------------------------------------
# Round-1 check 1: a hostile state file
# --------------------------------------------------------------------------

def hostile_symlink(ws):
    sentinel = ws.base / "sentinel"
    sentinel.write_text(json.dumps({"version": 1, "checked": None, "snooze_until": None}))
    sentinel.chmod(0o600)
    os.symlink(sentinel, ws.state)


def hostile_fifo(ws):
    os.mkfifo(ws.state)


def hostile_folder(ws):
    ws.state.mkdir()


def hostile_state_folder_link(ws):
    elsewhere = ws.base / "elsewhere"
    elsewhere.mkdir()
    (ws.path / "state").rmdir()
    os.symlink(elsewhere, ws.path / "state")


@pytest.mark.parametrize("make", [hostile_symlink, hostile_fifo, hostile_folder,
                                  hostile_state_folder_link])
def test_hostile_state_file_is_silent_and_leaves_the_tree(tmp_path, make):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git()
    make(ws)
    before = tree(tmp_path)
    try:
        result = run_prompt(ws, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("prompt blocked on the state file")
    assert kept_contract(result) == ""
    assert tree(tmp_path) == before
    assert ws.git_calls() == []


# --------------------------------------------------------------------------
# Round-1 check 2: broken inputs keep invariant 2
# --------------------------------------------------------------------------

def no_registry(ws):
    (ws.path / "registry.yaml").unlink()


def malformed_registry(ws):
    (ws.path / "registry.yaml").write_text("session_prompt: on\nprojects: [\n")


def registry_outside_grammar(ws):
    ws.document["surprise"] = 1
    ws.write_registry()


def registry_folder(ws):
    (ws.path / "registry.yaml").unlink()
    (ws.path / "registry.yaml").mkdir()


def unreadable_root(ws):
    ws.src.chmod(0)


def missing_root(ws):
    shutil.rmtree(ws.src)


def no_git(ws):
    ws.path_override = str(ws.bin)


def garbage_state(ws):
    ws.state.write_bytes(b"\x00garbage{")


def several_hosts(ws):
    ws.document["code_roots"]["laptop"] = "~/src"
    ws.write_registry()


@pytest.mark.parametrize("make", [no_registry, malformed_registry, registry_outside_grammar,
                                  registry_folder, unreadable_root, missing_root, no_git,
                                  garbage_state, several_hosts])
def test_broken_inputs_keep_the_contract(tmp_path, make):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.path_override = None
    make(ws)
    try:
        result = run_prompt(ws, path=ws.path_override)
    finally:
        if ws.src.exists():
            ws.src.chmod(0o755)
    kept_contract(result)


def test_garbage_state_is_left_as_it_is(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.state.write_bytes(b"{not json")
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.state.read_bytes() == b"{not json"


def test_no_git_is_silent_and_records_nothing(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    assert kept_contract(run_prompt(ws, path=str(ws.bin))) == ""
    assert not ws.state.exists()


# --------------------------------------------------------------------------
# Round-1 check 3: a read-only state/ prints nothing
# --------------------------------------------------------------------------

def test_read_only_state_folder_prints_nothing(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    (ws.path / "state").chmod(0o555)
    try:
        result = run_prompt(ws)
    finally:
        (ws.path / "state").chmod(0o755)
    assert kept_contract(result) == ""
    assert not ws.state.exists()


def test_state_file_it_cannot_replace_prints_nothing(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.write_state(checked="2000-01-01")
    before = ws.state.read_bytes()
    (ws.path / "state").chmod(0o555)
    try:
        result = run_prompt(ws)
    finally:
        (ws.path / "state").chmod(0o755)
    assert kept_contract(result) == ""
    assert ws.state.read_bytes() == before


# --------------------------------------------------------------------------
# Round-1 check 5: hostile names
# --------------------------------------------------------------------------

# 300 characters cannot be one folder name (NAME_MAX is 255 bytes on the
# usual file systems), so the folder takes the longest name it can; the
# 300-character name is judged directly by `prompt.render` below.
HOSTILE_NAMES = ["new\nline", "quo'te", 'dq"x', "esc\x1b[31mred", "a" * 255,
                 "sp ace", "café", "x" * 65]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_newest_name_is_dropped_and_output_matches_line(tmp_path, name):
    ws = WS(tmp_path)
    ws.repo("plain", age=500)
    ws.repo(name, age=1)
    out = kept_contract(run_prompt(ws))
    assert LINE.fullmatch(out), out
    assert out == NO_NAME


@pytest.mark.parametrize("name", ["a" * 300, "new\nline", "quo'te", "esc\x1b", "x" * 65, "",
                                  "café", "tab\t", "slash/x", None])
def test_render_drops_any_name_outside_the_grammar(name):
    out = prompt.render(4, name) + "\n"
    assert LINE.fullmatch(out), out
    assert "newest" not in out


@pytest.mark.parametrize("name", ["a", "a" * 64, "Repo.v2_x-1", "..."])
def test_render_names_a_name_inside_the_grammar(name):
    out = prompt.render(4, name) + "\n"
    assert LINE.fullmatch(out).group(1) == name


# --------------------------------------------------------------------------
# Round-1 check 6: snooze and deadline
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fields", [
    {"snooze_until": "9999-12-31"},
    {"checked": "TODAY"},
])
def test_snoozed_or_checked_today_runs_no_git(tmp_path, fields):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git()
    fields = {key: today().isoformat() if value == "TODAY" else value
              for key, value in fields.items()}
    ws.write_state(**fields)
    before = ws.state.read_bytes()
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.git_calls() == []
    assert ws.state.read_bytes() == before


def test_ended_snooze_and_yesterday_scan_again(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git()
    yesterday = (today() - datetime.timedelta(days=1)).isoformat()
    ws.write_state(checked=yesterday, snooze_until=today().isoformat())
    assert kept_contract(run_prompt(ws)) == (
        "1 unregistered repo (newest: stray). Say 'register stray' or 'decline stray'\n")
    assert ws.git_calls() != []


def test_second_run_the_same_day_is_quiet(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    assert kept_contract(run_prompt(ws)) != ""
    ws.recording_git()
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.git_calls() == []


def test_sleeping_git_is_cut_off_at_the_deadline(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git(then="sleep 60")
    started = time.monotonic()
    try:
        result = run_prompt(ws, timeout=prompt.DEADLINE_SECONDS + 6)
    except subprocess.TimeoutExpired:
        pytest.fail("prompt ran past its deadline")
    elapsed = time.monotonic() - started
    assert kept_contract(result) == ""
    assert ws.git_calls() != []
    assert elapsed < prompt.DEADLINE_SECONDS + 3, elapsed
    assert not ws.state.exists()


# --------------------------------------------------------------------------
# The state grammar: each near miss refused by name
# --------------------------------------------------------------------------

GOOD = {"version": 1, "checked": "2026-01-02", "snooze_until": None}


def with_fields(**fields):
    data = dict(GOOD)
    data.update(fields)
    return json.dumps(data).encode("utf-8")


@pytest.mark.parametrize("data, reason", [
    (b" " * 4097, prompt.STATE_TOO_BIG),
    (b"\xff{}", prompt.STATE_NOT_UTF8),
    (b"{not json", prompt.STATE_NOT_JSON),
    (b'{"version": NaN, "checked": null, "snooze_until": null}', prompt.STATE_NOT_JSON),
    (b'{"version": 1, "version": 1, "checked": null, "snooze_until": null}',
     prompt.STATE_REPEATED_KEY),
    (b"[1]", prompt.STATE_NOT_OBJECT),
    (b"null", prompt.STATE_NOT_OBJECT),
    (b'{"version": 1, "checked": null}', prompt.STATE_KEYS_DIFFER),
    (with_fields(extra=1), prompt.STATE_KEYS_DIFFER),
    (b'{"Version": 1, "checked": null, "snooze_until": null}', prompt.STATE_KEYS_DIFFER),
    (with_fields(version="1"), prompt.STATE_BAD_VERSION),
    (with_fields(version=True), prompt.STATE_BAD_VERSION),
    (with_fields(version=2), prompt.STATE_BAD_VERSION),
    (with_fields(version=1.0), prompt.STATE_BAD_VERSION),
    (with_fields(checked="2026-1-02"), prompt.STATE_BAD_DATE),
    (with_fields(checked="2026-13-02"), prompt.STATE_BAD_DATE),
    (with_fields(checked=" 2026-01-02"), prompt.STATE_BAD_DATE),
    (with_fields(checked="2026-01-02\n"), prompt.STATE_BAD_DATE),
    (with_fields(checked="20260102"), prompt.STATE_BAD_DATE),
    (with_fields(snooze_until=20260102), prompt.STATE_BAD_DATE),
    (with_fields(snooze_until="２026-01-02"), prompt.STATE_BAD_DATE),
])
def test_state_near_misses_are_refused_by_name(data, reason):
    with pytest.raises(prompt.BadState) as refused:
        prompt.parse_state(data)
    assert refused.value.reason == reason


def test_state_in_its_grammar_parses():
    assert prompt.parse_state(with_fields()) == GOOD
    assert prompt.parse_state(prompt.state_bytes(prompt.empty_state())) == prompt.empty_state()


def test_near_miss_state_on_disk_is_silent_and_kept(tmp_path):
    ws = WS(tmp_path)
    ws.repo("stray")
    ws.recording_git()
    data = with_fields(extra=1)
    ws.state.write_bytes(data)
    assert kept_contract(run_prompt(ws)) == ""
    assert ws.state.read_bytes() == data
    assert ws.git_calls() == []


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------

def test_cli_dispatches_prompt(tmp_path, monkeypatch, capsys):
    ws = WS(tmp_path)
    ws.repo("stray")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(ws.path)
    from workspace import cli

    assert cli.main(["prompt"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "1 unregistered repo (newest: stray). Say 'register stray' or 'decline stray'\n")
