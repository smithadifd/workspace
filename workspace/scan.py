"""Read-only drift scan: the repos under this host's code root against the registry.

Reads the workspace's `registry.yaml`, lists the host's code root one level
deep, and reports:

- DRIFT  a repo the registry does not name and has not declined;
- DRIFT  a registered repo missing on this host whose `required_on` names it;
- WARN   any other registered repo missing on this host;
- KNOWN  an archived repo present on disk (known, not drift).

Without a host, the registry's only `code_roots` host is used; with several it
is a could-not-verify. The machine's host name is never consulted.

scan gives a verdict only for repos in one plain shape it can read safely, and
is a could-not-verify for anything else. It does not model any other git or
filesystem layout:

- a code root entry is a real folder holding a `.git` folder (a repo) or a
  `.git` regular file (a worktree); an excluded entry is skipped, any other
  symlink in the code root is a could-not-verify and is never followed;
- a registered path is walked from the code root with no symlink, and a
  present one passes the same shape check as a code root entry;
- a repo's `.git` folder holds no `commondir`;
- a worktree's `.git` file holds the one line `gitdir: <path>`, naming
  `<repo>/.git/worktrees/<name>` for a repo found directly in the code root or
  registered, with a `commondir` file naming `<repo>/.git` and a `HEAD` file;
  it folds into that repo;
- the repo's one `config` is a regular file with one link and at most 1 MiB,
  has no include section, does not turn on per-worktree config, and holds at
  most one non-empty `remote.origin.url`.

Every path is walked hop by hop from the code root, as the kernel would walk
it: each hop is judged by its text, then lstat'd and judged by its node
identity, so an excluded folder is recognised under any spelling that reaches
it (another letter case on a case-insensitive disk, a symlinked parent). An
exclusion's text matches as a literal path as well as a glob, so a real name
that holds a glob character is covered, and the entry its literal text names is
lstat'd for its identity whatever characters it holds. A
parent step pops a verified hop and never rises above the code root. A symlink
or a missing hop is refused.

scan reads a repo's git files itself: the `.git` file, `commondir` and the
common `config`, each through one reader that opens it without following a
symlink and reads it only when it is the regular file the walk verified, with
one link and at most 1 MiB, and refuses it when it changed during the read.
git never runs on a repo: it only reads config bytes on its standard input,
with includes, the system and global config switched off, from the filesystem
root. It first lists any include key in those bytes (a config with an include
section is a could-not-verify; the file it names is never followed), then
reads `extensions.worktreeConfig` (a repo that uses per-worktree config is a
could-not-verify; `config.worktree` is never read), then reads every
`remote.origin.url` value.

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
GIT_TIMEOUT_SECONDS = 30
READ_CHUNK = 65536
MAX_FILE_BYTES = 1024 * 1024

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

# The remote from config bytes on standard input: every value, each ended by a
# NUL, so a value that holds an escaped newline still counts as one. No repo
# path is ever an argument, and includes are never followed.
GIT_REMOTE_ARGS = ("config", "--file", "-", "--no-includes", "--null", "--get-all",
                   "remote.origin.url")
# Before that, git's own parser lists any include key in the same bytes; an
# include section is refused, never followed.
GIT_INCLUDE_ARGS = ("config", "--file", "-", "--no-includes", "--name-only",
                    "--get-regexp", r"^(include|includeif)\.")
# Then whether the repo uses per-worktree config: scan does not judge it.
GIT_WORKTREE_CONFIG_ARGS = ("config", "--file", "-", "--no-includes", "--type=bool",
                            "--get", "extensions.worktreeConfig")

# A git file is opened without following a symlink and without blocking on a
# FIFO; it is read only after fstat shows the regular file the walk lstat'd.
READ_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_CLOEXEC", 0)
)

REFUSED_EXCLUDED = "excluded path, not read"
REFUSED_OUTSIDE = "resolves outside the code root"
REFUSED_IN_REPO = "inside repo"
NOT_WORKTREE = ("is not a worktree git folder: <common>/worktrees/<name> with commondir "
                "and HEAD files; not read")
WORKTREE_CONFIG = ("%s: per-worktree config is not supported (%s sets "
                   "extensions.worktreeConfig true); could not verify")
ROOT_SYMLINK = ("%s is a symlink in the code root; not followed "
                "(list it under excluded to skip it)")
REGISTERED_SYMLINK = "registered path %s: passes through the symlink %s; not followed"
REPO_COMMONDIR = "repo %s holds %s, which only a worktree's git folder has; not read"
GITFILE_LINES = "%s holds more than its gitdir line; not read"
FILE_LINKS = "%s has %d hard links; not read"
FILE_TOO_BIG = "%s is larger than %d bytes; not read"
FILE_CHANGED = "%s changed while it was read; not read"
REMOTE_EMPTY = "%s: remote.origin.url is empty in %s; could not verify"
REMOTE_MANY = "%s: remote.origin.url is set %d times in %s; could not verify"
GIT_ODD_OUTPUT = "git gave unexpected output on %s (%s); could not verify"

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
    """True when `path` is an excluded pattern or beneath one, by text only.

    A pattern matches as its literal text (the path itself, or a folder above
    it), so a real name that holds a glob character such as `[priv]` is
    covered; and as a glob, against the path and every folder above it.
    """
    return any(
        within(path, pattern)
        or any(fnmatch.fnmatchcase(candidate, pattern) for candidate in ancestors(path))
        for pattern in patterns
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
    reaches it under another spelling is still recognised. The literal text of
    every pattern is lstat'd whatever characters it holds, so a real name with
    a glob character is recognised too; any other entry a glob matches is
    matched by its text only.
    """
    nodes = set()
    for pattern in patterns:
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
# The walk: every path, hop by hop from the code root, never through a symlink
# --------------------------------------------------------------------------

def below_root(scope, parts, text):
    """The components of an absolute text below the code root's own components."""
    size = len(scope.root_parts)
    if parts[:size] != scope.root_parts:
        raise Refused(REFUSED_OUTSIDE, text)
    return parts[size:]


def walk(scope, start, text, guard_repos=False, missing_ok=False):
    """Follow `text` from the folder `start` names, hop by hop, as the kernel would.

    `start` is a list of (path, lstat) hops below the code root, each already
    verified; an empty list is the root itself. `text` is read as written, left
    to right and never normalised: an absolute text must begin with the code
    root's own components; a relative one starts at `start`. Each hop is judged
    by its text, then lstat'd and judged by its node identity against every
    excluded entry. A parent step pops the last verified hop and never rises
    above the code root. A symlink hop is refused. A missing hop, or a hop that
    is not a folder with more to walk, is refused, or with `missing_ok` gives
    None. Returns the hops `text` names; raises Refused.
    """
    hops = list(start)
    pending = split_text(text)
    if text.startswith(os.sep):
        pending = below_root(scope, pending, text)
        hops = []
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
            raise Refused(HOP_LINK, candidate)
        if pending and not stat.S_ISDIR(info.st_mode):
            if missing_ok:
                return None
            raise Refused(HOP_NOT_FOLDER, candidate)
        hops.append((candidate, info))
    return hops


def walk_registered(scope, path):
    """Walk a registered path from the code root: no symlink, nothing inside another repo.

    Returns its hops, or None when it is absent.
    """
    try:
        return walk(scope, [], path, guard_repos=True, missing_ok=True)
    except Refused as refused:
        if refused.reason == HOP_LINK:
            raise Unverifiable(REGISTERED_SYMLINK % (path, refused.hop))
        raise Unverifiable("registered path %s: %s" % (path, refused.reason))


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

def file_state(info):
    """What must not change while a file is read."""
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def read_file(path, info):
    """The bytes of `path`, the regular file the walk lstat'd as `info`.

    The only reader of a git file. Nothing is read unless fstat shows that
    regular file, with one link and at most MAX_FILE_BYTES; the file is refused
    when a second fstat after the read differs, or the bytes read are not its
    size. No refusal quotes the bytes.
    """
    try:
        handle = os.open(path, READ_FLAGS)
    except OSError as exc:
        raise Unverifiable("could not read %s: %s" % (path, exc))
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode):
            raise Unverifiable("%s is not a regular file; not read" % path)
        if node_of(before) != node_of(info):
            raise Unverifiable(FILE_CHANGED % path)
        if before.st_nlink != 1:
            raise Unverifiable(FILE_LINKS % (path, before.st_nlink))
        if before.st_size > MAX_FILE_BYTES:
            raise Unverifiable(FILE_TOO_BIG % (path, MAX_FILE_BYTES))
        chunks = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(handle, min(READ_CHUNK, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise Unverifiable(FILE_TOO_BIG % (path, MAX_FILE_BYTES))
        after = os.fstat(handle)
        if file_state(after) != file_state(before):
            raise Unverifiable(FILE_CHANGED % path)
        if total != before.st_size:
            raise Unverifiable(FILE_CHANGED % path)
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


def run_git(args, data, folder, path):
    """git on config bytes on stdin, from the filesystem root; never a repo path."""
    try:
        return subprocess.run(
            ["git", *args],
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


def git_failure(result, folder, path):
    return Unverifiable("git error on %s (%s): %s"
                        % (folder, path, result.stderr.decode("utf-8", "replace").strip()))


def refuse_includes(data, folder, path):
    """Refuse config bytes that hold an include section, as git's parser reads them."""
    result = run_git(GIT_INCLUDE_ARGS, data, folder, path)
    if result.returncode == 1 and not result.stdout.strip():
        return
    if result.returncode in (0, 1):
        raise Unverifiable("%s: git config includes are not followed (%s has one); not read"
                           % (folder, path))
    raise git_failure(result, folder, path)


def refuse_worktree_config(data, folder, path):
    """Refuse config bytes that turn on per-worktree config, as git's parser reads them."""
    result = run_git(GIT_WORKTREE_CONFIG_ARGS, data, folder, path)
    if result.returncode == 1 and not result.stdout.strip():
        return
    if result.returncode == 0:
        value = result.stdout.strip()
        if value == b"false":
            return
        if value == b"true":
            raise Unverifiable(WORKTREE_CONFIG % (folder, path))
    raise git_failure(result, folder, path)


def origin_remote(data, folder, path):
    """remote.origin.url from config bytes, or None; git sees only the bytes.

    Absent is None; exactly one non-empty value is the remote; an empty value,
    more than one value, or any other git result is a could-not-verify.
    """
    result = run_git(GIT_REMOTE_ARGS, data, folder, path)
    if result.returncode == 1:
        if result.stdout:
            raise Unverifiable(GIT_ODD_OUTPUT % (folder, path))
        return None
    if result.returncode != 0:
        raise git_failure(result, folder, path)
    if not result.stdout.endswith(b"\0"):
        raise Unverifiable(GIT_ODD_OUTPUT % (folder, path))
    values = result.stdout[:-1].split(b"\0")
    if len(values) != 1:
        raise Unverifiable(REMOTE_MANY % (folder, len(values), path))
    value = values[0].decode("utf-8", "replace").strip()
    if not value:
        raise Unverifiable(REMOTE_EMPTY % (folder, path))
    return value


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
    """The path a `.git` file names: its one non-empty line, `gitdir: <path>`."""
    lines = data.split(b"\n")
    first = os.fsdecode(lines[0]).strip()
    if not first.startswith("gitdir:") or not first[len("gitdir:"):].strip():
        raise Unverifiable("%s does not name a gitdir" % marker)
    if any(line not in (b"", b"\r") for line in lines[1:]):
        raise Unverifiable(GITFILE_LINES % marker)
    return first[len("gitdir:"):].strip()


def require_known_repo_worktrees(known, what, gitdir_hops):
    """A worktree's git dir is `<repo>/.git/worktrees/<name>`, `<repo>` a known repo.

    `known` maps each repo folder found directly in the code root or
    registered to its node identity. This is judged on the verified hops,
    before anything in the git dir is read.
    """
    if (len(gitdir_hops) < 4
            or os.path.basename(gitdir_hops[-2][0]) != "worktrees"
            or os.path.basename(gitdir_hops[-3][0]) != ".git"
            or known.get(gitdir_hops[-4][0]) != node_of(gitdir_hops[-4][1])):
        raise Unverifiable("%s, which %s" % (what, NOT_WORKTREE))


def require_worktree_gitdir(scope, label, what, gitdir_hops, pointer_hops, common_hops):
    """A worktree's git dir is `<common>/worktrees/<name>`, with commondir and HEAD files.

    The commondir file was walked to and is read by the caller; HEAD is walked
    to and lstat'd. A git dir anywhere else, such as another repo's checkout
    folder or its .git folder, is refused.
    """
    refused = Unverifiable("%s, which %s" % (what, NOT_WORKTREE))
    if pointer_hops is None:
        raise refused
    if (len(gitdir_hops) != len(common_hops) + 2
            or hop_path(scope, gitdir_hops[:-2]) != hop_path(scope, common_hops)
            or os.path.basename(gitdir_hops[-2][0]) != "worktrees"):
        raise refused
    head = os.path.join(hop_path(scope, gitdir_hops), "HEAD")
    head_hops = walk_git(scope, gitdir_hops, "HEAD",
                         "%s keeps its HEAD file at %s" % (label, head), missing_ok=True)
    if head_hops is None or not stat.S_ISREG(head_hops[-1][1].st_mode):
        raise refused


def identify(scope, hops, kind, marker_info, known):
    """Return (parent repo path, remote) for the repo or worktree `hops` names.

    Every git file is walked to and judged before any is read, and every read
    comes before git runs on the config bytes.
    """
    folder = hop_path(scope, hops)
    label = "%s %s" % (kind, folder)
    marker = os.path.join(folder, ".git")
    if kind == "repo":
        gitdir_hops = hops + [(marker, marker_info)]
        # Only a worktree's git folder has a commondir; a repo's never does.
        pointer = os.path.join(marker, "commondir")
        if walk_git(scope, gitdir_hops, "commondir",
                    "%s keeps its commondir file at %s" % (label, pointer),
                    missing_ok=True) is not None:
            raise Unverifiable(REPO_COMMONDIR % (folder, pointer))
        common_hops = gitdir_hops
    else:
        text = gitdir_text(marker, read_file(marker, marker_info))
        gitdir_what = "worktree %s keeps its git dir at %s" % (folder, shown(folder, text))
        gitdir_hops = walk_git(scope, hops, text, gitdir_what)
        require_folder(scope, gitdir_hops, gitdir_what)
        require_known_repo_worktrees(known, gitdir_what, gitdir_hops)
        gitdir = hop_path(scope, gitdir_hops)

        pointer = os.path.join(gitdir, "commondir")
        pointer_hops = walk_git(scope, gitdir_hops, "commondir",
                                "%s keeps its commondir file at %s" % (label, pointer),
                                missing_ok=True)
        if pointer_hops is None:
            common_hops = None
        else:
            text = os.fsdecode(read_file(pointer, pointer_hops[-1][1])).rstrip("\r\n")
            if not text:
                raise Unverifiable("%s names no common dir" % pointer)
            what = "%s has its common dir at %s" % (label, shown(gitdir, text))
            common_hops = walk_git(scope, gitdir_hops, text, what)
            require_folder(scope, common_hops, what)
        require_worktree_gitdir(scope, label, gitdir_what, gitdir_hops, pointer_hops, common_hops)
    common = hop_path(scope, common_hops)

    # Only the common config is read; config.worktree never is. A repo that
    # turns on per-worktree config is refused instead. A missing config is
    # refused, never read as "no remote".
    config = os.path.join(common, "config")
    found = walk_git(scope, common_hops, "config",
                     "%s keeps its git config at %s" % (label, config))
    data = read_file(config, found[-1][1])
    refuse_includes(data, folder, config)
    refuse_worktree_config(data, folder, config)
    remote = origin_remote(data, folder, config)
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


class Gathered:
    """What `gather` found: the repos to identify, which registered paths are present, the known repos."""

    def __init__(self, candidates, present, known, hops):
        self.candidates = candidates
        self.present = present
        self.known = known
        self.hops = hops


def root_scope(root, exclusions, nodes, registered):
    """The code root, verified as a folder that is not an excluded entry, as a Scope."""
    try:
        info = os.stat(root)
    except OSError as exc:
        raise Unverifiable("code root %s cannot be read: %s" % (root, exc))
    if not stat.S_ISDIR(info.st_mode):
        raise Unverifiable("code root %s is not a folder" % root)
    if root_is_excluded_node(root, nodes):
        raise Unverifiable("code root %s is inside an excluded path" % root)
    return Scope(root, info, exclusions, nodes, sorted(registered))


def gather(scope, registered, lines):
    """List the code root one level deep and walk every registered path.

    The one root walker: `scan`, the edge pass and `register` all call it.
    Returns the repos and worktrees to identify, with their verified hops,
    whether each registered path is present, and the known repos (each repo
    folder found directly in the code root or registered, by node identity).
    Leaves `scope.repo_paths` holding every registered path and every repo in
    the code root.
    """
    root = scope.root
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise Unverifiable("code root %s cannot be listed: %s" % (root, exc))

    # The code root's entries: an excluded entry is skipped, any other symlink
    # is a could-not-verify, and a folder is looked inside for its .git marker.
    candidates = []
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
            raise Unverifiable(ROOT_SYMLINK % entry)
        if not stat.S_ISDIR(entry_info.st_mode):
            continue
        kind, marker_info = git_marker(scope, entry)
        if kind is not None:
            candidates.append((entry, [(entry, entry_info)], kind, marker_info))

    # The registered paths, walked with no symlink and clear of every repo,
    # registered or found in the code root. A present one that is not also a
    # code root entry is judged by the same shape check.
    scope.repo_paths = sorted(set(registered) | {entry for entry, _, _, _ in candidates})
    in_root = {entry for entry, _, _, _ in candidates}
    present = {}
    walked = {}
    for path in sorted(registered):
        hops = walk_registered(scope, path)
        kind = None
        if hops and stat.S_ISDIR(hops[-1][1].st_mode):
            kind, marker_info = git_marker(scope, hops[-1][0])
        present[path] = kind is not None
        if kind is not None:
            walked[path] = hops
        if kind is not None and path not in in_root:
            candidates.append((path, hops, kind, marker_info))

    # Every repo a worktree may belong to is known before any is identified,
    # so the order of the entries never matters.
    known = {hops[-1][0]: node_of(hops[-1][1])
             for _, hops, kind, _ in candidates if kind == "repo"}
    return Gathered(candidates, present, known, walked)


def select_host(code_roots, host):
    """The host `scan` reads: the one `code_roots` host, or `host`, which must be a key."""
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
    return host


def prelude(code_roots, excluded, declined, projects, host):
    """The code root, the exclusions and the registered paths for `host`, as `scan` judges them.

    Returns (root, exclusions, nodes, registered, unplaced): `registered` maps
    each registered path to its projects (each project gains its `path`);
    `unplaced` lists the projects whose `required_on` names `host` but which
    record no path for it. Anything `scan` cannot verify raises Unverifiable.
    """
    root = expand_path(code_roots[host], "code root")
    exclusions = [exclusion_pattern(path) for path in excluded]
    if is_excluded(root, exclusions):
        raise Unverifiable("code root %s is inside an excluded path" % root)
    nodes = excluded_nodes(exclusions)

    registered = {}
    unplaced = []
    for project in projects:
        text = project["paths"].get(host)
        if text is None:
            if host in project["required_on"]:
                unplaced.append(project)
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
    return root, exclusions, nodes, registered, unplaced


def scan(workspace, host, state=None):
    """The drift scan. With `state`, a dict, it also records what it walked for the edge pass."""
    lines = []
    drift = 0
    warn = 0

    document = load_registry(workspace)
    code_roots, excluded, declined, projects = validate(document)
    host = select_host(code_roots, host)
    root, exclusions, nodes, registered, unplaced = prelude(
        code_roots, excluded, declined, projects, host)
    for project in unplaced:
        drift += 1
        lines.append("DRIFT  project `%s` is required on host `%s` but has no path for it"
                     % (project["name"], host))

    scope = root_scope(root, exclusions, nodes, registered)
    gathered = gather(scope, registered, lines)
    present = gathered.present
    candidates = gathered.candidates
    known = gathered.known
    if state is not None:
        state.update(document=document, host=host, root=root, scope=scope,
                     projects=projects, registered=registered, gathered=gathered)

    found = {}
    for entry, hops, kind, marker_info in candidates:
        parent, remote = identify(scope, hops, kind, marker_info, known)
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
        for project in registered[path]:
            if present[path]:
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


def main(workspace, host=None, out=None, edges=False):
    out = sys.stdout if out is None else out
    out.write("== workspace scan ==\n")
    state = {} if edges else None
    try:
        host, root, lines, drift, warn = scan(workspace, host, state)
    except Unverifiable as exc:
        out.write("UNKNOWN  %s\n" % exc)
        return EX_UNKNOWN
    except Exception as exc:  # fail closed: never let a crash read as drift
        out.write("UNKNOWN  scan failed: %s: %s\n" % (type(exc).__name__, exc))
        return EX_UNKNOWN
    edge_lines = []
    if edges:
        # The edge pass runs only once the drift half has its verdict. It
        # reports unreadable files as WARN lines itself; anything else that
        # escapes it is a could-not-verify, never a pass.
        from workspace import edges as edge_pass
        try:
            edge_lines = edge_pass.scan_lines(state)
        except Exception as exc:  # fail closed
            out.write("UNKNOWN  edge pass failed: %s: %s\n" % (type(exc).__name__, exc))
            return EX_UNKNOWN
    out.write("host `%s`, code root %s\n" % (host, root))
    for line in lines + edge_lines:
        out.write(line + "\n")
    if drift:
        out.write("== result: %d drift, %d warn ==\n" % (drift, warn))
        return EX_DRIFT
    out.write("PASS  no drift (%d warn)\n" % warn)
    return EX_CONFORMS
