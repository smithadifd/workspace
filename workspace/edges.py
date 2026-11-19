"""Edge proposals: which registered projects name each other in their source files.

`scan --edges` runs this after the drift scan has its verdict, and `register`
runs it for the repository it drafts. Only files inside registered, present,
non-archived repositories are read (and, for `register`, the repository being
registered). Each folder is descended with the scan's own walk, one step per
name: every step is judged by its text and by its identity against every
excluded entry, and a symlink is never followed. A folder is listed through a
descriptor opened without following a symlink, whose identity must be the one
the walk recorded; a file is read only through the scan's one reader. A name
that starts with `.env` is skipped before any file call, and a folder holding
`.git` below the start is a nested repository and is not entered.

What cannot be read is a WARN line, never a could-not-verify: the drift
verdict stands. A symlink or an excluded entry is counted on one INFO line.
Evidence is a path and a line number, never the line's text. It is kept
raw, as the real path, and each sink encodes it for itself: `register`
stores it quoted for YAML with every character outside printable ASCII
escaped, and a path from inside a repository is printed, here and in
`register`'s plan, with every backslash and every character that is not
printable escaped, so a file name cannot start a line of its own or send
control bytes to the terminal.
"""
import os
import re
import stat

from workspace import scan

# The detector seed: the file names and extensions read, and the folders skipped.
SKIP_NAMES = frozenset((
    "node_modules", ".venv", "venv", "dist", "build", ".git", "__pycache__", ".next",
    ".worktrees",
))
CODE_EXTENSIONS = frozenset((
    ".py", ".sh", ".bash", ".zsh", ".js", ".ts", ".tsx", ".mjs", ".cjs", ".yml", ".yaml",
    ".toml", ".json", ".plist", ".cfg", ".ini", ".service", ".conf", ".example",
))
BUILD_FILES = frozenset((
    "pyproject.toml", "package.json", "requirements.txt", "requirements-dev.txt", "setup.py",
    "setup.cfg", "Cargo.toml", "go.mod",
))
SHELL_EXTENSIONS = frozenset((".sh", ".bash", ".zsh"))
ENV_PREFIX = ".env"
MAX_EDGE_FILE_BYTES = 400000
KINDS = ("build", "source", "runtime")

FOLDER_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)

SHELL_SOURCE = re.compile(r"^\s*(?:source|\.)\s+[\"']?([^\s\"']+)")
PY_SYS_PATH = re.compile(r"sys\.path\.(?:insert|append)\(")
PY_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")
CAPS_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")
URL_HOST = re.compile(r"\b(?:https?|wss?)://([A-Za-z0-9][A-Za-z0-9-]*)")
# Stored evidence is raw: the real path, then `:LINE`. A path may hold a
# newline, a tab or a line separator, but not a plain space; evidence with a
# space is prose, as it was when evidence was stored escaped.
EVIDENCE_FORM = re.compile(r"[^ ]+:[0-9]+")

CANDIDATE_LINE = "WARN   candidate %s → %s (%s) %s"
STALE_LINE = "WARN   stale %s → %s (%s) %s"
UNREAD_LINE = "WARN   could not read %s (%s)"
NOT_CHECKED_LINE = "INFO   edge %s → %s (%s) not checked: %s"
PROSE_LINE = "INFO   %d edges have prose evidence and are not checked for staleness"
PRUNED_LINE = "INFO   edges: %d symlinks not followed, %d excluded entries not entered"
SHORT_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def printable(text):
    """`text` with a backslash and every non-printable character escaped, as Python writes them."""
    shown = []
    for char in text:
        code = ord(char)
        if char in SHORT_ESCAPES:
            shown.append(SHORT_ESCAPES[char])
        elif char.isprintable():
            shown.append(char)
        elif code < 0x100:
            shown.append("\\x%02x" % code)
        elif code < 0x10000:
            shown.append("\\u%04x" % code)
        else:
            shown.append("\\U%08x" % code)
    return "".join(shown)


def name_token(name):
    return r"(?<![\w-])" + re.escape(name) + r"(?![\w-])"


class Target:
    """How a project name is recognised in a file."""

    def __init__(self, name):
        self.name = name
        variants = {name, name.replace("-", "_"), name.replace("_", "-")}
        self.token = re.compile("|".join(name_token(item) for item in sorted(variants)))
        self.module = name.replace("-", "_")
        self.caps = name.upper().replace("-", "_") + "_"
        self.hosts = {name.lower(), name.replace("_", "-").lower()}


def is_read(name):
    """Whether a file is read for edges, by its name alone."""
    return name in BUILD_FILES or os.path.splitext(name)[1] in CODE_EXTENSIONS


def detect(name, data, targets):
    """Yield (target name, kind, line number) for each signal in one file's bytes."""
    extension = os.path.splitext(name)[1]
    text = data.decode("utf-8", "replace")
    for number, line in enumerate(text.split("\n"), 1):
        if name in BUILD_FILES:
            for target in targets:
                if target.token.search(line):
                    yield target.name, "build", number
        if extension in SHELL_EXTENSIONS:
            match = SHELL_SOURCE.match(line)
            if match:
                sourced = match.group(1)
                base = os.path.basename(sourced)
                for target in targets:
                    if "/%s/" % target.name in sourced:
                        yield target.name, "source", number
                    elif base.startswith(target.name + "-") and base.endswith(".sh"):
                        yield target.name, "runtime", number
        if extension == ".py":
            if PY_SYS_PATH.search(line):
                for target in targets:
                    if target.token.search(line):
                        yield target.name, "source", number
            match = PY_IMPORT.match(line)
            if match:
                top = match.group(1).split(".")[0]
                for target in targets:
                    if top == target.module:
                        yield target.name, "source", number
        for identifier in CAPS_IDENTIFIER.findall(line):
            for target in targets:
                if identifier.startswith(target.caps):
                    yield target.name, "runtime", number
        for host in URL_HOST.findall(line):
            label = host.lower()
            for target in targets:
                if label in target.hosts:
                    yield target.name, "runtime", number


class EdgeWalk:
    """Walks repositories for edge signals; records what it could not read and what it pruned."""

    def __init__(self, scope):
        self.scope = scope
        self.unread = []
        self.links = 0
        self.excluded = 0

    def warn(self, path, reason):
        self.unread.append(UNREAD_LINE % (printable(path), printable(str(reason))))

    def list_folder(self, path, info):
        """The names in `path`, listed through a descriptor that is the folder the walk found."""
        try:
            handle = os.open(path, FOLDER_FLAGS)
        except OSError as exc:
            raise scan.Unverifiable("could not open it: %s" % exc.strerror)
        try:
            now = os.fstat(handle)
            if not stat.S_ISDIR(now.st_mode) or scan.node_of(now) != scan.node_of(info):
                raise scan.Unverifiable("it changed after it was walked")
            return sorted(os.listdir(handle))
        except OSError as exc:
            raise scan.Unverifiable("could not list it: %s" % exc.strerror)
        finally:
            os.close(handle)

    def files(self, start):
        """Yield (path relative to the start, bytes) for every file read below `start` hops."""
        base = start[-1][0]
        stack = [start]
        while stack:
            hops = stack.pop()
            folder, info = hops[-1]
            try:
                names = self.list_folder(folder, info)
            except scan.Unverifiable as exc:
                self.warn(folder, exc)
                continue
            if hops is not start and ".git" in names:
                continue
            folders = []
            for name in names:
                # Decided by the name alone, before any file call.
                if name.startswith(ENV_PREFIX) or name in SKIP_NAMES:
                    continue
                path = os.path.join(folder, name)
                try:
                    walked = scan.walk(self.scope, hops, name, guard_repos=False)
                except scan.Refused as refused:
                    if refused.reason == scan.HOP_LINK:
                        self.links += 1
                    elif refused.reason == scan.REFUSED_EXCLUDED:
                        self.excluded += 1
                    else:
                        self.warn(path, refused.words("it"))
                    continue
                except scan.Unverifiable as exc:
                    self.warn(path, exc)
                    continue
                found = walked[-1][1]
                if stat.S_ISDIR(found.st_mode):
                    folders.append(walked)
                    continue
                if not stat.S_ISREG(found.st_mode) or not is_read(name):
                    continue
                if found.st_size > MAX_EDGE_FILE_BYTES:
                    continue
                try:
                    data = scan.read_file(path, found)
                except scan.Unverifiable as exc:
                    self.warn(path, exc)
                    continue
                yield os.path.relpath(path, base), name, data
            stack.extend(reversed(folders))

    def findings(self, start, targets):
        """{(target, kind): sorted evidence} for the files below `start`."""
        found = {}
        for rel, name, data in self.files(start):
            for target, kind, number in detect(name, data, targets):
                found.setdefault((target, kind), set()).add((rel, number))
        return {key: sorted(value) for key, value in found.items()}


def evidence_text(evidence):
    """The first evidence as `path:LINE`, raw: the real path, for storage and for display."""
    rel, number = evidence[0]
    return "%s:%d" % (rel, number)


def edge_pairs(document, key):
    pairs = set()
    for entry in document.get(key) or []:
        pairs.add((entry.get("from"), entry.get("to")))
    return pairs


def project_sources(state):
    """{path: (hops, [non-archived project names])} for each present registered path."""
    gathered = state["gathered"]
    sources = {}
    for path in sorted(state["registered"]):
        if not gathered.present.get(path):
            continue
        names = [project["name"] for project in state["registered"][path]
                 if project["lifecycle"] != "archived"]
        if names:
            sources[path] = (gathered.hops[path], names)
    return sources


def all_findings(walker, sources, targets):
    """{(from, to, kind): sorted evidence} over every source path; self-edges dropped."""
    findings = {}
    for path in sorted(sources):
        hops, names = sources[path]
        per_path = walker.findings(hops, targets)
        for origin in names:
            for (target, kind), evidence in per_path.items():
                if target != origin:
                    findings[(origin, target, kind)] = evidence
    return findings


def candidate_edges(findings, document):
    """The detected (from, to, kind) with no approved and no rejected edge for the pair."""
    held = edge_pairs(document, "edges") | edge_pairs(document, "rejected_edges")
    return {key: value for key, value in findings.items() if (key[0], key[1]) not in held}


def scan_lines(state):
    """The edge pass of `scan --edges`: the lines it prints, after the drift lines."""
    document = state["document"]
    projects = state["projects"]
    walker = EdgeWalk(state["scope"])
    targets = [Target(name) for name in sorted({project["name"] for project in projects})]
    sources = project_sources(state)
    findings = all_findings(walker, sources, targets)

    walked = {name for _, names in sources.values() for name in names}
    lifecycle = {project["name"]: project["lifecycle"] for project in projects}
    candidates = []
    for (origin, target, kind), evidence in candidate_edges(findings, document).items():
        candidates.append(CANDIDATE_LINE % (origin, target, kind,
                                            printable(evidence_text(evidence))))

    stale = []
    not_checked = []
    prose = 0
    detected = {(origin, target) for origin, target, _ in findings}
    for entry in document.get("edges") or []:
        origin, target, kind = entry.get("from"), entry.get("to"), entry.get("kind")
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not EVIDENCE_FORM.fullmatch(evidence):
            prose += 1
            continue
        if origin not in walked:
            if origin not in lifecycle:
                reason = "`%s` is not a registered project" % origin
            elif lifecycle[origin] == "archived":
                reason = "`%s` is archived" % origin
            else:
                reason = "`%s` is not on this host" % origin
            not_checked.append(NOT_CHECKED_LINE % (origin, target, kind, reason))
            continue
        if (origin, target) not in detected:
            stale.append(STALE_LINE % (origin, target, kind, printable(evidence)))

    lines = sorted(candidates) + sorted(stale) + sorted(walker.unread) + sorted(not_checked)
    if prose:
        lines.append(PROSE_LINE % prose)
    lines.append(PRUNED_LINE % (walker.links, walker.excluded))
    return lines
