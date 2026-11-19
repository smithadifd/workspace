"""Read-only consistency check for a workspace's registry, map, and settings.

Given a workspace folder holding `registry.yaml`, a hand-kept map (`AGENTS.md`,
or `CLAUDE.md` where the map has not moved yet), and `.claude/settings.json`,
prove:

1. every active project has its own map line
2. archived projects appear only in the compact line
3. every excluded path is in permissions.deny (a Read and an Edit rule), and in
   sandbox.filesystem.denyRead when the settings declare a `sandbox` block
4. duplicate YAML keys are rejected; an unhashable mapping key is UNKNOWN
5. no project folder that is not archived, on any host, is, holds or sits
   inside an excluded folder under any spelling (`workspace.overlap`); a
   step it cannot stat is UNKNOWN
6. a workspace `gen` has written (it holds `.workspace-gen.json`, or its map
   carries the GENERATED header) still matches what `gen` would write from
   the registry now: `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json` and
   `.workspace-gen.json` are each compared with the plan `gen` renders in
   memory (`gen.make_plan` with `shared_only`, never `--write`). `check` is
   not given a host, so the plan holds only those shared files: the per-host
   files (`<host>.code-workspace`, `state/gen-<host>.json`) are neither
   rendered nor read, and an edit to one cannot change the verdict.

Opens the three inputs for reading only; to judge rule 5 it looks up (stats)
each project folder and excluded folder, and the folders above them, and
opens nothing there; for rule 6 it reads what `gen` reads. No network. No
writes.

Exit codes:
  0  CONFORMS
  1  named GAP
  2  UNKNOWN (could not verify, including missing PyYAML and a malformed registry)
  3  USAGE
"""
from pathlib import Path
import json
import os
import re
import sys

from workspace import overlap, scan

try:
    import yaml
except ImportError:
    sys.stdout.write("UNKNOWN  PyYAML is unavailable — cannot verify\n")
    sys.exit(2)

EX_CONFORMS = 0
EX_GAP = 1
EX_UNKNOWN = 2
EX_USAGE = 3
PROJECT_LIFECYCLES = frozenset(
    ("active", "maintenance", "deprecated", "sunset", "archived")
)

# The map is AGENTS.md; a workspace whose hand-kept map has not moved yet keeps
# it in CLAUDE.md. The first name present is the map.
MAP_NAMES = ("AGENTS.md", "CLAUDE.md")
REGISTRY_NAME = "registry.yaml"
SETTINGS_PARTS = (".claude", "settings.json")

OWN_MAP_LINE = re.compile(r"^\s*-\s+`([^`]+)`:")
ARCHIVED_CLUSTER = re.compile(r"\(lifecycle:\s*archived\)")
ARCHIVED_COMPACT = re.compile(r"^\s*Archived\s*:")
CLUSTER_HEADER = re.compile(r"^\s*\*\*([^*]+)\*\*")
SECTION_HEADING = re.compile(r"^\s*#")
BACKTICK_TOKEN = re.compile(r"`([^`]+)`")


class DuplicateKeyError(yaml.constructor.ConstructorError):
    """A mapping in the registry YAML repeated a key."""

    def __init__(self, key, mark):
        self.dup_key = key
        super().__init__(None, None, "duplicate key: %r" % (key,), mark)


class UnhashableKeyError(yaml.constructor.ConstructorError):
    """A mapping in the registry YAML used a key that cannot be hashed."""

    def __init__(self, mark):
        self.key_mark = mark
        super().__init__(None, None, "unhashable mapping key", mark)


def is_hashable(value):
    try:
        hash(value)
    except TypeError:
        return False
    return True


def construct_unique_mapping(loader, node, deep=False):
    if not isinstance(node, yaml.MappingNode):
        raise yaml.constructor.ConstructorError(
            None, None, "expected a mapping node", node.start_mark
        )
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not is_hashable(key):
            raise UnhashableKeyError(key_node.start_mark)
        if key in mapping:
            raise DuplicateKeyError(key, key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


class UniqueKeyLoader(yaml.SafeLoader):
    pass


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def unknown(reason):
    sys.stdout.write("UNKNOWN  %s\n" % reason)
    sys.exit(EX_UNKNOWN)


def load_registry_yaml(text):
    return yaml.load(text, Loader=UniqueKeyLoader)


def read_text(path, label):
    if not path.is_file():
        unknown("%s is missing or not a file: %s" % (label, path))
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        unknown("could not read %s: %s" % (label, exc))


def exclusion_forms(path):
    stripped = path[:-3] if path.endswith("/**") else path.rstrip("/")
    return {path, stripped, stripped + "/**"}


def permission_form(form):
    # In permissions.deny a single leading slash is relative to the workspace
    # folder; an absolute path is written `//absolute/path`. `~/...` is as is.
    if form.startswith("/") and not form.startswith("//"):
        return "/" + form
    return form


def permission_forms(path):
    return {permission_form(form) for form in exclusion_forms(path)}


def sandbox_forms(path):
    # sandbox.filesystem.denyRead paths keep their own absolute form.
    return exclusion_forms(path)


def permission_covers(deny_rules, path):
    forms = permission_forms(path)
    has_read = False
    has_edit = False
    for rule in deny_rules:
        for form in forms:
            if rule == "Read(%s)" % form:
                has_read = True
            elif rule == "Edit(%s)" % form:
                has_edit = True
    return has_read and has_edit


def denyread_covers(deny_read, path):
    forms = sandbox_forms(path)
    return any(item in forms for item in deny_read)


def record_exclusion_gaps(gaps, path, deny_rules, deny_read, sandbox_declared):
    if not permission_covers(deny_rules, path):
        gaps.append("excluded path `%s` is not in permissions.deny" % path)
    if sandbox_declared and not denyread_covers(deny_read, path):
        gaps.append(
            "excluded path `%s` is not in sandbox.filesystem.denyRead" % path
        )


def own_map_names(map_text):
    names = set()
    for line in map_text.splitlines():
        match = OWN_MAP_LINE.match(line)
        if match:
            names.add(match.group(1))
    return names


def compact_by_cluster(map_text):
    """Map each cluster name to the projects on its own compact line.

    A `**Cluster** (lifecycle: archived): ...` line belongs to the cluster it
    names; an `Archived: ...` line belongs to the cluster heading above it.
    """
    by_cluster = {}
    current = None
    for line in map_text.splitlines():
        if SECTION_HEADING.match(line):
            current = None
            continue
        header = CLUSTER_HEADER.match(line)
        if header:
            current = header.group(1).strip()
        if ARCHIVED_CLUSTER.search(line):
            owner = header.group(1).strip() if header else None
            by_cluster.setdefault(owner, set()).update(BACKTICK_TOKEN.findall(line))
        elif ARCHIVED_COMPACT.match(line):
            by_cluster.setdefault(current, set()).update(BACKTICK_TOKEN.findall(line))
    return by_cluster


def registry_cluster_names(document):
    clusters = document.get("clusters")
    names = {}
    if isinstance(clusters, list):
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            cluster_id = cluster.get("id")
            cluster_name = cluster.get("name")
            if isinstance(cluster_id, str) and isinstance(cluster_name, str):
                names[cluster_id] = cluster_name
    return names


def declared_cluster_names(project, cluster_names):
    value = project.get("cluster")
    ids = value if isinstance(value, list) else [value]
    return {
        cluster_names.get(item, item)
        for item in ids
        if isinstance(item, str) and item
    }


def compact_listed(compact, clusters, name):
    # A project that declares no cluster has no line of its own to check, so any
    # compact line may carry it.
    if not clusters:
        return any(name in names for names in compact.values())
    return any(name in compact.get(cluster, ()) for cluster in clusters)


def exclusion_path(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        path = entry.get("path")
        return path if isinstance(path, str) else None
    return None


def settings_lists(settings):
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        deny = []
    sandbox = settings.get("sandbox")
    if not isinstance(sandbox, dict):
        sandbox = {}
    filesystem = sandbox.get("filesystem")
    if not isinstance(filesystem, dict):
        filesystem = {}
    deny_read = filesystem.get("denyRead")
    if not isinstance(deny_read, list):
        deny_read = []
    return [item for item in deny if isinstance(item, str)], [
        item for item in deny_read if isinstance(item, str)
    ]


def check(document, map_text, settings):
    gaps = []
    projects = document.get("projects")
    excluded = document.get("excluded")
    if not isinstance(projects, list):
        unknown("registry `projects` is missing or not a list")
    if not isinstance(excluded, list):
        unknown("registry `excluded` is missing or not a list")

    own = own_map_names(map_text)
    compact = compact_by_cluster(map_text)
    cluster_names = registry_cluster_names(document)
    deny_rules, deny_read = settings_lists(settings)
    sandbox_declared = "sandbox" in settings

    for project in projects:
        if not isinstance(project, dict):
            gaps.append("project entry is not a mapping")
            continue
        name = project.get("name")
        lifecycle = project.get("lifecycle")
        if not isinstance(name, str) or not name:
            gaps.append("project entry is missing name")
            continue
        if not isinstance(lifecycle, str) or lifecycle not in PROJECT_LIFECYCLES:
            if lifecycle is None or lifecycle == "":
                gaps.append("project `%s` is missing lifecycle" % name)
            else:
                gaps.append(
                    "project `%s` has unknown lifecycle `%s`" % (name, lifecycle)
                )
            continue
        project_lifecycle = lifecycle
        if project_lifecycle == "active" and name not in own:
            gaps.append("active project `%s` has no map line" % name)
        if project_lifecycle == "archived":
            if name in own:
                gaps.append("archived project `%s` has its own map line" % name)
            declared = declared_cluster_names(project, cluster_names)
            if not compact_listed(compact, declared, name):
                gaps.append(
                    "archived project `%s` is missing from the compact line" % name
                )

    for exclusion in excluded:
        path = exclusion_path(exclusion)
        if not path:
            gaps.append("excluded entry is missing path")
            continue
        record_exclusion_gaps(gaps, path, deny_rules, deny_read, sandbox_declared)
    gaps.extend(overlap.excluded_overlaps(document))
    return gaps


def map_path_for(workspace):
    for name in MAP_NAMES:
        candidate = workspace / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return workspace / MAP_NAMES[0]


def workspace_inputs(workspace_arg):
    """The three input paths inside the workspace folder, in check order."""
    workspace = Path(workspace_arg)
    if not workspace.is_dir():
        unknown("workspace is missing or not a folder: %s" % workspace)
    return (
        workspace / REGISTRY_NAME,
        map_path_for(workspace),
        workspace.joinpath(*SETTINGS_PARTS),
    )


def fix_host(document):
    """The host to name in the fix line: the one `code_roots` host, else `<host>`.

    `check` is not given a host and needs none (the plan is `shared_only`), so
    with several hosts only the person knows theirs.
    """
    code_roots = document.get("code_roots")
    if isinstance(code_roots, dict) and len(code_roots) == 1:
        (host,) = code_roots
        if isinstance(host, str):
            return host
    return "<host>"


def stale_generated(workspace_arg, document, map_text):
    """Gaps for each generated file that differs from what `gen` would write now.

    Renders the plan in memory (`gen.make_plan`, which writes nothing) and
    compares it with the committed files, so a map restored from before a
    registry change fails even when the manifest was restored with it, and a
    hand-reverted line fails even when the manifest hash was changed to match.
    A workspace `gen` never wrote (no manifest, no GENERATED header on the
    map) has no generated files to compare and gives none.
    """
    from workspace import gen  # here, not at the top: gen imports this module

    manifest_path = os.path.join(workspace_arg, gen.MANIFEST_NAME)
    if not os.path.lexists(manifest_path) and not map_text.startswith(gen.HEADER_PREFIX):
        return []
    fix = "run `workspace gen %s --host %s`" % (workspace_arg, fix_host(document))
    try:
        # shared_only: no host, and no per-host file is rendered, read or validated.
        plan = gen.make_plan(workspace_arg, None, False, shared_only=True)
    except gen.Refusal as refusal:
        return [
            "gen would refuse to render the generated files, so they cannot be "
            "compared: %s" % reason
            for reason in refusal.reasons
        ]
    except scan.Unverifiable:
        raise
    except Exception as exc:  # fail closed, as gen does: never pass on a crash
        unknown("could not render the plan to compare: %s: %s" % (type(exc).__name__, exc))
    stale = [target.rel for target in plan.targets if target.changes]
    if plan.manifest_text != plan.manifest_data:
        stale.append(gen.MANIFEST_NAME)
    return [
        "generated file `%s` is stale against registry.yaml; %s" % (rel, fix)
        for rel in stale
    ]


def main(workspace_arg):
    sys.stdout.write("== check-workspace-registry ==\n")

    registry_path, map_path, settings_path = workspace_inputs(workspace_arg)

    registry_text = read_text(registry_path, "registry")
    map_text = read_text(map_path, "map")
    settings_text = read_text(settings_path, "settings")
    if not registry_text.strip():
        unknown("registry is empty")
    if not map_text.strip():
        unknown("map is empty")
    if not settings_text.strip():
        unknown("settings is empty")

    try:
        document = load_registry_yaml(registry_text)
    except UnhashableKeyError as exc:
        unknown(
            "registry has an unhashable mapping key (line %d, column %d)"
            % (exc.key_mark.line + 1, exc.key_mark.column + 1)
        )
    except DuplicateKeyError as exc:
        sys.stdout.write("GAP   duplicate YAML key: %r\n" % (exc.dup_key,))
        sys.stdout.write("== result: 1 gap ==\n")
        sys.exit(EX_GAP)
    except yaml.YAMLError as exc:
        unknown("registry is not parseable YAML: %s" % exc)
    except OSError as exc:
        unknown("could not read registry: %s" % exc)

    if not isinstance(document, dict):
        unknown("registry root is not a mapping")

    try:
        settings = json.loads(settings_text)
    except json.JSONDecodeError as exc:
        unknown("settings is not parseable JSON: %s" % exc)
    if not isinstance(settings, dict):
        unknown("settings root is not an object")

    try:
        gaps = check(document, map_text, settings)
        gaps.extend(stale_generated(workspace_arg, document, map_text))
    except scan.Unverifiable as exc:
        unknown(str(exc))
    if gaps:
        for gap in gaps:
            sys.stdout.write("GAP   %s\n" % gap)
        sys.stdout.write("== result: %d gap ==\n" % len(gaps))
        sys.exit(EX_GAP)

    sys.stdout.write("PASS  registry, map, and settings are consistent\n")
    sys.exit(EX_CONFORMS)
