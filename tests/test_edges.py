"""`workspace scan WORKSPACE --edges`: edge proposals from files inside registered repos.

Every fixture is built in a temp dir with synthetic names. The tree:

    code/                 the code root for host `testhost`
      app/                registered, active: the repo the edge signals are written into
      beta/, roomkit/, serotine/, b-mod/, b-pkg/
                          registered, active: the projects app may name
    ws/registry.yaml      those projects, no edges

Each test writes the files it needs into app and asks the scan for its lines.
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workspace import edges, scan
from test_scan import HOST, REMOTE, REPO_ROOT, Trace, Tree, make_repo, run_scan

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "scan-golden"

NAMES = ("app", "beta", "roomkit", "serotine", "b-mod", "b-pkg")
EDGE_PRUNED = "INFO   edges: %d symlinks not followed, %d excluded entries not entered"


class EdgeTree:
    def __init__(self, base, names=NAMES):
        self.base = base
        self.root = base / "code"
        self.ws = base / "ws"
        self.outside = base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        for name in names:
            make_repo(self.root / name, REMOTE % name)
        self.app = self.root / "app"
        self.registry = {
            "system": "Fixture",
            "code_roots": {HOST: str(self.root)},
            "excluded": [],
            "projects": [
                {"name": name, "remote": REMOTE % name, "paths": {HOST: str(self.root / name)},
                 "subpath": None, "lifecycle": "active"}
                for name in names
            ],
            "edges": [],
            "rejected_edges": [],
        }
        self.ws.mkdir()
        self.write_registry()

    def write_registry(self):
        (self.ws / "registry.yaml").write_text(
            yaml.safe_dump(self.registry, sort_keys=False), encoding="utf-8")

    def put(self, rel, text, base=None):
        path = (base or self.app) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def edge(self, origin, target, kind, evidence):
        self.registry["edges"].append({"from": origin, "to": target, "kind": kind,
                                       "evidence": evidence, "approved_at": "2026-11-01"})
        self.write_registry()

    def exclude(self, path):
        self.registry["excluded"].append({"path": str(path), "reason": "fixture private"})
        self.write_registry()


def run_edges(tree, edges_on=True):
    buffer = io.StringIO()
    code = scan.main(str(tree.ws), HOST, out=buffer, edges=edges_on)
    return code, buffer.getvalue()


def traced(monkeypatch, call):
    """Run `call` with every filesystem call recorded, as UT4's Trace records them."""
    trace = Trace()
    with monkeypatch.context() as patch:
        for module, name in Trace.FUNCTIONS:
            patch.setattr(module, name, trace.wrap(name, getattr(module, name)))
        result = call()
    return trace, result


def edge_lines(out):
    return [line for line in out.splitlines() if "→" in line or "edges" in line
            or "could not read" in line]


def candidates(out):
    return [line for line in out.splitlines() if line.startswith("WARN   candidate")]


# --------------------------------------------------------------------------
# Detectors (E3) and evidence (E4)
# --------------------------------------------------------------------------

def test_shell_source_of_a_project_path_is_a_source_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "#!/bin/sh\nset -e\nsource ~/src/beta/lib.sh\n")
    code, out = run_edges(tree)
    assert code == 0, out
    assert candidates(out) == ["WARN   candidate app → beta (source) run.sh:3"], out


def test_build_file_naming_a_project_is_a_build_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("pyproject.toml", "[project]\nname = \"app\"\ndependencies = [\"b-pkg>=1\"]\n")
    code, out = run_edges(tree)
    assert "WARN   candidate app → b-pkg (build) pyproject.toml:3" in candidates(out), out


def test_caps_identifier_naming_a_project_is_a_runtime_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("tool.py", "import os\nBIN = os.environ['ROOMKIT_BIN']\n")
    code, out = run_edges(tree)
    assert candidates(out) == ["WARN   candidate app → roomkit (runtime) tool.py:2"], out


def test_sourcing_a_project_push_env_script_is_a_runtime_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("push.sh", ". ./serotine-push-env.sh\n")
    code, out = run_edges(tree)
    assert candidates(out) == ["WARN   candidate app → serotine (runtime) push.sh:1"], out


def test_python_import_with_underscores_is_a_source_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("main.py", "\nimport b_mod\n")
    code, out = run_edges(tree)
    assert candidates(out) == ["WARN   candidate app → b-mod (source) main.py:2"], out


def test_python_sys_path_naming_a_project_is_a_source_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("boot.py", "import sys\nsys.path.insert(0, '../roomkit/lib')\n")
    code, out = run_edges(tree)
    assert "WARN   candidate app → roomkit (source) boot.py:2" in candidates(out), out


def test_hostname_whose_first_label_is_a_project_is_a_runtime_candidate(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("client.js", "// client\nconst url = 'https://serotine.example.invalid/api';\n")
    code, out = run_edges(tree)
    assert candidates(out) == ["WARN   candidate app → serotine (runtime) client.js:2"], out


def test_self_edges_are_dropped(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("self.sh", "source ~/src/app/lib.sh\nAPP_HOME=1\n")
    code, out = run_edges(tree)
    assert candidates(out) == [], out


def test_rejected_pair_is_never_proposed(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.registry["rejected_edges"].append(
        {"from": "app", "to": "beta", "date": "2026-11-01", "reason": "not real"})
    tree.write_registry()
    code, out = run_edges(tree)
    assert candidates(out) == [], out


def test_pair_with_an_approved_edge_of_another_kind_is_not_proposed(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.edge("app", "beta", "runtime", "calls its API")
    code, out = run_edges(tree)
    assert candidates(out) == [], out


def test_evidence_is_a_path_and_line_never_the_text(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("config.py", "\n\n\n\n\n\nBETA_URL = \"TEXT_SENTINEL_77\"\n")
    code, out = run_edges(tree)
    assert candidates(out) == ["WARN   candidate app → beta (runtime) config.py:7"], out
    assert "TEXT_SENTINEL_77" not in out, out


# --------------------------------------------------------------------------
# Stale edges (E6)
# --------------------------------------------------------------------------

def test_approved_edge_with_detector_evidence_and_no_signal_is_stale(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.edge("app", "beta", "runtime", "run.sh:3")
    code, out = run_edges(tree)
    assert code == 0, out
    assert "WARN   stale app → beta (runtime) run.sh:3" in out.splitlines(), out


def test_approved_edge_with_a_signal_is_not_stale(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.edge("app", "beta", "runtime", "run.sh:1")
    code, out = run_edges(tree)
    assert "WARN   stale" not in out, out


def test_prose_evidence_edges_are_counted_once_not_checked(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.edge("app", "beta", "runtime", "polls read endpoints")
    tree.edge("app", "roomkit", "protocol", "shares the rooms folder")
    code, out = run_edges(tree)
    assert "WARN   stale" not in out, out
    assert [line for line in out.splitlines() if "prose evidence" in line] == [
        "INFO   2 edges have prose evidence and are not checked for staleness"], out


def test_edge_from_a_project_missing_on_this_host_is_not_checked(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.registry["projects"].append({"name": "ghost", "remote": REMOTE % "ghost",
                                      "paths": {HOST: str(tree.root / "ghost")},
                                      "subpath": None, "lifecycle": "active"})
    tree.edge("ghost", "beta", "runtime", "run.sh:3")
    code, out = run_edges(tree)
    assert "WARN   stale" not in out, out
    assert ("INFO   edge ghost → beta (runtime) not checked: `ghost` is not on this host"
            in out.splitlines()), out


# --------------------------------------------------------------------------
# What is never read (E2): .env*, symlinks, excluded entries, nested repos
# --------------------------------------------------------------------------

def env_tree(tmp_path):
    tree = EdgeTree(tmp_path)
    for name in (".env.example", ".env.sh", ".env", ".envrc"):
        tree.put(name, "SECRET_SENTINEL_9f3=hunter2\nBETA_URL=https://beta.example.invalid\n")
    return tree


def test_env_files_are_never_touched(tmp_path, monkeypatch):
    tree = env_tree(tmp_path)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 0, out
    assert "SECRET_SENTINEL_9f3" not in out, out
    assert ".env" not in out, out
    assert candidates(out) == [], out
    touched = [path for path in trace.paths if os.path.basename(path).startswith(".env")]
    assert touched == [], touched


def test_env_files_are_never_opened(tmp_path, monkeypatch):
    tree = env_tree(tmp_path)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    opened = [path for name, path in trace.calls
              if name in Trace.OPENERS and os.path.basename(path).startswith(".env")]
    assert opened == [], opened


def test_symlinked_file_out_of_the_root_is_not_read(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    target = tree.outside / "secret.sh"
    target.write_text("source ~/src/beta/lib.sh\n", encoding="utf-8")
    (tree.app / "linked.sh").symlink_to(target)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 0, out
    assert candidates(out) == [], out
    assert trace.under(tree.outside) == [], trace.under(tree.outside)
    assert EDGE_PRUNED % (1, 0) in out.splitlines(), out
    assert "could not read" not in out, out


def test_symlinked_folder_is_not_walked(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "echo\n", base=tree.outside / "real")
    tree.put("deep/x.sh", "source ~/src/beta/lib.sh\n", base=tree.outside / "real")
    (tree.app / "vendored").symlink_to(tree.outside / "real")
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    assert trace.beneath(tree.app / "vendored") == [], trace.beneath(tree.app / "vendored")
    assert trace.under(tree.outside) == [], trace.under(tree.outside)
    assert EDGE_PRUNED % (1, 0) in out.splitlines(), out
    assert "could not read" not in out, out


def test_excluded_subfolder_is_never_entered(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    private = tree.app / "private"
    tree.put("private/run.sh", "source ~/src/beta/lib.sh\n")
    tree.exclude(str(private) + "/**")
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 0, out
    assert candidates(out) == [], out
    assert trace.beneath(private) == [], trace.beneath(private)
    assert EDGE_PRUNED % (0, 1) in out.splitlines(), out


def fold_below(monkeypatch, folder, name):
    """Make folder/<name> answer to any letter case, as a case-insensitive disk does."""
    prefix = str(folder) + os.sep

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


def test_excluded_subfolder_in_another_letter_case_is_refused_by_identity(tmp_path, monkeypatch):
    # The registry says Private; the folder on disk is private.
    tree = EdgeTree(tmp_path)
    private = tree.app / "private"
    tree.put("private/run.sh", "source ~/src/beta/lib.sh\n")
    tree.exclude(tree.app / "Private")
    fold_below(monkeypatch, tree.app, "private")
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    assert trace.beneath(private) == [], trace.beneath(private)
    assert EDGE_PRUNED % (0, 1) in out.splitlines(), out


def test_symlinked_alias_of_an_excluded_folder_is_not_followed(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    private = tree.app / "private"
    tree.put("private/run.sh", "source ~/src/beta/lib.sh\n")
    tree.exclude(private)
    (tree.app / "alias").symlink_to(Path("private"))
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    assert trace.beneath(private) == [], trace.beneath(private)
    assert EDGE_PRUNED % (1, 1) in out.splitlines(), out
    assert "could not read" not in out, out


def test_glob_excluded_entry_in_a_repo_is_pruned_by_its_text(tmp_path, monkeypatch):
    # A glob in the middle of a name is matched by its text, as on plain scan.
    tree = EdgeTree(tmp_path)
    private = tree.app / "ed" / "priv-notes"
    tree.put("ed/priv-notes/run.sh", "source ~/src/beta/lib.sh\n")
    tree.exclude(str(tree.app / "ed" / "priv-*"))
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    assert trace.under(private) == [], trace.under(private)
    assert EDGE_PRUNED % (0, 1) in out.splitlines(), out


@pytest.mark.parametrize("swap", ["symlink", "folder"])
def test_folder_swapped_after_its_walk_is_not_listed(tmp_path, monkeypatch, swap):
    tree = EdgeTree(tmp_path)
    sub = tree.app / "sub"
    tree.put("sub/run.sh", "echo\n")
    elsewhere = tree.outside / "elsewhere"
    tree.put("x.sh", "source ~/src/beta/lib.sh\n", base=elsewhere)
    original = scan.walk

    def swapping_walk(scope, start, text, guard_repos=False, missing_ok=False):
        hops = original(scope, start, text, guard_repos, missing_ok)
        if hops and hops[-1][0] == str(sub):
            # The old folder is kept under another name, so its node is not reused.
            sub.rename(tree.outside / "old-sub")
            if swap == "symlink":
                sub.symlink_to(elsewhere)
            else:
                # Another folder under the same name: the walked identity no longer matches.
                tree.put("sub/y.sh", "source ~/src/roomkit/lib.sh\n")
        return hops

    monkeypatch.setattr(scan, "walk", swapping_walk)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 0, out
    reason = "could not open it" if swap == "symlink" else "it changed after it was walked"
    assert any(line.startswith("WARN   could not read %s (%s" % (sub, reason))
               for line in out.splitlines()), out
    assert candidates(out) == [], out
    assert trace.under(elsewhere) == [], trace.under(elsewhere)


def test_nested_repo_is_not_walked(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    nested = tree.app / "vendor" / "c"
    make_repo(nested, REMOTE % "c")
    tree.put("run.sh", "source ~/src/beta/lib.sh\n", base=nested)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    inside = [path for path in trace.paths if path.startswith(str(nested) + os.sep)]
    assert inside == [], inside


def test_archived_project_is_not_walked(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.registry["projects"][0]["lifecycle"] = "archived"
    tree.write_registry()
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert candidates(out) == [], out
    assert trace.opened(tree.app / "run.sh") == [], trace.opened(tree.app / "run.sh")


@pytest.mark.parametrize("size, read", [(400001, False), (400000, True)])
def test_file_over_the_size_cap_is_skipped(tmp_path, size, read):
    tree = EdgeTree(tmp_path)
    line = "source ~/src/beta/lib.sh\n"
    tree.put("big.sh", line + "#" * (size - len(line) - 1) + "\n")
    assert (tree.app / "big.sh").stat().st_size == size
    code, out = run_edges(tree)
    assert (candidates(out) == ["WARN   candidate app → beta (source) big.sh:1"]) == read, out


# --------------------------------------------------------------------------
# Exit codes and output (E7, E8, I3)
# --------------------------------------------------------------------------

def test_candidates_and_stale_edges_do_not_change_the_exit_code(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.edge("app", "roomkit", "runtime", "gone.sh:1")
    code, out = run_edges(tree)
    assert code == 0, out
    assert candidates(out) and "WARN   stale" in out, out
    assert out.splitlines()[-1] == "PASS  no drift (0 warn)", out


def test_drift_still_exits_1_with_candidates(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    make_repo(tree.root / "stray", REMOTE % "stray")
    code, out = run_edges(tree)
    assert code == 1, out
    assert candidates(out), out


def test_unreadable_and_hardlinked_files_are_warnings_not_unknown(tmp_path):
    tree = EdgeTree(tmp_path)
    locked = tree.put("locked.sh", "source ~/src/beta/lib.sh\n")
    linked = tree.put("linked.sh", "source ~/src/roomkit/lib.sh\n")
    os.link(linked, tree.outside / "second-name.sh")
    sealed = tree.app / "sealed"
    tree.put("sealed/run.sh", "source ~/src/serotine/lib.sh\n")
    locked.chmod(0)
    sealed.chmod(0)
    try:
        code, out = run_edges(tree)
    finally:
        locked.chmod(0o600)
        sealed.chmod(0o700)
    assert code == 0, out
    assert "UNKNOWN" not in out, out
    lines = out.splitlines()
    assert any(line.startswith("WARN   could not read %s (" % locked) for line in lines), out
    assert any(line.startswith("WARN   could not read %s (" % sealed) for line in lines), out
    assert ("WARN   could not read %s (%s)" % (linked, scan.FILE_LINKS % (linked, 2))) in lines, out
    assert candidates(out) == [], out


def test_edge_warnings_are_not_counted_in_the_summary(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    tree.registry["projects"].append({"name": "ghost", "remote": REMOTE % "ghost",
                                      "paths": {HOST: str(tree.root / "ghost")},
                                      "subpath": None, "lifecycle": "active"})
    tree.write_registry()
    plain_code, plain = run_edges(tree, edges_on=False)
    code, out = run_edges(tree)
    assert candidates(out), out
    assert plain.splitlines()[-1] == out.splitlines()[-1] == "PASS  no drift (1 warn)", out


def test_edge_lines_come_after_the_drift_lines_and_before_the_summary(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    # A missing project gives the drift half a line of its own.
    tree.registry["projects"].append({"name": "ghost", "remote": REMOTE % "ghost",
                                      "paths": {HOST: str(tree.root / "ghost")},
                                      "subpath": None, "lifecycle": "active"})
    tree.write_registry()
    code, plain = run_edges(tree, edges_on=False)
    code, out = run_edges(tree)
    plain_lines = plain.splitlines()
    lines = out.splitlines()
    assert lines[:len(plain_lines) - 1] == plain_lines[:-1], out
    assert lines[-1] == plain_lines[-1], out
    assert lines[len(plain_lines) - 1:-1] == [
        "WARN   candidate app → beta (source) run.sh:1", EDGE_PRUNED % (0, 0)], out


def test_edge_pass_never_runs_when_the_scan_cannot_verify(tmp_path, monkeypatch):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    (tree.root / "loose-link").symlink_to(tree.outside)
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 2, out
    assert "UNKNOWN  " + scan.ROOT_SYMLINK % (tree.root / "loose-link") in out, out
    assert edge_lines(out) == [], out
    assert trace.opened(tree.app / "run.sh") == [], trace.opened(tree.app / "run.sh")


def test_edge_pass_waits_for_every_repo_to_be_identified(tmp_path, monkeypatch):
    # The root walk succeeds; a repo's config holds an include, so the drift
    # half ends in exit 2 at identify: no repo may have been listed for edges.
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    config = tree.root / "beta" / ".git" / "config"
    with open(config, "a", encoding="utf-8") as handle:
        handle.write("[include]\n\tpath = elsewhere\n")
    trace, (code, out) = traced(monkeypatch, lambda: run_edges(tree))
    assert code == 2, out
    assert "git config includes are not followed" in out, out
    assert edge_lines(out) == [], out
    # identify reads app's git config; nothing else in app is opened or listed.
    opened = [path for path in trace.opened(tree.app)
              if path != str(tree.app / ".git" / "config")]
    assert opened == [], opened


def test_unparseable_registry_with_edges_is_unknown(tmp_path):
    tree = EdgeTree(tmp_path)
    (tree.ws / "registry.yaml").write_text("projects: [unclosed\n", encoding="utf-8")
    code, out = run_edges(tree)
    assert code == 2, out
    assert "UNKNOWN  registry is not parseable YAML" in out, out


def test_edge_output_is_sorted_and_independent_of_hash_seed(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\nROOMKIT_BIN=1\n")
    tree.put("z.py", "import b_mod\nSEROTINE_URL=1\n")
    tree.put("a/pyproject.toml", "deps = [\"b-pkg\"]\n")
    outputs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-P", "-m", "workspace", "scan", str(tree.ws), "--edges"],
            capture_output=True, text=True, env=env, check=False)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1], outputs
    found = candidates(outputs[0])
    assert found == sorted(found) and len(found) == 5, outputs[0]


def test_cli_accepts_edges_and_refuses_a_near_miss(tmp_path):
    tree = EdgeTree(tmp_path)
    tree.put("run.sh", "source ~/src/beta/lib.sh\n")
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    ok = subprocess.run([sys.executable, "-P", "-m", "workspace", "scan", str(tree.ws), "--edges"],
                        capture_output=True, text=True, env=env, check=False)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "WARN   candidate app → beta (source) run.sh:1" in ok.stdout, ok.stdout
    for arg in ("--edgez", "--edges=yes", "--edge"):
        bad = subprocess.run([sys.executable, "-P", "-m", "workspace", "scan", str(tree.ws), arg],
                             capture_output=True, text=True, env=env, check=False)
        assert bad.returncode == 3, (arg, bad.stdout + bad.stderr)
        assert "workspace: unknown option %s" % arg in bad.stderr, bad.stderr
    twice = subprocess.run([sys.executable, "-P", "-m", "workspace", "scan", str(tree.ws),
                            "--edges", "--edges"],
                           capture_output=True, text=True, env=env, check=False)
    assert twice.returncode == 3 and "--edges given twice" in twice.stderr, twice.stderr


# --------------------------------------------------------------------------
# Plain scan is UT4's scan (E1)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, options", [("tree", {}), ("tree-beta", {"beta": True})])
def test_plain_scan_output_equals_the_main_golden(tmp_path, name, options):
    # The goldens were captured from main 9ddb44c on these same fixtures.
    base = Path(os.path.realpath(tmp_path))
    tree = Tree(base, **options)
    code, out = run_scan(tree)
    assert out.replace(str(base), "<BASE>") == (GOLDEN / (name + ".txt")).read_text(
        encoding="utf-8")


def test_plain_scan_opens_only_git_files_inside_repos_and_lists_none(tmp_path, monkeypatch):
    tree = Tree(tmp_path, beta=True)
    root = tree.root
    (root / "alpha" / "run.sh").write_text("source ~/src/legacy/lib.sh\n", encoding="utf-8")
    trace = Trace()
    code, out = trace.run(monkeypatch, tree)
    assert code == 1, out
    repos = [root / name for name in ("alpha", "alpha-wt", "beta", "beta-wt", "gamma", "legacy")]
    allowed = {str(root / name / ".git" / "config") for name in ("alpha", "beta", "gamma",
                                                                  "legacy")}
    for repo, worktree in (("alpha", "alpha-wt"), ("beta", "beta-wt")):
        allowed.add(str(root / worktree / ".git"))
        allowed.add(str(root / repo / ".git" / "worktrees" / worktree / "commondir"))
    inside = [(name, path) for name, path in trace.calls
              if any(path == str(repo) or path.startswith(str(repo) + os.sep) for repo in repos)]
    opened = [path for name, path in inside if name in Trace.OPENERS]
    assert set(opened) <= allowed, sorted(set(opened) - allowed)
    listed = [path for name, path in inside if name in ("listdir", "scandir")]
    assert listed == [], listed


def test_name_decision_comes_before_the_extension_allowlist():
    # `.example` is an allowlisted extension: `.env.example` is protected by its name only.
    assert edges.is_read(".env.example")
    assert ".env.example".startswith(edges.ENV_PREFIX)
