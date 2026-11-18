"""Read-only drift scan: the repos under this host's code root against the registry.

Reads the workspace's `registry.yaml`, lists the host's code root one level
deep, and reports:

- DRIFT  a repo the registry does not name and has not declined;
- DRIFT  a registered repo missing on this host whose `required_on` names it;
- WARN   any other registered repo missing on this host;
- KNOWN  an archived repo present on disk (known, not drift).

A `.git` file marks a worktree; it folds into its parent through
`git rev-parse --git-common-dir`. Nothing inside a repo is listed, an excluded
path is refused before any filesystem call reaches it, and a symlink is followed
only while every hop stays inside the code root, outside every excluded path and
outside every repo (registered, or found directly under the root). Every check is lexical first: a path is judged by
its text before it is touched.

Writes nothing. No network.

Exit codes:
  0  conforms (WARN lines may be present)
  1  drift
  2  could not verify; the reason is always printed
"""
import fnmatch
import os
import re
import socket
import stat
import subprocess
import sys

EX_CONFORMS = 0
EX_DRIFT = 1
EX_UNKNOWN = 2

REGISTRY_NAME = "registry.yaml"
LIFECYCLES = frozenset(("active", "maintenance", "deprecated", "sunset", "archived"))
MAX_LINK_HOPS = 40
GIT_TIMEOUT_SECONDS = 30

# The registry grammar (ADR 0036 decision 4, flat keys). A key outside these
# sets is a could-not-verify, never silently skipped.
TOP_KEYS = frozenset((
    "system", "harness_root", "code_roots", "excluded", "clusters",
    "projects", "edges", "declined", "rejected_edges",
))
PROJECT_KEYS = frozenset((
    "name", "remote", "paths", "subpath", "one_liner", "owner",
    "owned_or_reference", "tier", "cluster", "lifecycle", "doc",
    "confused_with", "required_on",
))
CLUSTER_KEYS = frozenset(("id", "name", "owner", "lifecycle", "one_liner"))
EXCLUDED_KEYS = frozenset(("path", "reason"))
EDGE_KEYS = frozenset(("from", "to", "kind", "evidence", "approved_at"))
DECLINED_KEYS = frozenset(("remote", "date", "reason"))
REJECTED_EDGE_KEYS = frozenset(("from", "to", "date", "reason"))

URL_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/]+)(/.*)?$")
SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")

REFUSED_EXCLUDED = "excluded path, not read"
REFUSED_OUTSIDE = "resolves outside the code root"
REFUSED_IN_REPO = "inside repo"


class Unverifiable(Exception):
    """The scan cannot reach a verdict; the message is the printed reason."""


# --------------------------------------------------------------------------
# Paths: lexical judgements only, no filesystem calls
# --------------------------------------------------------------------------

def within(path, base):
    if base == os.sep:
        return path.startswith(os.sep)
    return path == base or path.startswith(base + os.sep)


def ancestors(path):
    current = path
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def expand_path(text, what):
    if text.startswith("~"):
        expanded = os.path.expanduser(text)
        if expanded.startswith("~"):
            raise Unverifiable("%s `%s` cannot be expanded: no home folder" % (what, text))
    else:
        expanded = text
    if not os.path.isabs(expanded):
        raise Unverifiable("%s `%s` is not an absolute path" % (what, text))
    return os.path.normpath(expanded)


def exclusion_pattern(text):
    stripped = text[:-3] if text.endswith("/**") else (text.rstrip("/") or "/")
    return expand_path(stripped, "excluded path")


def is_excluded(path, patterns):
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in patterns
        for candidate in ancestors(path)
    )


def normalize_remote(url):
    text = url.strip()
    match = URL_REMOTE.match(text)
    if match:
        host, path = match.group(1).lower(), match.group(2) or ""
    else:
        match = SCP_REMOTE.match(text)
        if not match:
            host, path = "", text
        else:
            host, path = match.group(1).lower(), "/" + match.group(2).lstrip("/")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host + path


class Scope:
    """The code root, the excluded patterns and the repo folders not to enter."""

    def __init__(self, root, exclusions, repo_paths):
        self.root = root
        self.exclusions = exclusions
        self.repo_paths = repo_paths

    def refusal(self, path, guard_repos=True):
        """Why `path` must not be touched, or None. Lexical only."""
        if is_excluded(path, self.exclusions):
            return REFUSED_EXCLUDED
        if not within(path, self.root):
            return REFUSED_OUTSIDE
        if guard_repos:
            for repo in self.repo_paths:
                if path != repo and within(path, repo):
                    return "%s %s" % (REFUSED_IN_REPO, repo)
        return None


def relative_parts(root, path):
    relative = os.path.relpath(path, root)
    if relative == os.curdir:
        return []
    return relative.split(os.sep)


def resolve(scope, path):
    """Follow `path` hop by hop from the code root.

    Returns (resolved, lstat result, refusal). Every hop is judged lexically
    by the scope before it is lstat'd, so no call reaches an excluded path,
    a path outside the root or a path inside a repo. An absent
    path returns (None, None, None).
    """
    pending = relative_parts(scope.root, path)
    current = scope.root
    last = None
    hops = 0
    while pending:
        candidate = os.path.join(current, pending.pop(0))
        refusal = scope.refusal(candidate)
        if refusal:
            return None, None, refusal
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return None, None, None
        except OSError as exc:
            raise Unverifiable("could not inspect %s: %s" % (candidate, exc))
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > MAX_LINK_HOPS:
                return None, None, "symlink loop"
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise Unverifiable("could not read symlink %s: %s" % (candidate, exc))
            spliced = os.path.normpath(os.path.join(current, target, *pending))
            refusal = scope.refusal(spliced)
            if refusal:
                return None, None, refusal
            pending = relative_parts(scope.root, spliced)
            current = scope.root
            last = None
            continue
        current = candidate
        last = info
    return current, last, None


# --------------------------------------------------------------------------
# Git: the only subprocess, read-only, with the caller's GIT_* stripped
# --------------------------------------------------------------------------

def git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(args, where):
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise Unverifiable("git is not available")
    except subprocess.TimeoutExpired:
        raise Unverifiable("git timed out on %s" % where)
    except OSError as exc:
        raise Unverifiable("git could not run on %s: %s" % (where, exc))


def origin_remote(args, where):
    result = run_git([*args, "config", "--get", "remote.origin.url"], where)
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode == 1:
        return None
    raise Unverifiable("git error on %s: %s" % (where, result.stderr.strip()))


def common_dir(folder):
    result = run_git(["-C", folder, "rev-parse", "--git-common-dir"], folder)
    if result.returncode != 0:
        raise Unverifiable("git error on %s: %s" % (folder, result.stderr.strip()))
    value = result.stdout.strip()
    if not value:
        raise Unverifiable("git printed no common dir for %s" % folder)
    return os.path.normpath(os.path.join(folder, value))


def git_marker(folder):
    """'repo' for a .git folder, 'worktree' for a .git file, None for neither."""
    marker = os.path.join(folder, ".git")
    try:
        info = os.lstat(marker)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unverifiable("could not inspect %s: %s" % (marker, exc))
    if stat.S_ISDIR(info.st_mode):
        return "repo"
    if stat.S_ISREG(info.st_mode):
        return "worktree"
    raise Unverifiable("%s is neither a folder nor a file" % marker)


def worktree_gitdir(scope, folder):
    marker = os.path.join(folder, ".git")
    try:
        with open(marker, encoding="utf-8") as handle:
            first = handle.readline().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise Unverifiable("could not read %s: %s" % (marker, exc))
    if not first.startswith("gitdir:"):
        raise Unverifiable("%s does not name a gitdir" % marker)
    gitdir = os.path.normpath(os.path.join(folder, first[len("gitdir:"):].strip()))
    refusal = scope.refusal(gitdir, guard_repos=False)
    if refusal:
        raise Unverifiable(
            "worktree %s keeps its git dir at %s, which %s; not read"
            % (folder, gitdir, "is excluded" if refusal == REFUSED_EXCLUDED else "is outside the code root")
        )
    return gitdir


def identify(scope, folder, kind):
    """Return (parent repo path, remote) for a repo or a worktree."""
    if kind == "repo":
        return folder, origin_remote(["--git-dir", os.path.join(folder, ".git")], folder)
    worktree_gitdir(scope, folder)
    common = common_dir(folder)
    refusal = scope.refusal(common, guard_repos=False)
    if refusal:
        raise Unverifiable("worktree %s has its common dir at %s, which %s" % (folder, common, refusal))
    parent = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return parent, origin_remote(["-C", folder], folder)


# --------------------------------------------------------------------------
# The registry grammar
# --------------------------------------------------------------------------

def require_entry(entry, allowed, where):
    if not isinstance(entry, dict):
        raise Unverifiable("%s is not a mapping" % where)
    extra = sorted(str(key) for key in entry if key not in allowed)
    if extra:
        raise Unverifiable("%s has unknown field(s): %s" % (where, ", ".join(extra)))


def require_list(document, key, required):
    value = document.get(key)
    if value is None:
        if required:
            raise Unverifiable("registry `%s` is missing or not a list" % key)
        return []
    if not isinstance(value, list):
        raise Unverifiable("registry `%s` is missing or not a list" % key)
    return value


def require_text(value, where):
    if not isinstance(value, str) or not value:
        raise Unverifiable("%s must be a non-empty string" % where)
    return value


def optional_text(value, where):
    if value is not None and not isinstance(value, str):
        raise Unverifiable("%s must be a string or null" % where)
    return value


def validate(document):
    """Check the registry against its grammar; return the parts scan uses."""
    if not isinstance(document, dict):
        raise Unverifiable("registry root is not a mapping")
    require_entry(document, TOP_KEYS, "registry")
    optional_text(document.get("system"), "registry `system`")
    optional_text(document.get("harness_root"), "registry `harness_root`")

    code_roots = document.get("code_roots")
    if not isinstance(code_roots, dict) or not code_roots:
        raise Unverifiable("registry `code_roots` is missing or not a non-empty mapping")
    for host, root in code_roots.items():
        require_text(host, "a `code_roots` host")
        require_text(root, "code root for host `%s`" % host)

    excluded = []
    for index, entry in enumerate(require_list(document, "excluded", True)):
        where = "excluded entry %d" % (index + 1)
        require_entry(entry, EXCLUDED_KEYS, where)
        excluded.append(require_text(entry.get("path"), where + " `path`"))
        require_text(entry.get("reason"), where + " `reason`")

    for index, entry in enumerate(require_list(document, "clusters", False)):
        require_entry(entry, CLUSTER_KEYS, "cluster entry %d" % (index + 1))
    for index, entry in enumerate(require_list(document, "edges", False)):
        require_entry(entry, EDGE_KEYS, "edge entry %d" % (index + 1))
    for index, entry in enumerate(require_list(document, "rejected_edges", False)):
        require_entry(entry, REJECTED_EDGE_KEYS, "rejected edge entry %d" % (index + 1))

    declined = []
    for index, entry in enumerate(require_list(document, "declined", False)):
        where = "declined entry %d" % (index + 1)
        require_entry(entry, DECLINED_KEYS, where)
        declined.append(require_text(entry.get("remote"), where + " `remote`"))

    projects = []
    for index, entry in enumerate(require_list(document, "projects", True)):
        where = "project entry %d" % (index + 1)
        require_entry(entry, PROJECT_KEYS, where)
        name = require_text(entry.get("name"), where + " `name`")
        where = "project `%s`" % name
        lifecycle = entry.get("lifecycle")
        if lifecycle not in LIFECYCLES:
            raise Unverifiable("%s has lifecycle `%s`, not one of %s"
                               % (where, lifecycle, ", ".join(sorted(LIFECYCLES))))
        remote = optional_text(entry.get("remote"), where + " `remote`")
        optional_text(entry.get("subpath"), where + " `subpath`")
        paths = entry.get("paths")
        if paths is None:
            paths = {}
        if not isinstance(paths, dict):
            raise Unverifiable("%s `paths` must be a mapping of host to path" % where)
        for host, path in paths.items():
            require_text(host, where + " `paths` host")
            require_text(path, "%s path for host `%s`" % (where, host))
        required_on = entry.get("required_on")
        if required_on is None:
            required_on = []
        if not isinstance(required_on, list) or not all(
            isinstance(host, str) and host for host in required_on
        ):
            raise Unverifiable("%s `required_on` must be a list of host names" % where)
        projects.append({
            "name": name,
            "lifecycle": lifecycle,
            "remote": normalize_remote(remote) if remote else None,
            "paths": paths,
            "required_on": required_on,
        })
    return code_roots, excluded, declined, projects


def load_registry(workspace):
    from workspace.check import (
        DuplicateKeyError,
        UnhashableKeyError,
        load_registry_yaml,
        yaml,
    )

    if not os.path.isdir(workspace):
        raise Unverifiable("workspace is missing or not a folder: %s" % workspace)
    path = os.path.join(workspace, REGISTRY_NAME)
    if not os.path.isfile(path):
        raise Unverifiable("registry is missing or not a file: %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise Unverifiable("could not read registry: %s" % exc)
    if not text.strip():
        raise Unverifiable("registry is empty")
    try:
        return load_registry_yaml(text)
    except DuplicateKeyError as exc:
        raise Unverifiable("registry repeats the key %r" % (exc.dup_key,))
    except UnhashableKeyError:
        raise Unverifiable("registry has an unhashable mapping key")
    except yaml.YAMLError as exc:
        raise Unverifiable("registry is not parseable YAML: %s" % exc)


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------

def declined_match(patterns, remote, path):
    for pattern in patterns:
        if pattern.startswith(("/", "~")):
            if fnmatch.fnmatchcase(path, expand_path(pattern, "declined path")):
                return True
        elif remote is not None and fnmatch.fnmatchcase(remote, normalize_remote(pattern)):
            return True
    return False


def scan(workspace, host):
    lines = []
    drift = 0
    warn = 0

    code_roots, excluded, declined, projects = validate(load_registry(workspace))
    if host is None:
        host = socket.gethostname().split(".")[0]
    if host not in code_roots:
        raise Unverifiable(
            "host `%s` is not in code_roots (known: %s); pass --host"
            % (host, ", ".join(sorted(str(name) for name in code_roots)))
        )
    root = expand_path(code_roots[host], "code root")
    exclusions = [exclusion_pattern(path) for path in excluded]
    if is_excluded(root, exclusions):
        raise Unverifiable("code root %s is inside an excluded path" % root)

    registered = {}
    for project in projects:
        text = project["paths"].get(host)
        if text is None:
            continue
        path = expand_path(text, "project `%s` path" % project["name"])
        if is_excluded(path, exclusions):
            raise Unverifiable("project `%s` path %s is excluded" % (project["name"], path))
        if not within(path, root) or path == root:
            raise Unverifiable(
                "project `%s` path %s is outside the code root %s; not checked"
                % (project["name"], path, root)
            )
        project["path"] = path
        registered.setdefault(path, []).append(project)
    for pattern in declined:
        if pattern.startswith(("/", "~")):
            expand_path(pattern, "declined path")

    try:
        info = os.stat(root)
    except OSError as exc:
        raise Unverifiable("code root %s cannot be read: %s" % (root, exc))
    if not stat.S_ISDIR(info.st_mode):
        raise Unverifiable("code root %s is not a folder" % root)
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise Unverifiable("code root %s cannot be listed: %s" % (root, exc))

    # Pass 1: the folders directly under the root. Symlinks wait for pass 2,
    # so every repo here is known before any link is followed.
    scope = Scope(root, exclusions, sorted(registered))
    candidates = []
    links = []
    for name in names:
        entry = os.path.join(root, name)
        refusal = scope.refusal(entry)
        if refusal:
            lines.append("SKIP   %s: %s" % (entry, refusal))
            continue
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Unverifiable("could not inspect %s: %s" % (entry, exc))
        if stat.S_ISLNK(info.st_mode):
            links.append(entry)
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        kind = git_marker(entry)
        if kind is not None:
            candidates.append((entry, entry, kind))

    # Pass 2: a link is followed only while it stays clear of every repo,
    # registered or found in pass 1.
    scope.repo_paths = sorted(set(registered) | {folder for _, folder, _ in candidates})
    for entry in links:
        resolved, info, refusal = resolve(scope, entry)
        if refusal:
            lines.append("SKIP   %s: symlink %s" % (entry, refusal))
            continue
        if resolved is None or not stat.S_ISDIR(info.st_mode):
            continue
        kind = git_marker(resolved)
        if kind is not None:
            candidates.append((entry, resolved, kind))

    found = {}
    for entry, folder, kind in candidates:
        parent, remote = identify(scope, folder, kind)
        if kind == "worktree":
            lines.append("INFO   worktree %s folds into %s" % (entry, parent))
        record = found.setdefault(parent, {"remote": None, "seen": []})
        record["seen"].append(entry)
        if remote and record["remote"] is None:
            record["remote"] = normalize_remote(remote)

    remotes = {project["remote"] for project in projects if project["remote"]}
    for parent in sorted(found):
        record = found[parent]
        remote = record["remote"]
        if parent in registered or (remote is not None and remote in remotes):
            continue
        shown = remote if remote is not None else "no remote"
        if declined_match(declined, remote, parent):
            lines.append("INFO   declined repo at %s (%s)" % (parent, shown))
            continue
        drift += 1
        lines.append("DRIFT  unregistered repo at %s (%s)" % (parent, shown))

    for path in sorted(registered):
        resolved, info, refusal = resolve(scope, path)
        if refusal:
            raise Unverifiable("registered path %s: %s" % (path, refusal))
        present = (
            resolved is not None
            and stat.S_ISDIR(info.st_mode)
            and git_marker(resolved) is not None
        )
        for project in registered[path]:
            if present:
                if project["lifecycle"] == "archived":
                    lines.append("KNOWN  archived project `%s` is present at %s"
                                 % (project["name"], path))
                continue
            if host in project["required_on"]:
                drift += 1
                lines.append("DRIFT  project `%s` is missing on host `%s`, which its required_on names: %s"
                             % (project["name"], host, path))
            else:
                warn += 1
                lines.append("WARN   project `%s` is missing on host `%s`: %s"
                             % (project["name"], host, path))
    return host, root, lines, drift, warn


def main(workspace, host=None, out=None):
    out = sys.stdout if out is None else out
    out.write("== workspace scan ==\n")
    try:
        host, root, lines, drift, warn = scan(workspace, host)
    except Unverifiable as exc:
        out.write("UNKNOWN  %s\n" % exc)
        return EX_UNKNOWN
    except Exception as exc:  # fail closed: never let a crash read as drift
        out.write("UNKNOWN  scan failed: %s: %s\n" % (type(exc).__name__, exc))
        return EX_UNKNOWN
    out.write("host `%s`, code root %s\n" % (host, root))
    for line in lines:
        out.write(line + "\n")
    if drift:
        out.write("== result: %d drift, %d warn ==\n" % (drift, warn))
        return EX_DRIFT
    out.write("PASS  no drift (%d warn)\n" % warn)
    return EX_CONFORMS
