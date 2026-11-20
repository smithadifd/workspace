"""The excluded-overlap rule: no project folder reaches an excluded folder by identity.

`gen` refuses, and `check` reports a gap for, a registry in which a project
that is not archived has a folder, on any host, that is an excluded folder,
holds one, or sits inside one, under any spelling that reaches it. `gen` runs
the rule before it reads anything else in the workspace, and `check`, which
`gen` runs on its own output, runs it again, so a hand-written map cannot
pass `check` either. Code roots are not judged: an excluded folder inside a
code root is the usual layout.

The folder. `project_folder` is the one function that says which folder a
project names on a host: its path, `~` expanded, joined with its `subpath`
and normalised. The editor file `gen` writes lists exactly that folder, so a
`subpath` such as `../../rooms/x` is judged as the folder it reaches.

The excluded folder. `scan.exclusion_pattern`: the entry with one trailing
`/**` or `/*` and any trailing `/` dropped, `~` expanded. An entry that still
holds `*`, `?` or `[` after that names a pattern, not a folder, and is a
could-not-verify (`scan.Unverifiable`), so every entry the rule judges is one
literal path.

Identity, two ways, each in both directions:

- By name. id(x) is `os.path.realpath(x)` (not strict, so a dangling symlink
  resolves to the text of its target), case-folded and Unicode-normalised
  (NFD), the way a case-insensitive APFS volume compares names, with a
  leading `/System/Volumes/Data` dropped, the macOS firmlink spelling of the
  same folder. chain(x) is id of x and of each folder above it, up to `/`. A
  folder P overlaps an excluded folder E when a step of chain(P) is id(E) or
  sits inside it (P is E, or inside E), or a step of chain(E) is id(P) or sits
  inside it (P holds E). "Inside" is per path component. Folding and the
  firmlink rule can only refuse more, never less.
- By node. node(x) is the (device, inode) of `os.stat(x)`, following
  symlinks. walk(s) is node(s), then node(s/..), node(s/../..) and so on, each
  `..` being the parent entry the kernel follows from the folder it reached,
  until the node repeats at the root; a path that is not a folder has only
  node(s). nodes(x) gathers walk(s) for each step s of chain's lexical steps
  that exists. P overlaps E when node(E) is in nodes(P), or node(P) is in
  nodes(E). So a spelling realpath does not unify (a firmlink, a volfs
  path, a symlink to either) is still caught, and so is every folder above
  it. Those are the spellings the tests cover; a bind mount is not claimed.

Fail closed. A step that does not exist (ENOENT, ENOTDIR) has no node, and
only the name clauses apply to it: a project folder that does not exist on
this host is not itself a refusal, but it is still judged by name and by the
folders above it that do exist. Any other error from `os.stat`, on a step or
on any `..` of its walk, on either side (EACCES, ELOOP, ...), is a
could-not-verify: `scan.Unverifiable`, naming the path.
"""
import os
import stat
import unicodedata

from workspace import scan

FIRMLINK_ROOT = "/System/Volumes/Data"
MISSING = (FileNotFoundError, NotADirectoryError)


def project_folder(project, host):
    """The folder a project names on `host`: its path joined with its subpath, or None."""
    text = (project.get("paths") or {}).get(host)
    if text is None:
        return None
    if not isinstance(text, str) or not text:
        raise scan.Unverifiable("project `%s` path for host `%s` is not a path string"
                                % (project.get("name"), host))
    path = scan.expand_path(text, "project `%s` path" % project.get("name"))
    subpath = project.get("subpath")
    if isinstance(subpath, str) and subpath:
        path = os.path.normpath(os.path.join(path, subpath))
    return path


def fold(text):
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


FOLDED_FIRMLINK_ROOT = fold(FIRMLINK_ROOT)


def unfirmlink(folded):
    """A folded path with a leading /System/Volumes/Data dropped (macOS firmlink spelling)."""
    if folded == FOLDED_FIRMLINK_ROOT:
        return "/"
    if folded.startswith(FOLDED_FIRMLINK_ROOT + "/"):
        return folded[len(FOLDED_FIRMLINK_ROOT):]
    return folded


def identity(path):
    return unfirmlink(fold(os.path.realpath(path)))


def chain(path):
    return [identity(step) for step in scan.ancestors(path)]


def cannot_stat(path, exc):
    return scan.Unverifiable("cannot stat %s: %s; could not verify that it stays clear of "
                             "the excluded paths" % (path, exc))


def node(path):
    """(st_dev, st_ino) of the path, following symlinks; None when it does not exist."""
    try:
        status = os.stat(path)
    except MISSING:
        return None
    except OSError as exc:
        raise cannot_stat(path, exc)
    return (status.st_dev, status.st_ino)


def parent_nodes(path):
    """node of the path, then of each `..` the kernel follows above it, to the root."""
    try:
        status = os.stat(path)
    except MISSING:
        return []
    except OSError as exc:
        raise cannot_stat(path, exc)
    found = [(status.st_dev, status.st_ino)]
    while stat.S_ISDIR(status.st_mode):
        path = os.path.join(path, os.pardir)
        try:
            status = os.stat(path)
        except OSError as exc:
            raise cannot_stat(path, exc)
        here = (status.st_dev, status.st_ino)
        if here in found:
            return found
        found.append(here)
    return found


def node_chain(path):
    found = []
    for step in scan.ancestors(path):
        for here in parent_nodes(step):
            if here not in found:
                found.append(here)
    return found


def inside(path, other):
    return path.startswith(other.rstrip("/") + "/")


def reaches(steps, target):
    return any(step == target or inside(step, target) for step in steps)


def shares_node(nodes, target):
    return target is not None and target in nodes


class Side:
    """One folder's identities: by name and by node."""

    def __init__(self, path):
        self.path = path
        self.identity = identity(path)
        self.chain = chain(path)
        self.node = node(path)
        self.nodes = node_chain(path)


def overlap(own, other):
    """True when the project folder `own` is, holds or sits inside the excluded folder `other`."""
    return (
        reaches(own.chain, other.identity)
        or reaches(other.chain, own.identity)
        or shares_node(own.nodes, other.node)
        or shares_node(other.nodes, own.node)
    )


def excluded_entries(document):
    """(entry text, Side) for each excluded entry that names a folder.

    An entry that is neither absolute nor starts with `~` (a placeholder such
    as `<excluded path 1>` in the shipped template) names no folder on disk,
    so it has no identity to compare: `gen` refuses it with exit 2 before this
    rule runs (`scan.exclusion_pattern`), and `check` judges it by its text
    only, as before. A `~` that cannot be expanded is a could-not-verify, and
    so is an entry that holds a glob character (`scan.exclusion_pattern`).
    """
    from workspace.check import exclusion_path

    entries = []
    for entry in document.get("excluded") or []:
        text = exclusion_path(entry)
        if not text:
            continue
        if not text.startswith("~") and not os.path.isabs(text):
            continue
        entries.append((text, Side(scan.exclusion_pattern(text))))
    return entries


def is_placeholder(text):
    """True for a placeholder project path such as `<absolute or ~/ project path>`."""
    return isinstance(text, str) and text.startswith("<") and text.endswith(">")


def excluded_overlaps(document):
    """One reason per project folder, on any host, that reaches an excluded folder.

    Archived projects are left out. A host whose path is null names no folder
    there (`project_folder` returns None), so it is not judged; `gen` refuses
    a null path before this rule runs (`scan.validate`). A placeholder path,
    text that starts with `<` and ends with `>` (such as
    `<absolute or ~/ project path>` in the shipped template), names no folder
    on disk either, so it is not judged: `gen` refuses it with exit 2 before
    this rule runs (`path_gate`). Any other path that is neither absolute nor
    starts with `~` is a could-not-verify, as is an empty one. Raises
    `scan.Unverifiable` when a step cannot be judged.
    """
    reasons = []
    excluded = None
    for project in document.get("projects") or []:
        if not isinstance(project, dict) or project.get("lifecycle") == "archived":
            continue
        paths = project.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        for host, text in paths.items():
            if is_placeholder(text):
                continue
            folder = project_folder(project, host)
            if folder is None:
                continue
            if excluded is None:
                excluded = excluded_entries(document)
            own = Side(folder)
            for text, other in excluded:
                if overlap(own, other):
                    reasons.append(
                        "project `%s` folder %s (host `%s`) reaches the excluded path `%s`: it "
                        "is, holds or sits inside that folder"
                        % (project.get("name"), folder, host, text))
    return reasons
