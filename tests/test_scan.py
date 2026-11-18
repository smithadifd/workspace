"""`workspace scan WORKSPACE`: the drift scan of a code root against the registry.

Every fixture is built in a temp dir with synthetic names. The tree:

    code/                     the code root for host `testhost`
      alpha/                  registered, active (remote acme/alpha)
        lanes/lane-1/         a nested lane repo inside a registered repo
      alpha-wt/               a worktree of alpha (.git file)
      gamma/                  declined by remote
      legacy/                 registered, archived, present on disk
      secret/                 an excluded path (a repo with a private file)
      notes.txt, plain/       not repos
    outside/delta-clone/      a repo outside the code root
    ws/registry.yaml          alpha, legacy, delta (missing) [, epsilon (missing, required_on)]

A symlink in the code root is a could-not-verify, so the tree holds none unless
a test asks for one by name (`links`):

      lane-link -> alpha/lanes/lane-1
      ext-link  -> ../outside/delta-clone      (resolves outside the root)
      sneaky    -> secret                      (in-root link into the excluded path)
"""
import builtins
import errno
import io
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import types
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


LINKS = {
    "lane-link": lambda tree: Path("alpha") / "lanes" / "lane-1",
    "ext-link": lambda tree: tree.outside / "delta-clone",
    "sneaky": lambda tree: Path("secret"),
}


class Tree:
    def __init__(self, base, beta=False, epsilon=False, links=()):
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
        for name in links:
            (root / name).symlink_to(LINKS[name](self))
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


def node(path, follow=True):
    """The (st_dev, st_ino) identity of `path`, or None when it cannot be reached."""
    try:
        info = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


def reaches(path, base, itself):
    """Whether a call on `path` reaches `base`, or with `itself` false a node beneath it.

    By text, and by node identity: `path`'s folders are resolved as the kernel
    resolves them (symlinks followed, any letter case the disk accepts), and
    any of them that is `base` puts `path` beneath it. The last component is
    not followed, as the scan's lstat and no-follow opens do not follow it.
    """
    if path.startswith(base + os.sep) or (itself and path == base):
        return True
    target = node(base, follow=False)
    if target is None:
        return False
    folder = os.path.realpath(os.path.dirname(path))
    if itself and node(os.path.join(folder, os.path.basename(path)), follow=False) == target:
        return True
    return any(node(above, follow=False) == target for above in scan.ancestors(folder))


class Trace:
    """Records every path the scan hands to a filesystem call, and every git run."""

    FUNCTIONS = (
        (os, "lstat"), (os, "stat"), (os, "listdir"), (os, "scandir"),
        (os, "readlink"), (os, "open"), (builtins, "open"), (io, "open"),
    )
    OPENERS = frozenset(("open",))

    def __init__(self):
        self.calls = []
        self.git_calls = []
        self.git_runs = []

    @property
    def paths(self):
        return [path for _, path in self.calls]

    def record(self, name, value):
        if isinstance(value, (str, bytes, os.PathLike)):
            self.calls.append((name, os.path.normpath(os.fsdecode(value))))

    def wrap(self, name, original):
        def traced(*args, **kwargs):
            if args:
                self.record(name, args[0])
            return original(*args, **kwargs)
        return traced

    def run(self, monkeypatch, tree, host=HOST):
        original_run = subprocess.run

        def traced_run(args, *rest, **kwargs):
            self.git_calls.append([os.fsdecode(arg) for arg in args])
            self.git_runs.append({
                "args": [os.fsdecode(arg) for arg in args],
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
                "input": kwargs.get("input"),
            })
            for flag, value in zip(args, args[1:]):
                if flag in ("-C", "--git-dir"):
                    self.record("git", value)
            return original_run(args, *rest, **kwargs)

        with monkeypatch.context() as patch:
            for module, name in self.FUNCTIONS:
                patch.setattr(module, name, self.wrap(name, getattr(module, name)))
            patch.setattr(subprocess, "run", traced_run)
            return run_scan(tree, host)

    def under(self, base):
        """The paths that are `base` or beneath it, by text or by node identity."""
        base = os.path.normpath(str(base))
        return [path for path in self.paths if reaches(path, base, itself=True)]

    def beneath(self, base):
        """The paths strictly beneath `base`, by text or by node identity.

        scan lstats each excluded entry itself to learn its identity, so for an
        excluded path the promise is that nothing beneath it is touched.
        """
        base = os.path.normpath(str(base))
        return [path for path in self.paths if reaches(path, base, itself=False)]

    def opened(self, base):
        """The paths handed to an open call that are `base` or beneath it."""
        base = os.path.normpath(str(base))
        return [path for name, path in self.calls
                if name in self.OPENERS and reaches(path, base, itself=True)]

    def git_under(self, base):
        """The git calls that name `base`, or a path beneath it, as an argument."""
        base = os.path.normpath(str(base))
        return [
            call for call in self.git_calls
            if any(arg == base or arg.startswith(base + os.sep) for arg in call)
        ]


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
    # A symlink in the code root into the nested repo is a could-not-verify
    # (declared change: it was a SKIP line), and is never followed.
    tree = Tree(tmp_path, links=("lane-link",))
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, scan.ROOT_SYMLINK % (tree.root / "lane-link"))
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


def test_out_of_root_symlink_is_unknown_and_never_followed(tmp_path, monkeypatch):
    # Declared change: a symlink in the code root was a SKIP line; it is now exit 2.
    tree = Tree(tmp_path, links=("ext-link",))
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, scan.ROOT_SYMLINK % (tree.root / "ext-link"))
    touched = trace.under(tree.outside)
    assert touched == [], "scan touched outside the code root: %r" % touched


def test_nothing_beneath_an_excluded_path_is_touched(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    touched = trace.beneath(tree.root / "secret")
    assert touched == [], "scan touched beneath the excluded path: %r" % touched
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
    make_repo(broken)
    config = broken / ".git" / "config"
    with open(config, "a", encoding="utf-8") as handle:
        handle.write("garbage line here\n")
    assert_unknown(*run_scan(tree), "git error on %s (%s): " % (broken, config))


def test_git_dir_that_does_not_exist_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    broken = tree.root / "broken"
    broken.mkdir()
    nowhere = tree.root / "nowhere"
    (broken / ".git").write_text("gitdir: %s\n" % nowhere, encoding="utf-8")
    assert_unknown(*run_scan(tree), "worktree %s keeps its git dir at %s, which does not exist "
                   "(no %s); not read" % (broken, nowhere, nowhere))


def test_unexpected_crash_is_unknown_not_drift(tmp_path, monkeypatch):
    tree = Tree(tmp_path)

    def broken_listdir(*args, **kwargs):
        raise RuntimeError("listing exploded")

    monkeypatch.setattr(os, "listdir", broken_listdir)
    assert_unknown(*run_scan(tree), "scan failed: RuntimeError: listing exploded")


# --------------------------------------------------------------------------
# Inputs: the host name, the home folder, the command line
# --------------------------------------------------------------------------

def test_default_host_is_the_only_code_roots_host(tmp_path, monkeypatch):
    # The machine's name is not a code_roots key: a scan that guessed from it
    # would stop at "host ... is not in code_roots".
    tree = Tree(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "elsewhere.example.invalid")
    code, out = run_scan(tree, host=None)
    assert code == 0, out
    assert "host `testhost`, code root %s" % tree.root in out, out


def test_no_host_with_several_code_roots_is_unknown(tmp_path, monkeypatch):
    # The machine's name matches the first key: a scan that guessed from it, or
    # took the first key, would scan and pass.
    tree = Tree(tmp_path)
    tree.registry["code_roots"]["otherhost"] = str(tmp_path / "other-code")
    tree.write_registry()
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost.example.invalid")
    assert_unknown(*run_scan(tree, host=None),
                   "no --host given and code_roots names more than one host "
                   "(otherhost, testhost); pass --host")


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


def run_cli(*args, timeout=None):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-P", "-m", "workspace", *args],
        capture_output=True, text=True, env=env, check=False, timeout=timeout,
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


def test_registered_path_that_links_outside_is_unknown(tmp_path, monkeypatch):
    # Declared change: the link was followed and refused as outside; now the
    # symlink hop itself is refused, and nothing outside is touched.
    tree = Tree(tmp_path)
    link = tree.root / "plain" / "ext-link"
    link.symlink_to(tree.outside / "delta-clone")
    tree.registry["projects"].append(project(tree.root, "ext-link", paths={HOST: str(link)}))
    tree.write_registry()
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, scan.REGISTERED_SYMLINK % (link, link))
    assert trace.under(tree.outside) == [], trace.under(tree.outside)


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
    touched = trace.beneath(tree.root / "secret")
    assert touched == [], "scan touched beneath the excluded path: %r" % touched


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


# --------------------------------------------------------------------------
# required_on: a project this host must hold
# --------------------------------------------------------------------------

def test_required_project_without_a_path_for_this_host_is_drift(tmp_path):
    tree = Tree(tmp_path)
    tree.registry["projects"].append(
        project(tree.root, "zeta", required_on=[HOST], paths={"otherhost": "/elsewhere/zeta"})
    )
    tree.write_registry()
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  project `zeta` is required on host `testhost` but has no path for it"
    ], out


# --------------------------------------------------------------------------
# Nesting: a registered path inside another registered repo is never entered
# --------------------------------------------------------------------------

def test_registered_path_inside_a_registered_repo_is_unknown_and_not_entered(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    alpha = tree.root / "alpha"
    lane = alpha / "lanes" / "lane-1"
    tree.registry["projects"].append(project(tree.root, "lane-1", paths={HOST: str(lane)}))
    tree.write_registry()
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "registered path %s: inside repo %s" % (lane, alpha))
    touched = trace.under(alpha / "lanes")
    assert touched == [], "scan touched inside a registered repo: %r" % touched


# --------------------------------------------------------------------------
# Git's own configuration: none of it can hide drift
# --------------------------------------------------------------------------

def stray_tree(tmp_path):
    """The conforming tree plus `stray`, an unregistered repo with no remote."""
    tree = Tree(tmp_path)
    make_repo(tree.root / "stray")
    return tree


def assert_stray_is_drift(tree):
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  unregistered repo at %s (no remote)" % (tree.root / "stray")
    ], "git config outside the repo gave stray a remote:\n" + out


def gitconfig_naming_alpha(path):
    path.write_text('[remote "origin"]\n\turl = %s\n' % (REMOTE % "alpha"), encoding="utf-8")


def test_caller_git_environment_cannot_hide_drift(tmp_path, monkeypatch):
    tree = stray_tree(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", REMOTE % "alpha")
    assert_stray_is_drift(tree)


def test_global_git_config_cannot_hide_drift(tmp_path, monkeypatch):
    tree = stray_tree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    gitconfig_naming_alpha(home / ".gitconfig")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    assert_stray_is_drift(tree)


def git_shim(tmp_path, monkeypatch, body):
    """Put a `git` first on PATH that runs `body` (shell) before the real git."""
    real = shutil.which("git")
    assert real, "git is not on PATH"
    folder = tmp_path / "shim-bin"
    folder.mkdir()
    shim = folder / "git"
    shim.write_text("#!/bin/sh\n%s\nexec %s \"$@\"\n" % (body, shlex.quote(real)),
                    encoding="utf-8")
    shim.chmod(stat.S_IRWXU)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))


def test_system_git_config_cannot_hide_drift(tmp_path, monkeypatch):
    # The shim stands in for a machine whose system git config sets a remote.
    tree = stray_tree(tmp_path)
    system = tmp_path / "system-gitconfig"
    gitconfig_naming_alpha(system)
    git_shim(tmp_path, monkeypatch,
             "GIT_CONFIG_SYSTEM=%s; export GIT_CONFIG_SYSTEM" % shlex.quote(str(system)))
    assert_stray_is_drift(tree)


# --------------------------------------------------------------------------
# Exclusions: every path scan or git would read is judged before it is touched
# --------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", ["/*", "/**", "/", ""], ids=["star", "star-star", "slash", "bare"])
def test_excluded_glob_forms_cover_the_folder_and_everything_beneath(tmp_path, monkeypatch, suffix):
    tree = Tree(tmp_path)
    secret = tree.root / "secret"
    tree.registry["excluded"] = [{"path": str(secret) + suffix, "reason": "fixture private data"}]
    tree.write_registry()
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert "SKIP   %s: excluded path, not read" % secret in out, out
    touched = trace.beneath(secret)
    assert touched == [], "scan touched beneath the excluded path: %r" % touched
    assert "secret" not in "\n".join(drift_lines(out)), out


def odd_repo(tree, remote=None):
    odd = tree.root / "odd"
    make_repo(odd, remote)
    return odd


def exclude(tree, path):
    tree.registry["excluded"].append({"path": str(path), "reason": "fixture"})
    tree.write_registry()


def test_excluded_git_marker_is_unknown_and_not_touched(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    odd = odd_repo(tree)
    marker = odd / ".git"
    exclude(tree, marker)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "git marker %s, which is excluded; not read" % marker)
    assert trace.beneath(marker) == [], trace.beneath(marker)
    assert trace.opened(marker) == [], trace.opened(marker)
    assert trace.git_under(odd) == [], trace.git_under(odd)


def test_excluded_git_config_is_unknown_and_git_never_runs_on_it(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    odd = odd_repo(tree, "https://example.invalid/acme/sentinel.git")
    config = odd / ".git" / "config"
    exclude(tree, config)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "repo %s keeps its git config at %s, which is excluded; not read"
                   % (odd, config))
    assert "sentinel" not in out, out
    assert trace.opened(config) == [], trace.opened(config)
    assert trace.git_under(odd) == [], trace.git_under(odd)


def with_include(config, header, included, bom=False):
    """Add an include section naming `included` to a repo's config."""
    data = config.read_bytes()
    section = ("%s\n\tpath = %s\n" % (header, included)).encode("utf-8")
    config.write_bytes(b"\xef\xbb\xbf" + section + data if bom else data + section)


@pytest.mark.parametrize(
    "header, bom",
    [
        ("[include]", False),
        ('[includeIf "gitdir:/"]', False),
        ("[core][include]", False),
        ("[include]", True),
    ],
    ids=["include", "includeIf", "adjacent-header", "bom"],
)
def test_git_config_include_is_unknown_and_never_followed(tmp_path, monkeypatch, header, bom):
    # The included file, under the excluded folder, would give the
    # unregistered repo alpha's remote and hide it.
    tree = stray_tree(tmp_path)
    stray = tree.root / "stray"
    config = stray / ".git" / "config"
    included = tree.root / "secret" / "included.gitconfig"
    gitconfig_naming_alpha(included)
    with_include(config, header, included, bom=bom)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "%s: git config includes are not followed (%s has one); not read"
                   % (stray, config))
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")
    assert trace.git_under(tree.root) == [], "git was handed a path: %r" % trace.git_under(tree.root)


def alpha_wt_gitdir(tree):
    return tree.root / "alpha" / ".git" / "worktrees" / "alpha-wt"


def test_excluded_commondir_file_is_unknown_and_not_read(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    worktree = tree.root / "alpha-wt"
    pointer = alpha_wt_gitdir(tree) / "commondir"
    exclude(tree, pointer)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its commondir file at %s, which is excluded; not read"
                   % (worktree, pointer))
    assert trace.opened(pointer) == [], trace.opened(pointer)
    assert trace.git_under(worktree) == [], trace.git_under(worktree)


def test_worktree_config_file_is_never_touched(tmp_path, monkeypatch):
    # scan never reads config.worktree: no file call reaches it.
    tree = Tree(tmp_path)
    worktree = tree.root / "alpha-wt"
    config = alpha_wt_gitdir(tree) / "config.worktree"
    gitconfig_naming_alpha(config)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert "INFO   worktree %s folds into %s" % (worktree, tree.root / "alpha") in out, out
    assert trace.under(config) == [], trace.under(config)


def synthetic_worktree(tree, common):
    """A worktree `odd` whose git dir is alpha/.git/worktrees/odd and names `common`.

    The git dir is under a known repo's worktrees folder, so the common dir
    walk is reached.
    """
    gitdir = tree.root / "alpha" / ".git" / "worktrees" / "odd"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (gitdir / "commondir").write_text("%s\n" % common, encoding="utf-8")
    odd = tree.root / "odd"
    odd.mkdir()
    (odd / ".git").write_text("gitdir: %s\n" % gitdir, encoding="utf-8")
    return odd


@pytest.mark.parametrize("where", ["outside", "excluded"])
def test_worktree_common_dir_is_judged_before_git_runs(tmp_path, monkeypatch, where):
    tree = Tree(tmp_path)
    if where == "outside":
        base, common, words = tree.outside, tree.outside / "delta-clone" / ".git", "is outside the code root"
    else:
        base, common, words = tree.root / "secret", tree.root / "secret" / ".git", "is excluded"
    odd = synthetic_worktree(tree, common)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s has its common dir at %s, which %s; not read"
                   % (odd, common, words))
    touched = trace.beneath(base) if where == "excluded" else trace.under(base)
    assert touched == [], touched
    assert trace.git_under(odd) == [], "git ran on the worktree: %r" % trace.git_under(odd)


def test_git_dir_through_a_symlink_is_unknown_and_not_followed(tmp_path, monkeypatch):
    # By its text the git dir is in the code root; the link takes it into the
    # excluded folder.
    tree = Tree(tmp_path)
    link = tree.root / "plain" / "odd-meta"
    link.symlink_to(Path("..") / "secret" / ".git")
    odd = tree.root / "odd"
    odd.mkdir()
    (odd / ".git").write_text("gitdir: %s\n" % link, encoding="utf-8")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which passes through the "
                   "symlink %s; not followed" % (odd, link, link))
    assert trace.under(link / "commondir") == [], trace.paths
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")
    assert trace.git_under(odd) == [], "git ran on the worktree: %r" % trace.git_under(odd)


def test_unreadable_commondir_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    pointer = alpha_wt_gitdir(tree) / "commondir"
    pointer.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        pointer.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert_unknown(code, out, "could not read %s" % pointer)


def test_git_dir_hop_that_cannot_be_inspected_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    worktrees = tree.root / "alpha" / ".git" / "worktrees"
    worktrees.chmod(0)
    try:
        code, out = run_scan(tree)
    finally:
        worktrees.chmod(stat.S_IRWXU)
    assert_unknown(code, out, "could not inspect %s" % (worktrees / "alpha-wt"))


def test_empty_commondir_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    pointer = alpha_wt_gitdir(tree) / "commondir"
    pointer.write_text("\n", encoding="utf-8")
    assert_unknown(*run_scan(tree), "%s names no common dir" % pointer)


# --------------------------------------------------------------------------
# The walk: a git file's path text is followed hop by hop, never normalised
# --------------------------------------------------------------------------

def worktree_file(tree, text, name="odd"):
    """A folder `name` whose .git file names the git dir `text`, as written."""
    folder = tree.root / name
    folder.mkdir()
    (folder / ".git").write_text("gitdir: %s\n" % text, encoding="utf-8")
    return folder


def secret_worktree_meta(tree):
    """A worktree git dir under the excluded repo: secret/.git/worktrees/odd."""
    meta = tree.root / "secret" / ".git" / "worktrees" / "odd"
    meta.mkdir(parents=True)
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (meta / "commondir").write_text("../..\n", encoding="utf-8")
    return meta


def test_git_dir_symlink_hop_then_parent_step_is_unknown_and_not_followed(tmp_path, monkeypatch):
    # By its text, plain/lnk/../worktrees/odd is plain/worktrees/odd, in the
    # code root. The kernel follows lnk first, so `..` lands in the excluded repo.
    tree = Tree(tmp_path)
    secret_worktree_meta(tree)
    link = tree.root / "plain" / "lnk"
    link.symlink_to(Path("..") / "secret" / ".git" / "worktrees")
    text = "%s/../worktrees/odd" % link
    odd = worktree_file(tree, text)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which passes through the "
                   "symlink %s; not followed" % (odd, text, link))
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_git_dir_missing_hop_then_parent_step_is_unknown(tmp_path, monkeypatch):
    # By its text, plain/ghost/../.. is the code root; the kernel cannot walk
    # through ghost, so git would not find this git dir either.
    tree = Tree(tmp_path)
    ghost = tree.root / "plain" / "ghost"
    text = "%s/../../alpha/.git/worktrees/alpha-wt" % ghost
    odd = worktree_file(tree, text)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which does not exist "
                   "(no %s); not read" % (odd, text, ghost))


def test_git_dir_parent_step_above_the_code_root_is_unknown(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    text = "../../outside/delta-clone/.git"
    odd = worktree_file(tree, text)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s/%s, which is outside the "
                   "code root; not read" % (odd, odd, text))
    assert trace.under(tree.outside) == [], trace.under(tree.outside)


def test_git_dir_through_a_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    notes = tree.root / "notes.txt"
    odd = worktree_file(tree, "%s/meta" % notes)
    assert_unknown(*run_scan(tree), "worktree %s keeps its git dir at %s/meta, which passes "
                   "through %s, not a folder; not read" % (odd, notes, notes))


def test_git_dir_that_is_a_file_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    notes = tree.root / "notes.txt"
    odd = worktree_file(tree, notes)
    assert_unknown(*run_scan(tree), "worktree %s keeps its git dir at %s, which is not a "
                   "folder; not read" % (odd, notes))


def test_commondir_text_through_a_symlink_is_unknown_and_not_followed(tmp_path, monkeypatch):
    # alpha-wt's commondir names an in-root symlink into the excluded repo. (A
    # repo's .git holding a commondir is refused before it is read.)
    tree = Tree(tmp_path)
    worktree = tree.root / "alpha-wt"
    link = tree.root / "plain" / "lnk"
    link.symlink_to(Path("..") / "secret" / ".git")
    (alpha_wt_gitdir(tree) / "commondir").write_text("%s\n" % link, encoding="utf-8")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s has its common dir at %s, which passes through the "
                   "symlink %s; not followed" % (worktree, link, link))
    assert "acme/secret" not in out, out
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_commondir_file_that_is_a_symlink_is_unknown_and_not_followed(tmp_path, monkeypatch):
    tree = stray_tree(tmp_path)
    pointer = tree.root / "stray" / ".git" / "commondir"
    (tree.root / "secret" / "pointer").write_text("%s\n" % (tree.root / "alpha" / ".git"),
                                                  encoding="utf-8")
    pointer.symlink_to(Path("..") / ".." / "secret" / "pointer")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "repo %s keeps its commondir file at %s, which passes through the "
                   "symlink %s; not followed" % (tree.root / "stray", pointer, pointer))
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_git_config_that_is_a_symlink_is_unknown_and_not_followed(tmp_path, monkeypatch):
    # The linked file, under the excluded folder, would give stray alpha's remote.
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    gitconfig_naming_alpha(tree.root / "secret" / "real-cfg")
    config.unlink()
    config.symlink_to(Path("..") / ".." / "secret" / "real-cfg")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "repo %s keeps its git config at %s, which passes through the "
                   "symlink %s; not followed" % (tree.root / "stray", config, config))
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


# --------------------------------------------------------------------------
# Reading git files: a regular file only, the one the walk verified
# --------------------------------------------------------------------------

FIFO_TIMEOUT_SECONDS = 30


def test_git_config_that_is_a_fifo_is_unknown_and_does_not_hang(tmp_path):
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    config.unlink()
    os.mkfifo(config)
    code, out = run_cli("scan", str(tree.ws), "--host", HOST, timeout=FIFO_TIMEOUT_SECONDS)
    assert_unknown(code, out, "%s is not a regular file; not read" % config)


def test_commondir_that_is_a_fifo_is_unknown_and_does_not_hang(tmp_path):
    tree = Tree(tmp_path)
    pointer = alpha_wt_gitdir(tree) / "commondir"
    pointer.unlink()
    os.mkfifo(pointer)
    code, out = run_cli("scan", str(tree.ws), "--host", HOST, timeout=FIFO_TIMEOUT_SECONDS)
    assert_unknown(code, out, "%s is not a regular file; not read" % pointer)


def test_git_file_swapped_after_its_walk_is_unknown_and_not_read(tmp_path, monkeypatch):
    # A writer replaces stray's config between the walk's lstat and the open;
    # the replacement would give stray alpha's remote.
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    swap = tmp_path / "swap-config"
    gitconfig_naming_alpha(swap)
    real_open = os.open

    def racing_open(path, *args, **kwargs):
        if os.fsdecode(path) == str(config) and swap.exists():
            os.replace(swap, config)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    assert_unknown(*run_scan(tree), "%s changed while it was read; not read" % config)


def test_git_config_swapped_for_a_symlink_after_its_walk_is_refused(tmp_path, monkeypatch):
    # A writer replaces stray's config with a symlink into the excluded folder
    # between the walk's lstat and the open; the open refuses to follow it.
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    target = tree.root / "secret" / "real-cfg"
    gitconfig_naming_alpha(target)
    real_open = os.open
    swapped = []

    def racing_open(path, *args, **kwargs):
        if os.fsdecode(path) == str(config) and not swapped:
            swapped.append(path)
            os.unlink(config)
            os.symlink(Path("..") / ".." / "secret" / "real-cfg", config)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    code, out = run_scan(tree)
    assert swapped, "the config was never opened"
    assert_unknown(code, out, "could not read %s: [Errno %d] %s"
                   % (config, errno.ELOOP, os.strerror(errno.ELOOP)))


def test_git_config_with_a_non_utf8_comment_is_read(tmp_path):
    tree = Tree(tmp_path)
    latin = tree.root / "latin"
    make_repo(latin, REMOTE % "latin")
    with open(latin / ".git" / "config", "ab") as handle:
        handle.write(b"# caf\xe9 \xff\xfe\n")
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  unregistered repo at %s (example.invalid/acme/latin)" % latin
    ], out


# --------------------------------------------------------------------------
# The one git call: config bytes on stdin, from the filesystem root
# --------------------------------------------------------------------------

GIT_REMOTE_CALL = ["git", "config", "--file", "-", "--no-includes", "--null", "--get-all",
                   "remote.origin.url"]
GIT_INCLUDE_CALL = ["git", "config", "--file", "-", "--no-includes", "--name-only",
                    "--get-regexp", r"^(include|includeif)\."]
GIT_WORKTREE_CONFIG_CALL = ["git", "config", "--file", "-", "--no-includes", "--type=bool",
                            "--get", "extensions.worktreeConfig"]
GIT_SET_VARIABLES = ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_OPTIONAL_LOCKS",
                     "GIT_TERMINAL_PROMPT"]


def test_git_reads_config_bytes_only_with_includes_and_outside_config_off(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tree.root / "secret" / ".git"))
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert trace.git_runs, "git never ran"
    # Each config's bytes are listed for include keys, read for per-worktree
    # config, then read for the remote.
    calls = [run["args"] for run in trace.git_runs]
    assert all(call in (GIT_INCLUDE_CALL, GIT_WORKTREE_CONFIG_CALL, GIT_REMOTE_CALL)
               for call in calls), calls
    assert (calls.count(GIT_INCLUDE_CALL) == calls.count(GIT_WORKTREE_CONFIG_CALL)
            == calls.count(GIT_REMOTE_CALL) > 0), calls
    for run in trace.git_runs:
        assert run["cwd"] == os.sep, run["cwd"]
        assert isinstance(run["input"], bytes), run["input"]
        env = run["env"]
        assert sorted(key for key in env if key.startswith("GIT_")) == GIT_SET_VARIABLES, env
        assert env["GIT_CONFIG_NOSYSTEM"] == "1", env
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull, env


def test_git_never_reads_the_repo_scan_runs_from(tmp_path, monkeypatch):
    # git run from inside a repo whose config is broken would fail; scan runs
    # git from the filesystem root, so where it is started does not matter.
    tree = Tree(tmp_path)
    here = tmp_path / "here"
    make_repo(here)
    with open(here / ".git" / "config", "a", encoding="utf-8") as handle:
        handle.write("garbage line here\n")
    monkeypatch.chdir(here)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "PASS  no drift (1 warn)" in out, out


# --------------------------------------------------------------------------
# Identity: an excluded entry is recognised under any spelling that reaches it
# --------------------------------------------------------------------------

def fold_case(monkeypatch, root, name):
    """Make root/<name> answer to any letter case, as a case-insensitive disk does.

    macOS's default volume already behaves this way; the shim makes a
    case-sensitive runner behave the same, so the test runs everywhere.
    """
    prefix = str(root) + os.sep

    def folded(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return path
        text = os.fsdecode(path)
        if not text.startswith(prefix):
            return path
        head, sep, tail = text[len(prefix):].partition(os.sep)
        if head.lower() != name.lower():
            return path
        return prefix + name + sep + tail

    for function in ("lstat", "stat", "open"):
        original = getattr(os, function)
        monkeypatch.setattr(os, function,
                            lambda path, *args, _original=original, **kwargs:
                            _original(folded(path), *args, **kwargs))


def assert_secret_skipped_by_identity(tree, code, out, trace):
    secret = tree.root / "secret"
    assert code == 0, out
    assert "SKIP   %s: excluded path, not read" % secret in out.splitlines(), out
    assert "secret" not in "\n".join(drift_lines(out)), out
    assert trace.beneath(secret) == [], "scan read beneath the excluded path: %r" % trace.beneath(secret)


def test_excluded_path_in_another_letter_case_is_matched_by_identity(tmp_path, monkeypatch):
    # The registry says Secret; the folder on disk is secret.
    tree = Tree(tmp_path)
    tree.registry["excluded"] = [{"path": str(tree.root / "Secret"), "reason": "fixture"}]
    tree.write_registry()
    fold_case(monkeypatch, tree.root, "secret")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_secret_skipped_by_identity(tree, code, out, trace)


def test_excluded_path_through_a_linked_parent_is_matched_by_identity(tmp_path, monkeypatch):
    # The registry reaches secret through a symlinked parent: via -> code.
    tree = Tree(tmp_path)
    (tmp_path / "via").symlink_to(Path("code"))
    tree.registry["excluded"] = [{"path": str(tmp_path / "via" / "secret"), "reason": "fixture"}]
    tree.write_registry()
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_secret_skipped_by_identity(tree, code, out, trace)


def test_common_dir_in_another_letter_case_is_matched_by_identity(tmp_path, monkeypatch):
    # alpha-wt's commondir names <root>/SECRET/.git; the excluded folder is secret.
    tree = Tree(tmp_path)
    worktree = tree.root / "alpha-wt"
    common = tree.root / "SECRET" / ".git"
    (alpha_wt_gitdir(tree) / "commondir").write_text("%s\n" % common, encoding="utf-8")
    fold_case(monkeypatch, tree.root, "secret")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s has its common dir at %s, which is excluded; not read"
                   % (worktree, common))
    assert "acme/secret" not in out, out
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_git_marker_through_a_linked_parent_is_matched_by_identity(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    odd = odd_repo(tree)
    marker = odd / ".git"
    (tmp_path / "via").symlink_to(Path("code"))
    exclude(tree, tmp_path / "via" / "odd" / ".git")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "git marker %s, which is excluded; not read" % marker)
    assert trace.beneath(marker) == [], trace.beneath(marker)


def test_code_root_reached_through_an_excluded_spelling_is_unknown(tmp_path):
    # alias -> the folder above the code root, so alias/code is the code root.
    tree = Tree(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path)
    tree.registry["excluded"].append({"path": str(tmp_path / "alias" / "code"), "reason": "fixture"})
    tree.write_registry()
    assert_unknown(*run_scan(tree), "code root %s is inside an excluded path" % tree.root)


# --------------------------------------------------------------------------
# Worktrees made by git fold into their parent, in both path forms
# --------------------------------------------------------------------------

def test_relative_paths_worktree_folds_into_its_parent(tmp_path):
    tree = Tree(tmp_path)
    root = tree.root
    make_repo(root / "beta", REMOTE % "beta", commit=True)
    git("-C", root / "beta", "worktree", "add", "-q", "--relative-paths", root / "beta-rel")
    text = (root / "beta-rel" / ".git").read_text(encoding="utf-8")
    assert text.startswith("gitdir: ../"), "fixture is not a relative-paths worktree: %r" % text
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  unregistered repo at %s (example.invalid/acme/beta)" % (root / "beta")
    ], "the worktree was not folded into its parent:\n" + out
    assert "INFO   worktree %s folds into %s" % (root / "beta-rel", root / "beta") in out, out


# --------------------------------------------------------------------------
# Per-worktree config: not judged, so a could-not-verify
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "setting",
    ["\tworktreeConfig = true\n", "\tworktreeConfig = yes\n", "\tworktreeconfig\n"],
    ids=["true", "yes", "bare-key"],
)
def test_repo_with_per_worktree_config_is_unknown(tmp_path, setting):
    tree = stray_tree(tmp_path)
    stray = tree.root / "stray"
    config = stray / ".git" / "config"
    with open(config, "a", encoding="utf-8") as handle:
        handle.write("[extensions]\n" + setting)
    assert_unknown(*run_scan(tree), "%s: per-worktree config is not supported (%s sets "
                   "extensions.worktreeConfig true); could not verify" % (stray, config))


def test_left_over_worktree_config_is_never_read(tmp_path):
    # stray has no remote and does not turn on per-worktree config, so git
    # ignores its config.worktree, which names a registered remote.
    tree = stray_tree(tmp_path)
    gitconfig_naming_alpha(tree.root / "stray" / ".git" / "config.worktree")
    assert_stray_is_drift(tree)


# --------------------------------------------------------------------------
# A worktree's git dir must be <common>/worktrees/<name>, with commondir and HEAD
# --------------------------------------------------------------------------

def lookalike_meta(tree):
    """A git dir outside any worktrees/ folder, with HEAD and a commondir naming alpha."""
    meta = tree.root / "plain" / "odd-meta"
    meta.mkdir()
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (meta / "commondir").write_text("%s\n" % (tree.root / "alpha" / ".git"), encoding="utf-8")
    return meta


def sibling_meta(tree):
    """A git dir with HEAD and commondir under alpha's .git, but not in worktrees/."""
    meta = tree.root / "alpha" / ".git" / "other" / "odd"
    meta.mkdir(parents=True)
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (meta / "commondir").write_text("../..\n", encoding="utf-8")
    return meta


def no_head(tree):
    (alpha_wt_gitdir(tree) / "HEAD").unlink()


def head_folder(tree):
    head = alpha_wt_gitdir(tree) / "HEAD"
    head.unlink()
    head.mkdir()


def cross_common_meta(tree):
    """alpha/.git/worktrees/cross, with HEAD, whose commondir names gamma's .git."""
    meta = tree.root / "alpha" / ".git" / "worktrees" / "cross"
    meta.mkdir()
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (meta / "commondir").write_text("%s\n" % (tree.root / "gamma" / ".git"), encoding="utf-8")
    return meta


@pytest.mark.parametrize(
    "case",
    ["checkout", "git-dir", "lookalike", "sibling-folder", "no-head", "head-folder",
     "cross-common"],
)
def test_git_dir_that_is_not_a_worktree_folder_is_unknown_and_does_not_fold(tmp_path, case):
    tree = Tree(tmp_path)
    root = tree.root
    if case in ("no-head", "head-folder"):
        worktree = root / "alpha-wt"
        text = str(alpha_wt_gitdir(tree))
        (no_head if case == "no-head" else head_folder)(tree)
    else:
        if case == "checkout":
            text = str(root / "alpha")
        elif case == "git-dir":
            text = str(root / "alpha" / ".git")
        elif case == "sibling-folder":
            text = str(sibling_meta(tree))
        elif case == "cross-common":
            text = str(cross_common_meta(tree))
        else:
            text = str(lookalike_meta(tree))
        worktree = worktree_file(tree, text)
    code, out = run_scan(tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which is not a worktree git "
                   "folder: <common>/worktrees/<name> with commondir and HEAD files; not read"
                   % (worktree, text))
    assert "folds into" not in out, out


# --------------------------------------------------------------------------
# The plain shape: no symlink in the code root or in a registered path
# --------------------------------------------------------------------------

LINK_REACH = {
    "lane-link": lambda tree: ("under", tree.root / "alpha" / "lanes"),
    "ext-link": lambda tree: ("under", tree.outside),
    "sneaky": lambda tree: ("beneath", tree.root / "secret"),
}


@pytest.mark.parametrize("name", sorted(LINKS))
def test_symlink_in_the_code_root_is_unknown_and_never_followed(tmp_path, monkeypatch, name):
    # Declared change: a symlink in the code root was followed hop by hop, or
    # printed a SKIP line. Now it ends the scan, and its target is never read.
    tree = Tree(tmp_path, links=(name,))
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "%s is a symlink in the code root; not followed "
                   "(list it under excluded to skip it)" % (tree.root / name))
    assert [path for call, path in trace.calls if call == "readlink"] == [], trace.calls
    how, base = LINK_REACH[name](tree)
    touched = trace.under(base) if how == "under" else trace.beneath(base)
    assert touched == [], "scan followed the symlink: %r" % touched


def test_excluded_symlink_in_the_code_root_is_skipped(tmp_path, monkeypatch):
    # The exclusion check comes before the symlink check.
    tree = Tree(tmp_path, links=("sneaky",))
    exclude(tree, tree.root / "sneaky")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 0, out
    assert "SKIP   %s: excluded path, not read" % (tree.root / "sneaky") in out.splitlines(), out
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_registered_path_through_a_symlink_is_unknown(tmp_path, monkeypatch):
    # By its target the registered path is gamma, a repo in the code root.
    tree = Tree(tmp_path)
    link = tree.root / "plain" / "lnk"
    link.symlink_to(Path("..") / "gamma")
    tree.registry["projects"].append(project(tree.root, "gamma-alias", paths={HOST: str(link)}))
    tree.write_registry()
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "registered path %s: passes through the symlink %s; not followed"
                   % (link, link))


def test_git_dir_excluded_by_a_glob_is_refused_by_its_text(tmp_path, monkeypatch):
    # A glob names no single entry, so its text is the only guard.
    tree = Tree(tmp_path)
    tree.registry["excluded"] = [{"path": str(tree.root / "secr?t"), "reason": "fixture"}]
    tree.write_registry()
    meta = secret_worktree_meta(tree)
    odd = worktree_file(tree, meta)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "worktree %s keeps its git dir at %s, which is excluded; not read"
                   % (odd, meta))
    assert trace.beneath(tree.root / "secret") == [], trace.beneath(tree.root / "secret")


def test_git_marker_excluded_by_a_glob_is_unknown_and_not_touched(tmp_path, monkeypatch):
    # The glob names no entry on disk, so the marker's text is its only guard.
    tree = Tree(tmp_path)
    marker = tree.root / "alpha-wt" / ".git"
    exclude(tree, tree.root / "alpha-w*" / ".git")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "git marker %s, which is excluded; not read" % marker)
    assert trace.under(marker) == [], trace.under(marker)
    assert trace.opened(marker) == [], trace.opened(marker)


GLOB_NAMES = pytest.mark.parametrize("name", ["[priv]", "pr*v", "pr?v"],
                                     ids=["brackets", "star", "question-mark"])


def glob_named_repo(tree, name, written):
    """A repo whose real folder name holds a glob character, excluded as `written`."""
    folder = tree.root / name
    make_repo(folder, "https://example.invalid/acme/private-sentinel.git")
    exclude(tree, tree.root / written)
    return folder


def assert_glob_named_skipped(folder, code, out, trace):
    assert code == 0, out
    assert "SKIP   %s: excluded path, not read" % folder in out.splitlines(), out
    assert "sentinel" not in out, out
    assert trace.beneath(folder) == [], "scan touched beneath the excluded path: %r" % trace.beneath(folder)
    assert trace.opened(folder) == [], trace.opened(folder)
    assert trace.git_under(folder) == [], trace.git_under(folder)


@GLOB_NAMES
def test_excluded_name_holding_a_glob_character_is_matched_by_its_text(tmp_path, monkeypatch, name):
    tree = Tree(tmp_path)
    folder = glob_named_repo(tree, name, name)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_glob_named_skipped(folder, code, out, trace)
    # Its text refuses it before any file call: the one call that reaches it
    # is the lstat that learns its identity, never a look by the listing.
    calls = [call for call in trace.calls if reaches(call[1], str(folder), itself=True)]
    assert calls == [("lstat", str(folder))], calls


@GLOB_NAMES
def test_excluded_name_holding_a_glob_character_in_another_case_is_matched_by_identity(
        tmp_path, monkeypatch, name):
    # The registry says the name in capitals; the folder on disk is lower case.
    tree = Tree(tmp_path)
    folder = glob_named_repo(tree, name, name.upper())
    fold_case(monkeypatch, tree.root, name)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_glob_named_skipped(folder, code, out, trace)


# --------------------------------------------------------------------------
# The plain shape: a repo's .git folder has no commondir
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["file", "folder", "fifo"])
def test_repo_git_folder_holding_commondir_is_unknown_and_not_read(tmp_path, monkeypatch, kind):
    # Declared change: it was followed. Here stray's would name alpha's .git and
    # give the unregistered repo a registered remote.
    tree = stray_tree(tmp_path)
    stray = tree.root / "stray"
    pointer = stray / ".git" / "commondir"
    if kind == "file":
        pointer.write_text("%s\n" % (tree.root / "alpha" / ".git"), encoding="utf-8")
    elif kind == "folder":
        pointer.mkdir()
    else:
        os.mkfifo(pointer)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, "repo %s holds %s, which only a worktree's git folder has; "
                   "not read" % (stray, pointer))
    assert trace.opened(pointer) == [], trace.opened(pointer)


# --------------------------------------------------------------------------
# The plain shape: a .git file is the one gitdir line
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tail",
    ["gitdir: {gamma}\n", "more\n", "\n# note\n"],
    ids=["second-gitdir", "second-line", "after-a-blank-line"],
)
def test_git_file_with_more_than_its_gitdir_line_is_unknown(tmp_path, tail):
    tree = Tree(tmp_path)
    marker = tree.root / "alpha-wt" / ".git"
    marker.write_text("gitdir: %s\n" % alpha_wt_gitdir(tree)
                      + tail.format(gamma=tree.root / "gamma" / ".git"), encoding="utf-8")
    code, out = run_scan(tree)
    assert_unknown(code, out, "%s holds more than its gitdir line; not read" % marker)
    assert "folds into" not in out, out


@pytest.mark.parametrize("ending", ["", "\n", "\r\n", "\n\n"],
                         ids=["no-newline", "newline", "crlf", "blank-line"])
def test_git_file_line_endings_still_fold(tmp_path, ending):
    tree = Tree(tmp_path)
    marker = tree.root / "alpha-wt" / ".git"
    marker.write_bytes(("gitdir: %s%s" % (alpha_wt_gitdir(tree), ending)).encode("utf-8"))
    code, out = run_scan(tree)
    assert code == 0, out
    assert "INFO   worktree %s folds into %s" % (tree.root / "alpha-wt", tree.root / "alpha") in out, out


def test_git_file_whose_first_line_is_not_gitdir_is_unknown(tmp_path):
    tree = Tree(tmp_path)
    marker = tree.root / "alpha-wt" / ".git"
    marker.write_text("\ngitdir: %s\n" % alpha_wt_gitdir(tree), encoding="utf-8")
    assert_unknown(*run_scan(tree), "%s does not name a gitdir" % marker)


# --------------------------------------------------------------------------
# The one reader: one link, at most 1 MiB, unchanged across the read
# --------------------------------------------------------------------------

CAP = 1024 * 1024
SENTINEL_REMOTE = "https://example.invalid/acme/sentinel.git"


class ReadSpy:
    """Counts the opens of `path` and every os.read on a descriptor it was opened as."""

    def __init__(self, monkeypatch, path):
        self.path = str(path)
        self.fds = set()
        self.opened = 0
        self.reads = 0
        real_open, real_read, real_close = os.open, os.read, os.close

        def spy_open(name, *args, **kwargs):
            fd = real_open(name, *args, **kwargs)
            if os.fsdecode(name) == self.path:
                self.opened += 1
                self.fds.add(fd)
            return fd

        def spy_read(fd, *args, **kwargs):
            if fd in self.fds:
                self.reads += 1
            return real_read(fd, *args, **kwargs)

        def spy_close(fd):
            self.fds.discard(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "read", spy_read)
        monkeypatch.setattr(os, "close", spy_close)


def excluded_twin(tree, text):
    """A file in the excluded folder holding `text`, to be hard linked into a repo."""
    twin = tree.root / "secret" / "twin"
    twin.write_text(text, encoding="utf-8")
    return twin


def hardlinked_config(tree):
    twin = excluded_twin(tree, '[remote "origin"]\n\turl = %s\n' % SENTINEL_REMOTE)
    config = tree.root / "stray" / ".git" / "config"
    config.unlink()
    os.link(twin, config)
    return config


def hardlinked_git_file(tree):
    twin = excluded_twin(tree, "gitdir: %s\n" % (tree.root / "sentinel"))
    odd = tree.root / "odd"
    odd.mkdir()
    os.link(twin, odd / ".git")
    return odd / ".git"


def hardlinked_commondir(tree):
    twin = excluded_twin(tree, "%s\n" % (tree.root / "sentinel" / ".git"))
    pointer = alpha_wt_gitdir(tree) / "commondir"
    pointer.unlink()
    os.link(twin, pointer)
    return pointer


HARDLINKED = {
    "config": hardlinked_config,
    "git-file": hardlinked_git_file,
    "commondir": hardlinked_commondir,
}


@pytest.mark.parametrize("which", sorted(HARDLINKED))
def test_hardlinked_git_file_is_unknown_and_never_read(tmp_path, monkeypatch, which):
    # Declared change: it was read. The link aliases a file in the excluded
    # folder; none of its bytes is read, so its sentinel text never prints.
    tree = stray_tree(tmp_path)
    path = HARDLINKED[which](tree)
    spy = ReadSpy(monkeypatch, path)
    code, out = run_scan(tree)
    assert_unknown(code, out, "%s has 2 hard links; not read" % path)
    assert "sentinel" not in out, out
    assert spy.opened == 1, spy.opened
    assert spy.reads == 0, "scan read %d time(s) from the hard-linked file" % spy.reads


def test_git_config_larger_than_the_cap_is_unknown(tmp_path, monkeypatch):
    # Refused on its size before any byte is read.
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    with open(config, "a", encoding="utf-8") as handle:
        handle.write("# " + "x" * CAP + "\n")
    spy = ReadSpy(monkeypatch, config)
    assert_unknown(*run_scan(tree), "%s is larger than %d bytes; not read" % (config, CAP))
    assert spy.opened == 1, spy.opened
    assert spy.reads == 0, "scan read %d time(s) from the oversized config" % spy.reads


def test_git_config_at_the_cap_is_read(tmp_path):
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    size = config.stat().st_size
    with open(config, "a", encoding="utf-8") as handle:
        handle.write("# " + "x" * (CAP - size - 3) + "\n")
    assert config.stat().st_size == CAP
    assert_stray_is_drift(tree)


def test_git_config_rewritten_while_it_is_read_is_unknown(tmp_path, monkeypatch):
    # A writer rewrites stray's config in place, same inode and same size,
    # between the reader's reads. The file's times are aged first, so a
    # coarse timestamp cannot hide the rewrite.
    tree = stray_tree(tmp_path)
    config = tree.root / "stray" / ".git" / "config"
    data = config.read_bytes()
    rewritten = data.replace(b"bare = false", b"bare = FALSE")
    assert rewritten != data and len(rewritten) == len(data), data
    aged = time.time() - 3600
    os.utime(config, (aged, aged))
    real_open, real_read = os.open, os.read
    fds = set()
    rewrote = []

    def spy_open(name, *args, **kwargs):
        fd = real_open(name, *args, **kwargs)
        if os.fsdecode(name) == str(config):
            fds.add(fd)
        return fd

    def rewriting_read(fd, *args, **kwargs):
        chunk = real_read(fd, *args, **kwargs)
        if fd in fds and chunk and not rewrote:
            rewrote.append(fd)
            with builtins.open(config, "r+b") as handle:
                handle.write(rewritten)
        return chunk

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "read", rewriting_read)
    code, out = run_scan(tree)
    assert rewrote, "the config was never read"
    assert_unknown(code, out, "%s changed while it was read; not read" % config)


def test_short_read_is_unknown(tmp_path, monkeypatch):
    # fstat reports the same file before and after, but fewer bytes arrive
    # than its size: the bytes are not the file.
    path = tmp_path / "config"
    path.write_bytes(b"0123456789")
    info = os.lstat(path)
    same = types.SimpleNamespace(
        st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino, st_nlink=1,
        st_size=10, st_mtime_ns=1, st_ctime_ns=1,
    )
    chunks = [b"012", b""]
    monkeypatch.setattr(os, "fstat", lambda fd: same)
    monkeypatch.setattr(os, "read", lambda fd, size: chunks.pop(0))
    with pytest.raises(scan.Unverifiable) as caught:
        scan.read_file(str(path), info)
    assert str(caught.value) == "%s changed while it was read; not read" % path


# --------------------------------------------------------------------------
# The plain shape: one config, present, with at most one origin URL
# --------------------------------------------------------------------------

def test_repo_without_a_git_config_is_unknown(tmp_path):
    # Declared change: it was "no remote".
    tree = stray_tree(tmp_path)
    stray = tree.root / "stray"
    config = stray / ".git" / "config"
    config.unlink()
    assert_unknown(*run_scan(tree), "repo %s keeps its git config at %s, which does not exist "
                   "(no %s); not read" % (stray, config, config))


def add_to_config(tree, text):
    config = tree.root / "stray" / ".git" / "config"
    with open(config, "a", encoding="utf-8") as handle:
        handle.write(text)
    return config


def test_origin_url_set_more_than_once_is_unknown(tmp_path):
    # Declared change: git's last value was used.
    tree = stray_tree(tmp_path)
    config = add_to_config(tree, '[remote "origin"]\n\turl = %s\n\turl = %s\n'
                           % (REMOTE % "alpha", REMOTE % "stray"))
    assert_unknown(*run_scan(tree), "%s: remote.origin.url is set 2 times in %s; could not verify"
                   % (tree.root / "stray", config))


@pytest.mark.parametrize("line", ["\turl =\n", "\turl\n"], ids=["empty", "bare-key"])
def test_empty_origin_url_is_unknown(tmp_path, line):
    # Declared change: it read as "no remote".
    tree = stray_tree(tmp_path)
    config = add_to_config(tree, '[remote "origin"]\n' + line)
    assert_unknown(*run_scan(tree), "%s: remote.origin.url is empty in %s; could not verify"
                   % (tree.root / "stray", config))


def test_origin_url_holding_an_escaped_newline_is_one_value(tmp_path):
    tree = stray_tree(tmp_path)
    add_to_config(tree, '[remote "origin"]\n\turl = "https://example.invalid/acme/stray\\n.git"\n')
    code, out = run_scan(tree)
    assert code == 1, out
    assert "UNKNOWN" not in out, out
    assert "DRIFT  unregistered repo at %s (https://example.invalid/acme/stray\n" % (
        tree.root / "stray") in out, out


@pytest.mark.parametrize("answer", ["printf 'x'; exit 0", "printf 'x'; exit 1"],
                         ids=["no-nul", "output-on-exit-1"])
def test_unexpected_git_output_for_the_remote_is_unknown(tmp_path, monkeypatch, answer):
    # The shim stands in for a git whose remote answer is not the NUL-ended form.
    tree = stray_tree(tmp_path)
    git_shim(tmp_path, monkeypatch,
             'case "$*" in *remote.origin.url*) %s;; esac' % answer)
    assert_unknown(*run_scan(tree), "git gave unexpected output on %s (%s); could not verify"
                   % (tree.root / "alpha", tree.root / "alpha" / ".git" / "config"))


# --------------------------------------------------------------------------
# A registered path at any depth passes the same shape check
# --------------------------------------------------------------------------

def register_nested(tree, nested):
    tree.registry["projects"].append(project(tree.root, "nested", paths={HOST: str(nested)}))
    tree.write_registry()


def test_nested_registered_repo_in_the_plain_shape_passes(tmp_path):
    tree = Tree(tmp_path)
    nested = tree.root / "plain" / "nested"
    make_repo(nested, REMOTE % "nested")
    register_nested(tree, nested)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "PASS  no drift (1 warn)" in out, out


@pytest.mark.parametrize(
    "case", ["worktree-config", "include", "commondir", "malformed-git-file"],
)
def test_nested_registered_path_outside_the_plain_shape_is_unknown(tmp_path, case):
    # Declared change: a registered path below a code root entry was only
    # checked for a .git marker, so each of these passed.
    tree = Tree(tmp_path)
    nested = tree.root / "plain" / "nested"
    if case == "malformed-git-file":
        nested.mkdir()
        (nested / ".git").write_text("nonsense\n", encoding="utf-8")
        reason = "%s does not name a gitdir" % (nested / ".git")
    else:
        make_repo(nested, REMOTE % "nested")
        config = nested / ".git" / "config"
        if case == "worktree-config":
            # What `git sparse-checkout init` writes.
            with open(config, "a", encoding="utf-8") as handle:
                handle.write("[extensions]\n\tworktreeConfig = true\n")
            reason = ("%s: per-worktree config is not supported (%s sets "
                      "extensions.worktreeConfig true); could not verify" % (nested, config))
        elif case == "include":
            with_include(config, "[include]", tree.root / "secret" / "included.gitconfig")
            reason = ("%s: git config includes are not followed (%s has one); not read"
                      % (nested, config))
        else:
            pointer = nested / ".git" / "commondir"
            pointer.write_text("%s\n" % (tree.root / "alpha" / ".git"), encoding="utf-8")
            reason = ("repo %s holds %s, which only a worktree's git folder has; not read"
                      % (nested, pointer))
    register_nested(tree, nested)
    assert_unknown(*run_scan(tree), reason)


# --------------------------------------------------------------------------
# A worktree's git dir is <repo>/.git/worktrees/<name>, <repo> a known repo
# --------------------------------------------------------------------------

def not_a_worktree(worktree, text):
    return ("worktree %s keeps its git dir at %s, which is not a worktree git folder: "
            "<common>/worktrees/<name> with commondir and HEAD files; not read" % (worktree, text))


def test_worktree_git_dir_inside_a_working_tree_is_unknown_and_not_read(tmp_path, monkeypatch):
    # alpha/src/worktrees/x looks like a worktree git dir whose common dir is
    # alpha/src, a folder in alpha's working tree. Nothing there is opened.
    tree = Tree(tmp_path)
    src = tree.root / "alpha" / "src"
    meta = src / "worktrees" / "x"
    meta.mkdir(parents=True)
    (meta / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (meta / "commondir").write_text("../..\n", encoding="utf-8")
    (src / "config").write_text('[remote "origin"]\n\turl = %s\n' % SENTINEL_REMOTE,
                                encoding="utf-8")
    odd = worktree_file(tree, meta)
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert_unknown(code, out, not_a_worktree(odd, meta))
    assert trace.opened(src) == [], "scan opened inside a working tree: %r" % trace.opened(src)


def test_worktree_of_an_unregistered_repo_below_the_code_root_is_unknown(tmp_path):
    # Declared change: it folded into its repo, reported as unregistered drift.
    tree = Tree(tmp_path)
    nested = tree.root / "plain" / "nested"
    make_repo(nested, REMOTE % "nested", commit=True)
    git("-C", nested, "worktree", "add", "-q", tree.root / "nested-wt")
    gitdir = nested / ".git" / "worktrees" / "nested-wt"
    assert_unknown(*run_scan(tree), not_a_worktree(tree.root / "nested-wt", gitdir))


def test_worktree_of_a_nested_registered_repo_folds(tmp_path):
    tree = Tree(tmp_path)
    nested = tree.root / "plain" / "nested"
    make_repo(nested, REMOTE % "nested", commit=True)
    git("-C", nested, "worktree", "add", "-q", tree.root / "nested-wt")
    register_nested(tree, nested)
    code, out = run_scan(tree)
    assert code == 0, out
    assert "INFO   worktree %s folds into %s" % (tree.root / "nested-wt", nested) in out, out


def test_worktree_named_before_its_repo_folds(tmp_path):
    tree = Tree(tmp_path)
    zz = tree.root / "zz"
    make_repo(zz, REMOTE % "zz", commit=True)
    git("-C", zz, "worktree", "add", "-q", tree.root / "aa-wt")
    code, out = run_scan(tree)
    assert code == 1, out
    assert drift_lines(out) == [
        "DRIFT  unregistered repo at %s (example.invalid/acme/zz)" % zz
    ], out
    assert "INFO   worktree %s folds into %s" % (tree.root / "aa-wt", zz) in out, out
