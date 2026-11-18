"""`workspace scan WORKSPACE`: the drift scan of a code root against the registry.

Every fixture is built in a temp dir with synthetic names. The tree:

    code/                     the code root for host `testhost`
      alpha/                  registered, active (remote acme/alpha)
        lanes/lane-1/         a nested lane repo inside a registered repo
      alpha-wt/               a worktree of alpha (.git file)
      gamma/                  declined by remote
      legacy/                 registered, archived, present on disk
      secret/                 an excluded path (a repo with a private file)
      lane-link -> alpha/lanes/lane-1
      ext-link  -> ../outside/delta-clone      (resolves outside the root)
      sneaky    -> secret                      (in-root link into the excluded path)
      notes.txt, plain/       not repos
    outside/delta-clone/      a repo outside the code root
    ws/registry.yaml          alpha, legacy, delta (missing) [, epsilon (missing, required_on)]
"""
import builtins
import io
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workspace import scan

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST = "testhost"
REMOTE = "https://example.invalid/acme/%s.git"
GIT_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def git(*args):
    subprocess.run(
        [
            "git",
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            *[str(arg) for arg in args],
        ],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )


def make_repo(path, remote=None, commit=False):
    path.mkdir(parents=True)
    git("init", "-q", path)
    if remote:
        git("-C", path, "remote", "add", "origin", remote)
    if commit:
        git("-C", path, "commit", "-q", "--allow-empty", "-m", "fixture")


def project(root, name, lifecycle="active", **extra):
    entry = {
        "name": name,
        "remote": REMOTE % name,
        "paths": {HOST: str(root / name)},
        "subpath": None,
        "lifecycle": lifecycle,
    }
    entry.update(extra)
    return entry


class Tree:
    def __init__(self, base, beta=False, epsilon=False):
        self.base = base
        self.root = base / "code"
        self.outside = base / "outside"
        self.ws = base / "ws"
        root = self.root
        root.mkdir()
        make_repo(root / "alpha", REMOTE % "alpha", commit=True)
        make_repo(root / "alpha" / "lanes" / "lane-1", REMOTE % "lane-1")
        git("-C", root / "alpha", "worktree", "add", "-q", root / "alpha-wt")
        if beta:
            make_repo(root / "beta", REMOTE % "beta", commit=True)
            git("-C", root / "beta", "worktree", "add", "-q", root / "beta-wt")
        make_repo(root / "gamma", REMOTE % "gamma")
        make_repo(root / "legacy", REMOTE % "legacy")
        make_repo(root / "secret", REMOTE % "secret")
        (root / "secret" / "private.txt").write_text("never read\n", encoding="utf-8")
        make_repo(self.outside / "delta-clone", REMOTE % "delta-clone")
        (root / "lane-link").symlink_to(Path("alpha") / "lanes" / "lane-1")
        (root / "ext-link").symlink_to(self.outside / "delta-clone")
        (root / "sneaky").symlink_to(Path("secret"))
        (root / "notes.txt").write_text("not a repo\n", encoding="utf-8")
        (root / "plain").mkdir()

        projects = [
            project(root, "alpha"),
            project(root, "legacy", lifecycle="archived"),
            project(root, "delta"),
        ]
        if epsilon:
            projects.append(project(root, "epsilon", required_on=[HOST]))
        self.registry = {
            "system": "Fixture",
            "harness_root": "$HARNESS_ROOT",
            "code_roots": {HOST: str(root)},
            "excluded": [{"path": str(root / "secret"), "reason": "fixture private data"}],
            "projects": projects,
            "declined": [
                {"remote": REMOTE % "gamma", "date": "2026-11-01", "reason": "fixture declined"}
            ],
        }
        self.ws.mkdir()
        self.write_registry()

    def write_registry(self, text=None):
        if text is None:
            text = yaml.safe_dump(self.registry, sort_keys=False)
        (self.ws / "registry.yaml").write_text(text, encoding="utf-8")


def run_scan(tree, host=HOST):
    buffer = io.StringIO()
    code = scan.main(str(tree.ws), host, out=buffer)
    return code, buffer.getvalue()


class Trace:
    """Records every path the scan hands to a filesystem call or to git."""

    FUNCTIONS = (
        (os, "lstat"), (os, "stat"), (os, "listdir"), (os, "scandir"),
        (os, "readlink"), (os, "open"), (builtins, "open"), (io, "open"),
    )

    def __init__(self):
        self.paths = []

    def record(self, value):
        if isinstance(value, (str, bytes, os.PathLike)):
            self.paths.append(os.path.normpath(os.fsdecode(value)))

    def wrap(self, original):
        def traced(*args, **kwargs):
            if args:
                self.record(args[0])
            return original(*args, **kwargs)
        return traced

    def run(self, monkeypatch, tree, host=HOST):
        original_run = subprocess.run

        def traced_run(args, *rest, **kwargs):
            for flag, value in zip(args, args[1:]):
                if flag in ("-C", "--git-dir"):
                    self.record(value)
            return original_run(args, *rest, **kwargs)

        with monkeypatch.context() as patch:
            for module, name in self.FUNCTIONS:
                patch.setattr(module, name, self.wrap(getattr(module, name)))
            patch.setattr(subprocess, "run", traced_run)
            return run_scan(tree, host)

    def under(self, base):
        base = os.path.normpath(str(base))
        return [path for path in self.paths if path == base or path.startswith(base + os.sep)]


def drift_lines(out):
    return [line for line in out.splitlines() if line.startswith("DRIFT")]


# --------------------------------------------------------------------------
# The fixtures, one behaviour each
# --------------------------------------------------------------------------

def test_conforming_tree_passes_with_every_line_named(tmp_path):
    tree = Tree(tmp_path)
    code, out = run_scan(tree)
    root = tree.root
    assert code == 0, out
    assert drift_lines(out) == [], out
    for line in (
        "host `testhost`, code root %s" % root,
        "SKIP   %s: excluded path, not read" % (root / "secret"),
        "SKIP   %s: symlink excluded path, not read" % (root / "sneaky"),
        "SKIP   %s: symlink resolves outside the code root" % (root / "ext-link"),
        "SKIP   %s: symlink inside repo %s" % (root / "lane-link", root / "alpha"),
        "INFO   worktree %s folds into %s" % (root / "alpha-wt", root / "alpha"),
        "INFO   declined repo at %s (example.invalid/acme/gamma)" % (root / "gamma"),
        "KNOWN  archived project `legacy` is present at %s" % (root / "legacy"),
        "WARN   project `delta` is missing on host `testhost`: %s" % (root / "delta"),
        "PASS  no drift (1 warn)",
    ):
        assert line in out.splitlines(), "missing line %r in:\n%s" % (line, out)


def test_worktree_folds_into_its_parent(tmp_path):
    tree = Tree(tmp_path, beta=True)
    code, out = run_scan(tree)
    root = tree.root
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  unregistered repo at %s (example.invalid/acme/beta)" % (root / "beta")
    ], "the worktree was not folded into its parent:\n" + out
    assert "INFO   worktree %s folds into %s" % (root / "beta-wt", root / "beta") in out, out


def test_nested_lane_repo_is_never_entered(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert "lane-1" not in "\n".join(drift_lines(out)), out
    assert "SKIP   %s: symlink inside repo %s" % (
        tree.root / "lane-link", tree.root / "alpha") in out, out
    touched = trace.under(tree.root / "alpha" / "lanes")
    assert touched == [], "scan touched inside a registered repo: %r" % touched


def test_declined_repo_is_not_drift(tmp_path):
    tree = Tree(tmp_path)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "INFO   declined repo at %s (example.invalid/acme/gamma)" % (tree.root / "gamma") in out, out
    assert "gamma" not in "\n".join(drift_lines(out)), out


def test_archived_repo_on_disk_is_known(tmp_path):
    tree = Tree(tmp_path)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "KNOWN  archived project `legacy` is present at %s" % (tree.root / "legacy") in out, out


def test_missing_host_path_is_a_warning(tmp_path):
    tree = Tree(tmp_path)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "WARN   project `delta` is missing on host `testhost`: %s" % (tree.root / "delta") in out, out


def test_missing_host_path_named_by_required_on_is_drift(tmp_path):
    tree = Tree(tmp_path, epsilon=True)
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  project `epsilon` is missing on host `testhost`, which its required_on names: %s"
        % (tree.root / "epsilon")
    ], out


def test_out_of_root_symlink_is_skipped_and_never_followed(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert "SKIP   %s: symlink resolves outside the code root" % (tree.root / "ext-link") in out, out
    touched = trace.under(tree.outside)
    assert touched == [], "scan touched outside the code root: %r" % touched
    assert "delta-clone" not in "\n".join(drift_lines(out)), out


def test_excluded_path_is_never_touched(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    touched = trace.under(tree.root / "secret")
    assert touched == [], "scan touched the excluded path: %r" % touched
    assert "secret" not in "\n".join(drift_lines(out)), out


def test_excluded_path_whose_read_would_fail_is_not_read(tmp_path):
    # Out of process, no tracing: the excluded folder refuses every access, so
    # any look inside it would end the scan with UNKNOWN.
    tree = Tree(tmp_path)
    secret = tree.root / "secret"
    secret.chmod(0)
    try:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        result = subprocess.run(
            [sys.executable, "-P", "-m", "workspace", "scan", str(tree.ws), "--host", HOST],
            capture_output=True, text=True, env=env, check=False,
        )
    finally:
        secret.chmod(stat.S_IRWXU)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  no drift (1 warn)" in result.stdout, result.stdout


def snapshot(base):
    entries = {}
    for folder, dirs, files in os.walk(base):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            entries[path] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return entries


def test_scan_writes_nothing(tmp_path):
    tree = Tree(tmp_path, beta=True)
    before = snapshot(tmp_path)
    code, out = run_scan(tree)
    assert code == 1, out
    assert snapshot(tmp_path) == before


# --------------------------------------------------------------------------
# Could not verify: exit 2, the reason printed, never PASS or DRIFT
# --------------------------------------------------------------------------

def assert_unknown(code, out, reason):
    assert code == 2, out
    assert "UNKNOWN  " + reason in out, "expected reason %r in:\n%s" % (reason, out)
    assert "PASS" not in out and "DRIFT" not in out, out


def test_missing_registry_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    (tree.ws / "registry.yaml").unlink()
    assert_unknown(*run_scan(tree), "registry is missing or not a file")


def test_unparseable_registry_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.write_registry("projects: [unclosed\n")
    assert_unknown(*run_scan(tree), "registry is not parseable YAML")


def test_duplicate_key_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.write_registry("system: A\nsystem: B\n")
    assert_unknown(*run_scan(tree), "registry repeats the key 'system'")


def test_unknown_top_level_field_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["colour"] = "blue"
    tree.write_registry()
    assert_unknown(*run_scan(tree), "registry has unknown field(s): colour")


def test_unknown_project_field_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"][0]["owner_email"] = "someone@example.invalid"
    tree.write_registry()
    assert_unknown(*run_scan(tree), "project entry 1 has unknown field(s): owner_email")


def test_unknown_lifecycle_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"][0]["lifecycle"] = "bogon"
    tree.write_registry()
    assert_unknown(*run_scan(tree), "project `alpha` has lifecycle `bogon`")


def test_excluded_entry_without_reason_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    del tree.registry["excluded"][0]["reason"]
    tree.write_registry()
    assert_unknown(*run_scan(tree), "excluded entry 1 `reason` must be a non-empty string")


def test_relative_excluded_path_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["excluded"].append({"path": "secret", "reason": "relative"})
    tree.write_registry()
    assert_unknown(*run_scan(tree), "excluded path `secret` is not an absolute path")


def test_host_not_in_code_roots_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    assert_unknown(*run_scan(tree, host="elsewhere"),
                   "host `elsewhere` is not in code_roots (known: testhost); pass --host")


def test_missing_code_root_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    missing = tmp_path / "nowhere"
    tree.registry["code_roots"][HOST] = str(missing)
    tree.registry["projects"] = []
    tree.write_registry()
    assert_unknown(*run_scan(tree), "code root %s cannot be read" % missing)


def test_excluded_code_root_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["excluded"].append({"path": str(tmp_path), "reason": "everything"})
    tree.write_registry()
    assert_unknown(*run_scan(tree), "code root %s is inside an excluded path" % tree.root)


def test_project_path_outside_code_root_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"][0]["paths"][HOST] = str(tree.outside / "delta-clone")
    tree.write_registry()
    assert_unknown(*run_scan(tree), "project `alpha` path %s is outside the code root"
                   % (tree.outside / "delta-clone"))


def test_git_error_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    broken = tree.root / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: %s\n" % (tree.root / "nowhere"), encoding="utf-8")
    assert_unknown(*run_scan(tree), "git error on %s" % broken)


def test_unexpected_crash_is_unknown_not_drift(tmp_path, monkeypatch):
    tree = Tree(tmp_path)

    def broken_listdir(*args, **kwargs):
        raise RuntimeError("listing exploded")

    monkeypatch.setattr(os, "listdir", broken_listdir)
    assert_unknown(*run_scan(tree), "scan failed: RuntimeError: listing exploded")


# --------------------------------------------------------------------------
# Inputs: the host name, the home folder, the command line
# --------------------------------------------------------------------------

def test_default_host_is_the_short_host_name(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost.example.invalid")
    code, out = run_scan(tree, host=None)
    assert code == 0, out
    assert "host `testhost`" in out, out


def test_tilde_paths_expand_against_the_home_folder(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    tree.registry["code_roots"][HOST] = "~/code"
    tree.registry["excluded"] = [{"path": "~/code/secret/**", "reason": "fixture private data"}]
    for entry in tree.registry["projects"]:
        entry["paths"][HOST] = "~/code/" + entry["name"]
    tree.write_registry()
    code, out = run_scan(tree)
    assert code == 0, out
    assert "host `testhost`, code root %s" % tree.root in out, out
    assert "SKIP   %s: excluded path, not read" % (tree.root / "secret") in out, out


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-P", "-m", "workspace", *args],
        capture_output=True, text=True, env=env, check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_cli_scan_exit_codes(tmp_path):
    tree = Tree(tmp_path, beta=True)
    code, out = run_cli("scan", str(tree.ws), "--host", HOST)
    assert code == 1, out
    code, out = run_cli("scan", "--host=" + HOST, str(tmp_path / "no-workspace"))
    assert code == 2, out
    assert "UNKNOWN  workspace is missing or not a folder" in out, out


def test_cli_usage_errors_exit_3():
    for args, message in (
        (("scan",), "scan needs a WORKSPACE"),
        (("scan", "ws", "--host"), "--host needs a value"),
        (("scan", "--bogus"), "unknown option --bogus"),
        (("scan", "ws", "--bogus"), "unknown option --bogus"),
        (("scan", "a", "b"), "scan takes one WORKSPACE"),
        (("scan", "ws", "--host", "a", "--host", "b"), "--host given twice"),
        (("scan", "ws", "--host=a", "--host=b"), "--host given twice"),
        (("scan", "ws", "--host="), "--host needs a value"),
        (("frobnicate",), "unknown verb frobnicate"),
    ):
        code, out = run_cli(*args)
        assert code == 3, (args, out)
        assert "workspace: " + message in out, (args, out)
        assert "Usage: workspace check WORKSPACE" in out, out


# --------------------------------------------------------------------------
# The registry grammar: every refusal is named
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda r: r.update(projects=["alpha"]), "project entry 1 is not a mapping"),
        (lambda r: r.pop("projects"), "registry `projects` is missing or not a list"),
        (lambda r: r.update(harness_root=5), "registry `harness_root` must be a string or null"),
        (lambda r: r.update(code_roots={}),
         "registry `code_roots` is missing or not a non-empty mapping"),
        (lambda r: r["projects"][0].update(paths=["x"]),
         "project `alpha` `paths` must be a mapping of host to path"),
        (lambda r: r["projects"][0].update(required_on="testhost"),
         "project `alpha` `required_on` must be a list of host names"),
    ],
    ids=["entry-not-mapping", "projects-missing", "harness-root-type", "code-roots-empty",
         "paths-type", "required-on-type"],
)
def test_registry_grammar_refusals(tmp_path, change, reason):
    tree = Tree(tmp_path)
    change(tree.registry)
    tree.write_registry()
    assert_unknown(*run_scan(tree), reason)


@pytest.mark.parametrize(
    "text, reason",
    [
        ("- a\n", "registry root is not a mapping"),
        ("  \n", "registry is empty"),
        ("? [a, b]\n: x\n", "registry has an unhashable mapping key"),
    ],
    ids=["root-not-mapping", "empty", "unhashable-key"],
)
def test_registry_text_refusals(tmp_path, text, reason):
    tree = Tree(tmp_path)
    tree.write_registry(text)
    assert_unknown(*run_scan(tree), reason)


# --------------------------------------------------------------------------
# Paths the scan refuses to judge: exit 2, never a silent skip
# --------------------------------------------------------------------------

def test_project_path_under_excluded_path_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"][0]["paths"][HOST] = str(tree.root / "secret" / "alpha")
    tree.write_registry()
    assert_unknown(*run_scan(tree), "project `alpha` path %s is excluded"
                   % (tree.root / "secret" / "alpha"))


def test_code_root_that_is_a_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["code_roots"][HOST] = str(tree.root / "notes.txt")
    tree.registry["projects"] = []
    tree.write_registry()
    assert_unknown(*run_scan(tree), "code root %s is not a folder" % (tree.root / "notes.txt"))


def test_code_root_that_cannot_be_listed_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.root.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        tree.root.chmod(stat.S_IRWXU)
    assert_unknown(code, out, "code root %s cannot be listed" % tree.root)


def test_registered_path_that_links_outside_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"].append(project(tree.root, "ext-link"))
    tree.write_registry()
    assert_unknown(*run_scan(tree), "registered path %s: resolves outside the code root"
                   % (tree.root / "ext-link"))


def test_git_marker_that_is_a_symlink_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    odd = tree.root / "odd"
    odd.mkdir()
    (odd / ".git").symlink_to(Path("..") / "alpha" / ".git")
    assert_unknown(*run_scan(tree), "%s is neither a folder nor a file" % (odd / ".git"))


def test_git_file_without_gitdir_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    odd = tree.root / "odd"
    odd.mkdir()
    (odd / ".git").write_text("nonsense\n", encoding="utf-8")
    assert_unknown(*run_scan(tree), "%s does not name a gitdir" % (odd / ".git"))


def test_worktree_with_git_dir_in_excluded_path_is_unknown_and_not_read(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    odd = tree.root / "odd"
    odd.mkdir()
    gitdir = tree.root / "secret" / ".git"
    (odd / ".git").write_text("gitdir: %s\n" % gitdir, encoding="utf-8")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which is excluded; not read"
                   % (odd, gitdir))
    touched = trace.under(tree.root / "secret")
    assert touched == [], "scan touched the excluded path: %r" % touched


def test_worktree_with_git_dir_outside_root_is_unknown_and_not_read(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    odd = tree.root / "odd"
    odd.mkdir()
    gitdir = tree.outside / "delta-clone" / ".git"
    (odd / ".git").write_text("gitdir: %s\n" % gitdir, encoding="utf-8")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out,
                   "worktree %s keeps its git dir at %s, which is outside the code root; not read"
                   % (odd, gitdir))
    touched = trace.under(tree.outside)
    assert touched == [], "scan touched outside the code root: %r" % touched


# --------------------------------------------------------------------------
# Identity: remotes in other forms, declined globs
# --------------------------------------------------------------------------

def test_registered_remote_matches_in_scp_form(tmp_path):
    tree = Tree(tmp_path)
    alpha = tree.registry["projects"][0]
    alpha["remote"] = "git@example.invalid:acme/alpha.git"
    alpha["paths"] = {"otherhost": "/elsewhere/alpha"}
    tree.write_registry()
    code, out = run_scan(tree)
    assert code == 0, out
    assert drift_lines(out) == [], out
    # alpha is a repo on disk though not a registered path here: the link into
    # its nested lane repo is still not followed.
    assert "SKIP   %s: symlink inside repo %s" % (
        tree.root / "lane-link", tree.root / "alpha") in out, out


def test_declined_remote_glob_matches(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["declined"][0]["remote"] = "https://example.invalid/acme/gam*"
    tree.write_registry()
    code, out = run_scan(tree)
    assert code == 0, out
    assert "INFO   declined repo at %s (example.invalid/acme/gamma)" % (tree.root / "gamma") in out, out


def test_declined_path_glob_matches(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["declined"][0]["remote"] = str(tree.root / "gam*")
    tree.write_registry()
    code, out = run_scan(tree)
    assert code == 0, out
    assert "INFO   declined repo at %s (example.invalid/acme/gamma)" % (tree.root / "gamma") in out, out


# --------------------------------------------------------------------------
# I/O failures: exit 2 with the reason, never a pass
# --------------------------------------------------------------------------

def test_missing_git_is_unknown(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert_unknown(*run_scan(tree), "git is not available")


def test_unreadable_worktree_git_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    odd = tree.root / "odd"
    odd.mkdir()
    marker = odd / ".git"
    marker.write_text("gitdir: %s\n" % (tree.root / "alpha" / ".git"), encoding="utf-8")
    marker.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert_unknown(code, out, "could not read %s" % marker)


def test_unreadable_registry_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    registry = tree.ws / "registry.yaml"
    registry.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        registry.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert_unknown(code, out, "could not read registry")


def test_folder_that_cannot_be_inspected_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    plain = tree.root / "plain"
    plain.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        plain.chmod(stat.S_IRWXU)
    assert_unknown(code, out, "could not inspect %s" % (plain / ".git"))
