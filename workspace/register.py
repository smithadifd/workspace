"""register and decline: add to `registry.yaml`, on a confirm, keeping every other byte.

`register WORKSPACE PATH` drafts a project entry for the repository at PATH,
plus edges the edge pass finds between it and the registered repositories.
PATH is judged by the scan's own walk before anything under it is touched:
an excluded step (by text or identity), a step outside the code root, inside
a registered repository or through a symlink, or a missing step is refused.
The known repositories come from the scan's own root walker, PATH must lie
inside none of them and hold no registered path, and the scan's own reader
identifies the repository and its remote. So a project `register` writes
never makes the next `scan` fail on its path.

`decline WORKSPACE PATTERN` records a remote or path pattern under `declined`.

Both print the change to `registry.yaml` and a plan hash, and write nothing;
with `--write --expect HASH` they write exactly that change. They splice text
into the file (comments, order and formatting stay) and refuse a splice that
does not reparse to the old registry plus exactly the new entries. Only
`registry.yaml` is written; run `workspace gen` afterwards.

`registry.yaml` keeps gen's write-path rule (see `workspace.gen`): it is read
once, through one descriptor opened by the workspace folder's descriptor
without following a symlink, and must be a regular file, or the verb refuses
with exit 1. The confirmed write re-opens it the same way, locks it, checks
that the name is still that file and still holds the planned bytes, and the
new file takes the locked file's mode. Drafted edge evidence is stored as the
real path and line, quoted for YAML with every character outside printable
ASCII escaped, so it parses back to that path; the plan's terminal output
escapes every line through `edges.printable`.

Exit codes: 0 planned or written, 1 refused, 2 could not verify.
"""
import copy
import datetime
import fcntl
import os
import re
import stat
import sys

from workspace import check, edges, gen, scan

EX_DONE = 0
EX_REFUSED = 1
EX_UNKNOWN = 2

DEFAULT_INDENT = "  "
TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")
EMPTY_FLOW = re.compile(r"^\s*\[\s*\]\s*(#.*)?$")
EMPTY_VALUE = re.compile(r"^\s*(#.*)?$")
ITEM_LINE = re.compile(r"^(\s*)- ")
URL_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^@/]*@")
PLAN_CHANGED = gen.PLAN_CHANGED
KIND_ORDER = {kind: index for index, kind in enumerate(edges.KINDS)}


class Refusal(gen.Refusal):
    pass


def quoted(value):
    """A YAML double-quoted scalar holding only printable ASCII.

    A double quote and a backslash are escaped, and so is every character
    outside printable ASCII: as `\\uXXXX`, or `\\UXXXXXXXX` above U+FFFF (YAML
    takes both, and JSON's surrogate pairs would parse back as two characters).
    So a character YAML refuses raw (U+007F, the C1 controls) or reads as a
    line break (U+0085, U+2028, U+2029) parses back to itself.
    """
    shown = []
    for char in value:
        code = ord(char)
        if char in ('"', "\\"):
            shown.append("\\" + char)
        elif 0x20 <= code < 0x7F:
            shown.append(char)
        elif code <= 0xFFFF:
            shown.append("\\u%04x" % code)
        else:
            shown.append("\\U%08x" % code)
    return '"%s"' % "".join(shown)


def scalar(value):
    if value is None:
        return "null"
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (quoted(key), scalar(item))
                                  for key, item in value.items())
    return quoted(value)


def render_item(entry, indent):
    lines = []
    for index, (key, value) in enumerate(entry.items()):
        lead = indent + "- " if index == 0 else indent + "  "
        lines.append("%s%s: %s\n" % (lead, key, scalar(value)))
    return "".join(lines)


def splice(text, key, entries):
    """`text` with `entries` added at the end of the top-level list `key`, every other byte kept."""
    if not entries:
        return text
    lines = newline_lines(text)
    start = None
    for index, line in enumerate(lines):
        match = TOP_KEY.match(line.rstrip("\r\n"))
        if match and match.group(1) == key:
            start = index
            break
    if start is None:
        body = text if not text or text.endswith("\n") else text + "\n"
        rendered = "".join(render_item(entry, DEFAULT_INDENT) for entry in entries)
        return body + "\n%s:\n%s" % (key, rendered)
    rest = TOP_KEY.match(lines[start].rstrip("\r\n")).group(2)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line[:1] not in ("", " ", "\t", "#", "\n", "\r", "-"):
            end = index
            break
    last = None
    indent = None
    for index in range(start + 1, end):
        bare = lines[index].strip()
        if not bare or bare.startswith("#"):
            continue
        last = index
        if indent is None:
            match = ITEM_LINE.match(lines[index])
            if match:
                indent = match.group(1)
    if last is None:
        if EMPTY_FLOW.match(rest):
            head = "%s:%s" % (key, line_ending(lines[start]))
        elif EMPTY_VALUE.match(rest):
            head = lines[start] if lines[start].endswith("\n") else lines[start] + "\n"
        else:
            raise Refusal("registry `%s` is written in flow form; write it as a block list "
                          "first" % key)
        rendered = "".join(render_item(entry, DEFAULT_INDENT) for entry in entries)
        return "".join(lines[:start]) + head + rendered + "".join(lines[start + 1:])
    if indent is None:
        indent = DEFAULT_INDENT
    rendered = "".join(render_item(entry, indent) for entry in entries)
    before = "".join(lines[:last + 1])
    if not before.endswith("\n"):
        before += "\n"
    return before + rendered + "".join(lines[last + 1:])


def newline_lines(text):
    """`text` cut after each newline character (U+000A) only, each piece keeping it.

    Never `splitlines()`: it also cuts at U+0085, U+2028 and others, which
    can sit inside a quoted value on one registry line.
    """
    pieces = text.split("\n")
    last = pieces.pop()
    lines = [piece + "\n" for piece in pieces]
    if last:
        lines.append(last)
    return lines


def line_ending(line):
    return "\r\n" if line.endswith("\r\n") else "\n"


def parse(text):
    try:
        return check.load_registry_yaml(text)
    except (check.yaml.YAMLError, ValueError) as exc:
        raise Refusal("the spliced registry does not reparse: %s; nothing written" % exc)


def verified_splice(text, document, additions):
    """Splice every addition; refuse unless it reparses as the old registry plus exactly them."""
    new_text = text
    expected = copy.deepcopy(document)
    for key, entries in additions:
        new_text = splice(new_text, key, entries)
        current = expected.get(key)
        expected[key] = (list(current) if isinstance(current, list) else []) + [
            copy.deepcopy(entry) for entry in entries]
    reparsed = parse(new_text)
    try:
        scan.validate(copy.deepcopy(reparsed))
    except scan.Unverifiable as exc:
        raise Refusal("the spliced registry fails the grammar: %s; nothing written" % exc)
    if reparsed != expected:
        raise Refusal("the spliced registry is not the old registry plus the new entries; "
                      "nothing written")
    return new_text


def read_registry(workspace):
    """The registry's one snapshot, and the document parsed from those same bytes."""
    try:
        snapshot = scan.registry_snapshot(workspace)
    except scan.NotRegular:
        raise
    except scan.Unverifiable as exc:
        if str(exc).startswith("could not read registry"):
            raise
        raise scan.Unverifiable("could not read registry: %s" % exc)
    return snapshot, scan.parse_registry(snapshot.data)


def read_handle(handle):
    chunks = []
    while True:
        chunk = os.read(handle, scan.READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def write_registry(workspace, data, new_text):
    """Replace registry.yaml atomically, only while it still holds the bytes the plan read.

    The comparison and the replacement run under an exclusive lock on the
    registry file, so two confirmed writes never both pass the comparison: the
    one that waits then finds the file replaced, and refuses. The file is
    opened by the workspace folder's descriptor without following a symlink
    and must be a regular file; under the lock, its name, looked up without
    following a symlink, must still be that file, checked before any temp file
    is made. The new file takes the mode of the locked file, and its rename
    rechecks the name's bytes as well (`gen.require_unchanged`).
    """
    try:
        folder = gen.open_folder(workspace, "")
    except OSError as exc:
        raise scan.Unverifiable("could not read registry: %s" % exc)
    try:
        handle, _ = scan.open_regular(folder, scan.REGISTRY_NAME, scan.REGISTRY_NAME)
        if handle is None:
            raise scan.Unverifiable("could not read registry: %s is missing"
                                    % scan.REGISTRY_NAME)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            locked = os.fstat(handle)
            now = scan.entry_identity(folder, scan.REGISTRY_NAME)
            if now is None or not now[2] or now[0] != scan.node_of(locked):
                raise Refusal(PLAN_CHANGED)
            if read_handle(handle) != data:
                raise Refusal(PLAN_CHANGED)
            try:
                gen.write_atomic(folder, scan.REGISTRY_NAME, new_text.encode("utf-8"),
                                 (scan.node_of(locked), stat.S_IMODE(locked.st_mode), data))
            except gen.TargetChanged:
                raise Refusal(PLAN_CHANGED)
        finally:
            os.close(handle)
    finally:
        os.close(folder)


def shown(text):
    """`text` for the terminal: each line, cut on the newline character only, through
    `edges.printable`, as `scan --edges` prints a path from inside a repository."""
    pieces = text.split("\n")
    if pieces[-1] == "":
        pieces.pop()
    return "".join(edges.printable(piece) + "\n" for piece in pieces)


def emit_plan(out, data, new_text, digest, notes):
    for note in notes:
        out.write(note + "\n")
    out.write(shown(gen.unified(data.decode("utf-8"), new_text, scan.REGISTRY_NAME)))
    out.write("plan sha256=%s\n" % digest)


def finish(out, workspace, request, data, new_text, digest, notes):
    if not request["write"]:
        emit_plan(out, data, new_text, digest, notes)
        out.write("nothing written; to write this plan, rerun with --write --expect %s\n"
                  % digest)
        return EX_DONE
    if request["expect"] != digest:
        out.write("REFUSED  %s\n" % PLAN_CHANGED)
        return EX_REFUSED
    write_registry(workspace, data, new_text)
    out.write("wrote  %s\n" % scan.REGISTRY_NAME)
    out.write("run: workspace gen %s\n" % workspace)
    return EX_DONE


def run(verb, plan, request, out):
    out = sys.stdout if out is None else out
    out.write("== workspace %s ==\n" % verb)
    try:
        return plan(request, out)
    except scan.NotRegular as exc:
        out.write("REFUSED  %s\n" % exc)
        return EX_REFUSED
    except scan.Unverifiable as exc:
        out.write("UNKNOWN  %s\n" % exc)
        return EX_UNKNOWN
    except gen.Refusal as refusal:
        for reason in refusal.reasons:
            out.write("REFUSED  %s\n" % reason)
        return EX_REFUSED
    except Exception as exc:  # fail closed
        out.write("UNKNOWN  %s failed: %s: %s\n" % (verb, type(exc).__name__, exc))
        return EX_UNKNOWN


def today():
    return datetime.date.today()


# --------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------

def tilde_form(path):
    home = os.path.expanduser("~")
    if home not in ("~", os.sep) and scan.within(path, os.path.normpath(home)):
        rest = os.path.relpath(path, os.path.normpath(home))
        return "~" if rest == os.curdir else "~/" + rest
    return path


def stored_remote(remote):
    """The remote as written to the registry: a URL keeps no user name or password."""
    return URL_USERINFO.sub(r"\1", remote.strip())


def edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for row, a in enumerate(left, 1):
        current = [row]
        for column, b in enumerate(right, 1):
            current.append(min(previous[column] + 1, current[column - 1] + 1,
                               previous[column - 1] + (a != b)))
        previous = current
    return previous[-1]


def look_alikes(name, basename, projects):
    notes = []
    for project in projects:
        other = project["name"]
        folders = {os.path.basename(scan.expand_path(path, "project path"))
                   for path in (project.get("paths") or {}).values()}
        reasons = []
        if basename == other or basename in folders:
            reasons.append("same folder name")
        elif name.replace("_", "-") == other.replace("_", "-"):
            reasons.append("a -/_ variant")
        elif edit_distance(name, other) <= 2:
            reasons.append("a near spelling")
        if reasons and other != name:
            notes.append("NOTE   look-alike: `%s` (%s); consider confused_with: %s"
                         % (other, reasons[0], other))
    return notes


def register_plan(request, out):
    workspace = request["workspace"]
    path_text = request["path"]
    snapshot, document = read_registry(workspace)
    data = snapshot.data
    code_roots, excluded, declined, projects = scan.validate(copy.deepcopy(document))
    host = scan.select_host(code_roots, request["host"])

    # Step 1: the scan's own prelude and scope.
    root, exclusions, nodes, registered, _ = scan.prelude(
        code_roots, excluded, declined, projects, host)
    scope = scan.root_scope(root, exclusions, nodes, registered)
    path = scan.expand_path(path_text, "PATH")

    # Step 2: the walk refuses PATH before anything under it is touched.
    if path == root:
        raise Refusal("path %s is the code root; not registered" % path_text)
    try:
        hops = scan.walk(scope, [], path, guard_repos=True)
    except scan.Refused as refused:
        raise Refusal(refused.words("path " + path_text))
    if not stat.S_ISDIR(hops[-1][1].st_mode):
        raise Refusal("path %s is not a folder; not registered" % path_text)

    # Step 3: the known repositories, from the scan's own root walker.
    gathered = scan.gather(scope, registered, [])

    # Step 4: containment both ways, against every repo the next scan holds apart.
    refusal = scope.refusal(path, guard_repos=True)
    if refusal:
        raise Refusal(scan.Refused(refusal, path).words("path " + path_text))
    held = sorted(item for item in registered if item != path and scan.within(item, path))
    if held:
        raise Refusal("path %s, which holds the registered path%s %s; not registered"
                      % (path_text, "s" if len(held) > 1 else "", ", ".join(held)))

    # Step 5: identify the repository with the scan's own reader.
    kind, marker_info = scan.git_marker(scope, path)
    if kind is None:
        raise scan.Unverifiable("path %s is not a git repository (no .git)" % path_text)
    parent, remote = scan.identify(scope, hops, kind, marker_info, gathered.known)
    if remote is None:
        raise scan.Unverifiable("%s has no origin remote; register needs one" % parent)
    normalized = scan.normalize_remote(remote)

    # G-R3: refusals against this registry.
    name = request["name"] or os.path.basename(parent)
    reasons = []
    for project in projects:
        if project["remote"] == normalized:
            reasons.append("remote %s is already registered as project `%s`"
                           % (normalized, project["name"]))
    if parent in registered:
        reasons.append("%s is already registered for host `%s` as project `%s`"
                       % (parent, host, registered[parent][0]["name"]))
    if any(project["name"] == name for project in projects):
        reasons.append("project name `%s` is already in this registry; pass --name" % name)
    if "`" in name:
        reasons.append("project name %r holds a backtick, which the map line cannot carry"
                       % name)
    clusters = {cluster.get("id"): cluster for cluster in document.get("clusters") or []}
    if request["cluster"] not in clusters:
        reasons.append("cluster `%s` is not in clusters" % request["cluster"])
    one_liner = request["one_liner"]
    if not gen.single_line(one_liner):
        reasons.append("the one-liner must be one line")
    elif len(one_liner) > gen.ONE_LINER_LIMIT:
        reasons.append("the one-liner is %d characters, over the limit of %d"
                       % (len(one_liner), gen.ONE_LINER_LIMIT))
    if scan.declined_match(declined, normalized, parent):
        reasons.append("%s matches a declined pattern; remove it from declined first" % parent)
    if reasons:
        raise Refusal(*reasons)
    notes = look_alikes(name, os.path.basename(parent), projects)

    # G-R2: the entry, and the edges between it and the registered repositories.
    owner = clusters[request["cluster"]].get("owner")
    entry = {
        "name": name,
        "remote": stored_remote(remote),
        "paths": {host: tilde_form(parent)},
        "subpath": None,
        "one_liner": one_liner,
        "owner": owner,
        "owned_or_reference": "owned",
        "tier": "project",
        "cluster": request["cluster"],
        "lifecycle": request["lifecycle"],
        "doc": None,
        "confused_with": None,
    }
    if parent == path:
        start = hops
    else:
        start = next(found_hops for entry_path, found_hops, found_kind, _ in gathered.candidates
                     if entry_path == parent and found_kind == "repo")
    drafted = draft_edges(scope, registered, gathered, start, name, projects, document)
    additions = [("projects", [entry])]
    if drafted:
        additions.append(("edges", drafted))
    text = data.decode("utf-8")
    new_text = verified_splice(text, document, additions)
    flags = [(key, request[key]) for key in ("path", "cluster", "one_liner", "name",
                                             "lifecycle", "host")]
    digest = gen.plan_digest("register", flags, {scan.REGISTRY_NAME: new_text.encode("utf-8")})
    return finish(out, workspace, request, data, new_text, digest, notes)


def draft_edges(scope, registered, gathered, start, name, projects, document):
    """Edges between the new repository and every registered, present, non-archived one."""
    walker = edges.EdgeWalk(scope)
    names = sorted({project["name"] for project in projects} | {name})
    targets = [edges.Target(item) for item in names]
    sources = edges.project_sources({"gathered": gathered, "registered": registered})
    sources["\0new"] = (start, [name])
    findings = edges.all_findings(walker, sources, targets)
    involved = {key: value for key, value in findings.items() if name in key[:2]}
    candidates = edges.candidate_edges(involved, document)
    chosen = {}
    for (origin, target, kind), evidence in candidates.items():
        pair = (origin, target)
        if pair not in chosen or KIND_ORDER[kind] < KIND_ORDER[chosen[pair][0]]:
            chosen[pair] = (kind, evidence)
    drafted = []
    for (origin, target) in sorted(chosen):
        kind, evidence = chosen[(origin, target)]
        drafted.append({
            "from": origin,
            "to": target,
            "kind": kind,
            "evidence": edges.evidence_text(evidence),
            "approved_at": today(),
        })
    return drafted


def register_main(request, out=None):
    return run("register", register_plan, request, out)


# --------------------------------------------------------------------------
# decline
# --------------------------------------------------------------------------

def decline_plan(request, out):
    workspace = request["workspace"]
    pattern = request["pattern"]
    reason = request["reason"]
    snapshot, document = read_registry(workspace)
    data = snapshot.data
    _, _, declined, projects = scan.validate(copy.deepcopy(document))
    reasons = []
    if not reason.strip():
        reasons.append("the reason is empty")
    if pattern in declined:
        reasons.append("pattern %s is already declined" % pattern)
    for project in projects:
        paths = [scan.expand_path(text, "project `%s` path" % project["name"])
                 for text in project["paths"].values()]
        if any(scan.declined_match([pattern], project["remote"], path) for path in paths) or (
                not paths and scan.declined_match([pattern], project["remote"], "")):
            reasons.append("pattern %s matches the registered project `%s`"
                           % (pattern, project["name"]))
    if reasons:
        raise Refusal(*reasons)
    entry = {"remote": pattern, "date": today(), "reason": reason}
    text = data.decode("utf-8")
    new_text = verified_splice(text, document, [("declined", [entry])])
    flags = [("pattern", pattern), ("reason", reason)]
    digest = gen.plan_digest("decline", flags, {scan.REGISTRY_NAME: new_text.encode("utf-8")})
    return finish(out, workspace, request, data, new_text, digest, [])


def decline_main(request, out=None):
    return run("decline", decline_plan, request, out)
