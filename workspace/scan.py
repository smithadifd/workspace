"""Read-only drift scan: the repos under this host's code root against the registry.

Reads the workspace's `registry.yaml`, lists the host's code root one level
deep, and reports:

- DRIFT  a repo the registry does not name and has not declined;
- DRIFT  a registered repo missing on this host whose `required_on` names it;
- WARN   any other registered repo missing on this host;
- KNOWN  an archived repo present on disk (known, not drift).

Without a host, the registry's only `code_roots` host is used; with several it
is a could-not-verify. The machine's host name is never consulted.

A `.git` file marks a worktree; it folds into its parent, the common dir its
git files name. Nothing inside a repo is listed. An excluded path, written bare
or as `folder/*` or `folder/**`, covers the folder and everything beneath it.

Every path is walked hop by hop from the code root, as the kernel would walk
it: each hop is judged by its text, then lstat'd and judged by its node
identity, so an excluded folder is recognised under any spelling that reaches
it (another letter case on a case-insensitive disk, a symlinked parent). A
parent step pops a verified hop and never rises above the code root. A symlink
in the code root is followed only while every hop stays inside the code root,
outside every excluded path and outside every repo (registered, or found
directly under the root); a symlink or a missing hop on the way to a repo's git
files is refused.

scan reads a repo's git files itself: the `.git` file, `commondir`, `config`
and `config.worktree`, each opened without following a symlink and read only
when it is the regular file the walk verified. git never runs on a repo: its
one call reads `remote.origin.url` from config bytes on its standard input,
with includes, the system and global config switched off, from the filesystem
root.

Writes nothing. No network.

Exit codes:
  0  conforms (WARN lines may be present)
  1  drift
  2  could not verify; the reason is always printed
"""
import fnmatch
import os
import re
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
READ_CHUNK = 65536

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
GLOB_MAGIC = re.compile(r"[*?[]")

# The one git call: the remote from config bytes on standard input. No repo
# path is ever an argument, and includes are never followed.
GIT_REMOTE_ARGS = ("config", "--file", "-", "--no-includes", "--get", "remote.origin.url")

# A git file is opened without following a symlink and without blocking on a
# FIFO; it is read only after fstat shows the regular file the walk lstat'd.
READ_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_CLOEXEC", 0)
)

REFUSED_EXCLUDED = "excluded path, not read"
REFUSED_OUTSIDE = "resolves outside the code root"
REFUSED_IN_REPO = "inside repo"
REFUSED_LOOP = "symlink loop"

# Why a walk stopped at a hop that is not judged by the scope.
HOP_LINK = "link"
HOP_MISSING = "missing"
HOP_NOT_FOLDER = "not a folder"


class Unverifiable(Exception):
    """The scan cannot reach a verdict; the message is the printed reason."""


class Refused(Exception):
    """A walk stopped at `hop`: a scope refusal, or a link, missing or non-folder hop."""

    def __init__(self, reason, hop):
        Exception.__init__(self, reason)
        self.reason = reason
        self.hop = hop

    def words(self, what):
        if self.reason == HOP_LINK:
            return "%s, which passes through the symlink %s; not followed" % (what, self.hop)
        if self.reason == HOP_MISSING:
            return "%s, which does not exist (no %s); not read" % (what, self.hop)
        if self.reason == HOP_NOT_FOLDER:
            return "%s, which passes through %s, not a folder; not read" % (what, self.hop)
        return "%s, which %s; not read" % (what, refusal_words(self.reason))


# --------------------------------------------------------------------------
# Paths: lexical judgements, no filesystem calls
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
    """An excluded path as the folder it covers, with everything beneath it.

    The bare folder and its glob forms `folder/*` and `folder/**` all mean the
    folder itself and everything under it: `is_excluded` matches a path whose
    ancestor matches the pattern.
    """
    for suffix in ("/**", "/*"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return expand_path(text.rstrip("/") or "/", "excluded path")


def is_excluded(path, patterns):
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in patterns
        for candidate in ancestors(path)
    )


def split_text(text):
    """The components of a path text, as written: no normalising, `..` kept."""
    return [part for part in text.split(os.sep) if part not in ("", os.curdir)]


def shown(base, text):
    """A path text as git would join it to `base`, for messages; not normalised."""
    return text if text.startswith(os.sep) else os.path.join(base, text)


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


# --------------------------------------------------------------------------
# Identity: the excluded entries as nodes on disk
# --------------------------------------------------------------------------

def node_of(info):
    return (info.st_dev, info.st_ino)


def excluded_nodes(patterns):
    """The node identity of each excluded entry that exists.

    Each entry itself is lstat'd, never anything beneath it, so a hop that
    reaches it under another spelling is still recognised. A glob pattern
    names no single entry and is matched by its text only.
    """
    nodes = set()
    for pattern in patterns:
        if GLOB_MAGIC.search(pattern):
            continue
        try:
            info = os.lstat(pattern)
        except OSError:
            continue
        nodes.add(node_of(info))
    return nodes


def is_excluded_node(info, nodes):
    return node_of(info) in nodes


def root_is_excluded_node(root, nodes):
    """True when the code root, or a folder above it, is an excluded entry by identity."""
    for path in ancestors(root):
        for call in (os.lstat, os.stat):
            try:
                info = call(path)
            except OSError:
                continue
            if is_excluded_node(info, nodes):
                return True
    return False


class Scope:
    """The code root, the excluded patterns and nodes, and the repo folders not to enter."""

    def __init__(self, root, root_info, exclusions, nodes, repo_paths):
        self.root = root
        self.root_info = root_info
        self.root_parts = split_text(root)
        self.exclusions = exclusions
        self.nodes = nodes
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

    def excluded_node(self, info):
        return is_excluded_node(info, self.nodes)


def refusal_words(refusal):
    if refusal == REFUSED_EXCLUDED:
        return "is excluded"
    if refusal == REFUSED_OUTSIDE:
        return "is outside the code root"
    return "is %s" % refusal


def require_clear(scope, path, what):
    """Refuse `path`, by its text alone, unless it is inside the root and not excluded."""
    refusal = scope.refusal(path, guard_repos=False)
    if refusal:
        raise Unverifiable("%s, which %s; not read" % (what, refusal_words(refusal)))


# --------------------------------------------------------------------------
# The walk: every path, hop by hop from the code root
# --------------------------------------------------------------------------

def below_root(scope, parts, text):
    """The components of an absolute text below the code root's own components."""
    size = len(scope.root_parts)
    if parts[:size] != scope.root_parts:
        raise Refused(REFUSED_OUTSIDE, text)
    return parts[size:]


def walk(scope, start, text, links=False, guard_repos=False, missing_ok=False):
    """Follow `text` from the folder `start` names, hop by hop, as the kernel would.

    `start` is a list of (path, lstat) hops below the code root, each already
    verified; an empty list is the root itself. `text` is read as written, left
    to right and never normalised: an absolute text must begin with the code
    root's own components; a relative one starts at `start`. Each hop is judged
    by its text, then lstat'd and judged by its node identity against every
    excluded entry. A parent step pops the last verified hop and never rises
    above the code root. A symlink hop is refused, or with `links` its target
    text is spliced in front of what is left. A missing hop, or a hop that is
    not a folder with more to walk, is refused, or with `missing_ok` gives None.
    Returns the hops `text` names; raises Refused.
    """
    hops = list(start)
    pending = split_text(text)
    if text.startswith(os.sep):
        pending = below_root(scope, pending, text)
        hops = []
    followed = 0
    while pending:
        name = pending.pop(0)
        if name == os.pardir:
            if not hops:
                raise Refused(REFUSED_OUTSIDE, text)
            hops.pop()
            continue
        parent = hops[-1][0] if hops else scope.root
        candidate = os.path.join(parent, name)
        refusal = scope.refusal(candidate, guard_repos)
        if refusal:
            raise Refused(refusal, candidate)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise Refused(HOP_MISSING, candidate)
        except OSError as exc:
            raise Unverifiable("could not inspect %s: %s" % (candidate, exc))
        if scope.excluded_node(info):
            raise Refused(REFUSED_EXCLUDED, candidate)
        if stat.S_ISLNK(info.st_mode):
            if not links:
                raise Refused(HOP_LINK, candidate)
            followed += 1
            if followed > MAX_LINK_HOPS:
                raise Refused(REFUSED_LOOP, candidate)
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise Unverifiable("could not read symlink %s: %s" % (candidate, exc))
            spliced = split_text(target)
            if target.startswith(os.sep):
                spliced = below_root(scope, spliced, target)
                hops = []
            pending = spliced + pending
            continue
        if pending and not stat.S_ISDIR(info.st_mode):
            if missing_ok:
                return None
            raise Refused(HOP_NOT_FOLDER, candidate)
        hops.append((candidate, info))
    return hops


def resolve(scope, path):
    """Follow a root entry or a registered path, symlinks included.

    Returns (hops, refusal): hops is None when the path is absent, and an
    empty list when it is the code root itself.
    """
    try:
        return walk(scope, [], path, links=True, guard_repos=True, missing_ok=True), None
    except Refused as refused:
        return None, refused.reason


def walk_git(scope, start, text, what, missing_ok=False):
    """Walk a path to a repo's git files: no symlink and no missing hop."""
    try:
        return walk(scope, start, text, missing_ok=missing_ok)
    except Refused as refused:
        raise Unverifiable(refused.words(what))


def hop_path(scope, hops):
    return hops[-1][0] if hops else scope.root


def require_folder(scope, hops, what):
    info = hops[-1][1] if hops else scope.root_info
    if not stat.S_ISDIR(info.st_mode):
        raise Unverifiable("%s, which is not a folder; not read" % what)


# --------------------------------------------------------------------------
# Reading git files, and the one git call
# --------------------------------------------------------------------------

def read_file(path, info):
    """The bytes of `path`, the regular file the walk lstat'd as `info`."""
    try:
        handle = os.open(path, READ_FLAGS)
    except OSError as exc:
        raise Unverifiable("could not read %s: %s" % (path, exc))
    try:
        found = os.fstat(handle)
        if not stat.S_ISREG(found.st_mode):
            raise Unverifiable("%s is not a regular file; not read" % path)
        if node_of(found) != node_of(info):
            raise Unverifiable("%s changed while it was read; not read" % path)
        chunks = []
        while True:
            chunk = os.read(handle, READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise Unverifiable("could not read %s: %s" % (path, exc))
    finally:
        os.close(handle)


def git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def origin_remote(data, folder, path):
    """remote.origin.url from config bytes, or None; git sees only the bytes."""
    try:
        result = subprocess.run(
            ["git", *GIT_REMOTE_ARGS],
            input=data,
            capture_output=True,
            env=git_env(),
            cwd=os.sep,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise Unverifiable("git is not available")
    except subprocess.TimeoutExpired:
        raise Unverifiable("git timed out on %s (%s)" % (folder, path))
    except OSError as exc:
        raise Unverifiable("git could not run on %s (%s): %s" % (folder, path, exc))
    if result.returncode == 0:
        return result.stdout.decode("utf-8", "replace").strip() or None
    if result.returncode == 1:
        return None
    raise Unverifiable("git error on %s (%s): %s"
                       % (folder, path, result.stderr.decode("utf-8", "replace").strip()))


def git_marker(scope, folder):
    """('repo', lstat) for a .git folder, ('worktree', lstat) for a .git file, or (None, None)."""
    marker = os.path.join(folder, ".git")
    require_clear(scope, marker, "git marker %s" % marker)
    try:
        info = os.lstat(marker)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise Unverifiable("could not inspect %s: %s" % (marker, exc))
    if scope.excluded_node(info):
        raise Unverifiable("git marker %s, which is excluded; not read" % marker)
    if stat.S_ISDIR(info.st_mode):
        return "repo", info
    if stat.S_ISREG(info.st_mode):
        return "worktree", info
    raise Unverifiable("%s is neither a folder nor a file" % marker)


def gitdir_text(marker, data):
    first = os.fsdecode(data.split(b"\n", 1)[0]).strip()
    if not first.startswith("gitdir:") or not first[len("gitdir:"):].strip():
        raise Unverifiable("%s does not name a gitdir" % marker)
    return first[len("gitdir:"):].strip()


def identify(scope, hops, kind, marker_info):
    """Return (parent repo path, remote) for the repo or worktree `hops` names.

    Every git file is walked to and judged before any is read, and every read
    comes before git runs on the config bytes.
    """
    folder = hop_path(scope, hops)
    label = "%s %s" % (kind, folder)
    marker = os.path.join(folder, ".git")
    if kind == "repo":
        gitdir_hops = hops + [(marker, marker_info)]
    else:
        text = gitdir_text(marker, read_file(marker, marker_info))
        what = "worktree %s keeps its git dir at %s" % (folder, shown(folder, text))
        gitdir_hops = walk_git(scope, hops, text, what)
        require_folder(scope, gitdir_hops, what)
    gitdir = hop_path(scope, gitdir_hops)

    pointer = os.path.join(gitdir, "commondir")
    pointer_hops = walk_git(scope, gitdir_hops, "commondir",
                            "%s keeps its commondir file at %s" % (label, pointer),
                            missing_ok=True)
    if pointer_hops is None:
        common_hops = gitdir_hops
    else:
        text = os.fsdecode(read_file(pointer, pointer_hops[-1][1])).rstrip("\r\n")
        if not text:
            raise Unverifiable("%s names no common dir" % pointer)
        what = "%s has its common dir at %s" % (label, shown(gitdir, text))
        common_hops = walk_git(scope, gitdir_hops, text, what)
        require_folder(scope, common_hops, what)
    common = hop_path(scope, common_hops)

    configs = []
    for start, base, name, words in (
        (common_hops, common, "config", "git config"),
        (gitdir_hops, gitdir, "config.worktree", "worktree config"),
    ):
        path = os.path.join(base, name)
        found = walk_git(scope, start, name, "%s keeps its %s at %s" % (label, words, path),
                         missing_ok=True)
        if found is not None:
            configs.append((path, found[-1][1]))
    contents = [(path, read_file(path, info)) for path, info in configs]

    # config.worktree is read after config, so its value wins, as in git.
    remote = None
    for path, data in contents:
        value = origin_remote(data, folder, path)
        if value is not None:
            remote = value
    if kind == "repo":
        return folder, remote
    parent = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return parent, remote


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
        if len(code_roots) != 1:
            raise Unverifiable(
                "no --host given and code_roots names more than one host (%s); pass --host"
                % ", ".join(sorted(str(name) for name in code_roots))
            )
        host = next(iter(code_roots))
    if host not in code_roots:
        raise Unverifiable(
            "host `%s` is not in code_roots (known: %s); pass --host"
            % (host, ", ".join(sorted(str(name) for name in code_roots)))
        )
    root = expand_path(code_roots[host], "code root")
    exclusions = [exclusion_pattern(path) for path in excluded]
    if is_excluded(root, exclusions):
        raise Unverifiable("code root %s is inside an excluded path" % root)
    nodes = excluded_nodes(exclusions)

    registered = {}
    for project in projects:
        text = project["paths"].get(host)
        if text is None:
            if host in project["required_on"]:
                drift += 1
                lines.append("DRIFT  project `%s` is required on host `%s` but has no path for it"
                             % (project["name"], host))
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
    if root_is_excluded_node(root, nodes):
        raise Unverifiable("code root %s is inside an excluded path" % root)
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise Unverifiable("code root %s cannot be listed: %s" % (root, exc))

    # Pass 1: the folders directly under the root. Symlinks wait for pass 2,
    # so every repo here is known before any link is followed.
    scope = Scope(root, info, exclusions, nodes, sorted(registered))
    candidates = []
    links = []
    for name in names:
        entry = os.path.join(root, name)
        refusal = scope.refusal(entry)
        if refusal:
            lines.append("SKIP   %s: %s" % (entry, refusal))
            continue
        try:
            entry_info = os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Unverifiable("could not inspect %s: %s" % (entry, exc))
        if scope.excluded_node(entry_info):
            lines.append("SKIP   %s: %s" % (entry, REFUSED_EXCLUDED))
            continue
        if stat.S_ISLNK(entry_info.st_mode):
            links.append(entry)
            continue
        if not stat.S_ISDIR(entry_info.st_mode):
            continue
        kind, marker_info = git_marker(scope, entry)
        if kind is not None:
            candidates.append((entry, [(entry, entry_info)], kind, marker_info))

    # Pass 2: a link is followed only while it stays clear of every repo,
    # registered or found in pass 1.
    scope.repo_paths = sorted(set(registered) | {hops[-1][0] for _, hops, _, _ in candidates})
    for entry in links:
        hops, refusal = resolve(scope, entry)
        if refusal:
            lines.append("SKIP   %s: symlink %s" % (entry, refusal))
            continue
        if not hops or not stat.S_ISDIR(hops[-1][1].st_mode):
            continue
        kind, marker_info = git_marker(scope, hops[-1][0])
        if kind is not None:
            candidates.append((entry, hops, kind, marker_info))

    found = {}
    for entry, hops, kind, marker_info in candidates:
        parent, remote = identify(scope, hops, kind, marker_info)
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
        shown_remote = remote if remote is not None else "no remote"
        if declined_match(declined, remote, parent):
            lines.append("INFO   declined repo at %s (%s)" % (parent, shown_remote))
            continue
        drift += 1
        lines.append("DRIFT  unregistered repo at %s (%s)" % (parent, shown_remote))

    for path in sorted(registered):
        hops, refusal = resolve(scope, path)
        if refusal:
            raise Unverifiable("registered path %s: %s" % (path, refusal))
        present = (
            bool(hops)
            and stat.S_ISDIR(hops[-1][1].st_mode)
            and git_marker(scope, hops[-1][0])[0] is not None
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
