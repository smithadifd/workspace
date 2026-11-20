"""`workspace gen WORKSPACE`: render the map, the pointer, the settings and the editor file.

Every fixture is a temp workspace with synthetic names; HOME is the temp dir,
so `~/src` is the code root and `~/private` an excluded folder. gen never
touches a repository, so no repo is created unless a test needs one.
"""
import ast
import io
import json
import os
import random
import re
import stat
import subprocess
import sys
import datetime

import pytest
import yaml

from workspace import check, gen, scan
from test_scan import REPO_ROOT

HOST = "desk"
CLUSTERS = [
    {"id": "shop", "name": "Shop", "owner": "team", "lifecycle": "active",
     "one_liner": "Selling things."},
    {"id": "data", "name": "Data", "owner": "team", "lifecycle": "active",
     "one_liner": "Moving data."},
    {"id": "old", "name": "Old team", "owner": "team", "lifecycle": "archived",
     "one_liner": "What the old team ran."},
]


def project(name, cluster, lifecycle="active", one_liner=None, paths=None, **extra):
    entry = {
        "name": name,
        "remote": "https://example.invalid/acme/%s.git" % name,
        "paths": {HOST: "~/src/%s" % name} if paths is None else paths,
        "subpath": None,
        "one_liner": one_liner if one_liner is not None else "The %s project." % name,
        "owner": "team",
        "owned_or_reference": "owned",
        "tier": "project",
        "cluster": cluster,
        "lifecycle": lifecycle,
        "doc": None,
        "confused_with": None,
    }
    entry.update(extra)
    return entry


def registry():
    return {
        "system": "Fixture",
        "code_roots": {HOST: "~/src"},
        "excluded": [{"path": "~/private", "reason": "personal data"}],
        "clusters": [dict(cluster) for cluster in CLUSTERS],
        "projects": [
            project("shop-web", "shop"),
            project("ingest", "data"),
            project("legacy", "old", lifecycle="archived"),
        ],
        "edges": [],
        "declined": [],
        "rejected_edges": [],
    }


class WS:
    def __init__(self, base, monkeypatch, document=None):
        monkeypatch.setenv("HOME", str(base))
        self.base = base
        self.path = base / "ws"
        self.path.mkdir()
        (base / "src").mkdir()
        self.document = registry() if document is None else document
        self.write_registry()

    def write_registry(self, text=None):
        if text is None:
            text = yaml.safe_dump(self.document, sort_keys=False, allow_unicode=True)
        (self.path / "registry.yaml").write_text(text, encoding="utf-8")

    def file(self, rel):
        return self.path / rel

    def read(self, rel):
        return (self.path / rel).read_text(encoding="utf-8")

    def write(self, rel, text):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def settings(self):
        return json.loads(self.read(".claude/settings.json"))


def run(ws, *flags, expect=None, host=HOST):
    buffer = io.StringIO()
    code = gen.main(str(ws.path), host=host, adopt="--adopt" in flags, write=expect is not None,
                    expect=expect, out=buffer)
    return code, buffer.getvalue()


def plan_hash(out):
    found = re.findall(r"^plan sha256=([0-9a-f]{64})$", out, re.M)
    assert len(found) == 1, out
    return found[0]


def generate(ws, *flags, host=HOST):
    code, out = run(ws, *flags, host=host)
    assert code == 0, out
    code, written = run(ws, *flags, expect=plan_hash(out), host=host)
    assert code == 0, written
    return written


def contents(folder):
    """Every file and folder under `folder`, with each file's bytes."""
    found = {}
    for current, dirs, files in os.walk(folder):
        for name in dirs:
            found[os.path.relpath(os.path.join(current, name), folder)] = None
        for name in files:
            path = os.path.join(current, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, folder)] = handle.read()
    return found


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run([sys.executable, "-P", "-m", "workspace", *args],
                            capture_output=True, text=True, env=env, check=False)
    return result.returncode, result.stdout + result.stderr


def check_passes(ws):
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out


def refused(code, out, text):
    assert code == 1, out
    assert "REFUSED  " in out and text in out, "expected %r in:\n%s" % (text, out)


# --------------------------------------------------------------------------
# A. The command line
# --------------------------------------------------------------------------

@pytest.mark.parametrize("args, message", [
    (("gen",), "gen needs a WORKSPACE"),
    (("gen", "ws", "--write"), "--write needs --expect"),
    (("gen", "ws", "--expect", "0" * 64), "--expect needs --write"),
    (("gen", "ws", "--write", "--expect", "abc"), "--expect needs the 64-hex plan hash"),
    (("gen", "ws", "--write", "--expect", "A" * 64), "--expect needs the 64-hex plan hash"),
    (("gen", "ws", "--write", "--expect", "0" * 65), "--expect needs the 64-hex plan hash"),
    (("gen", "ws", "--adopt", "--adopt"), "--adopt given twice"),
    (("gen", "ws", "--bogus"), "unknown option --bogus"),
    (("gen", "ws", "other"), "gen takes WORKSPACE"),
    (("gen", "ws", "--host"), "--host needs a value"),
    (("gen", "ws", "--host", "a", "--host=b"), "--host given twice"),
    (("register", "ws", "~/src/x", "--one-liner", "x"), "register needs --cluster"),
    (("register", "ws", "~/src/x", "--cluster", "c"), "register needs --one-liner"),
    (("register", "ws", "src/x", "--cluster", "c", "--one-liner", "x"),
     "PATH must be absolute or start with ~"),
    (("register", "ws", "~/src/x/../new", "--cluster", "c", "--one-liner", "x"),
     "PATH must not hold a . or .. component"),
    (("register", "ws", "~/src/./new", "--cluster", "c", "--one-liner", "x"),
     "PATH must not hold a . or .. component"),
    (("register", "ws", "~/src/x", "--cluster", "c", "--one-liner", "x", "--lifecycle", "bogon"),
     "--lifecycle must be one of"),
    (("decline", "ws", "pat"), "decline needs --reason"),
    (("decline", "ws", "pat", "--reason", ""), "--reason needs a value"),
    (("frob", "ws"), "unknown verb frob"),
])
def test_usage_errors_exit_3(args, message):
    code, out = run_cli(*args)
    assert code == 3, (args, out)
    assert "workspace: " + message in out, (args, out)


def test_usage_names_the_five_verbs():
    code, out = run_cli("--help")
    assert code == 0, out
    for verb in ("check", "scan", "gen", "register", "decline"):
        assert "workspace %s WORKSPACE" % verb in out, out
    assert "Both verbs only read" not in out, out


@pytest.mark.parametrize("text, reason", [
    ("projects: [unclosed\n", "registry is not parseable YAML"),
    ("system: A\nsystem: B\n", "registry repeats the key 'system'"),
])
@pytest.mark.parametrize("verb", ["gen", "register", "decline"])
def test_unreadable_registry_is_unknown_for_every_writing_verb(tmp_path, monkeypatch, verb,
                                                                text, reason):
    ws = WS(tmp_path, monkeypatch)
    ws.write_registry(text)
    before = contents(ws.path)
    args = {"gen": ["gen", str(ws.path)],
            "register": ["register", str(ws.path), "~/src/x", "--cluster", "shop",
                         "--one-liner", "x"],
            "decline": ["decline", str(ws.path), "pat", "--reason", "r"]}[verb]
    code, out = run_cli(*args)
    assert code == 2, out
    assert "UNKNOWN  " + reason in out, out
    assert contents(ws.path) == before


# --------------------------------------------------------------------------
# B. The plan and the confirm (W1-W3)
# --------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    before = contents(ws.path)
    code, out = run(ws)
    assert code == 0, out
    for name in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "desk.code-workspace"):
        assert "%s %s" % ("change" if name.startswith(".claude") else "new   ", name) in out, out
    plan_hash(out)
    assert contents(ws.path) == before


def test_write_with_the_plan_hash_writes_every_file(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    for name in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "desk.code-workspace",
                 ".workspace-gen.json", "state/gen-desk.json"):
        assert ws.file(name).is_file(), name
    manifest = json.loads(ws.read(".workspace-gen.json"))
    assert set(manifest["files"]) == {"AGENTS.md", "CLAUDE.md", ".claude/settings.json"}
    assert all("pending" not in entry for entry in manifest["files"].values()), manifest
    check_passes(ws)


def test_registry_change_after_the_dry_run_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    code, out = run(ws)
    old = plan_hash(out)
    ws.document["projects"].append(project("extra", "shop"))
    ws.write_registry()
    before = contents(ws.path)
    code, out = run(ws, expect=old)
    refused(code, out, "plan changed since review; rerun without --write")
    assert contents(ws.path) == before


def test_a_hash_that_shares_only_a_prefix_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    code, out = run(ws)
    digest = plan_hash(out)
    near = digest[:8] + ("0" if digest[8] != "0" else "1") + digest[9:]
    before = contents(ws.path)
    code, out = run(ws, expect=near)
    refused(code, out, "plan changed")
    assert contents(ws.path) == before


def test_zero_hash_on_a_fresh_workspace_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    before = contents(ws.path)
    code, out = run(ws, expect="0" * 64)
    refused(code, out, "plan changed")
    assert contents(ws.path) == before


def test_no_change_plan_is_stable_across_days(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    code, first = run(ws)

    class Tomorrow(datetime.date):
        @classmethod
        def today(cls):
            return datetime.date(2099, 1, 2)

    monkeypatch.setattr(datetime, "date", Tomorrow)
    code, second = run(ws)
    assert plan_hash(first) == plan_hash(second)
    assert "change" not in first and "new   " not in first, first


GOLDEN = REPO_ROOT / "tests" / "fixtures" / "gen" / "example-seed-AGENTS.md"
SEED = REPO_ROOT / "tests" / "fixtures" / "workspace-registry" / "example-seed" / "registry.yaml"


def test_seed_render_is_byte_identical_to_the_golden_under_any_hash_seed(tmp_path):
    outputs = []
    for seed in ("1", "2"):
        base = tmp_path / seed
        (base / "ws").mkdir(parents=True)
        (base / "src").mkdir()
        (base / "ws" / "registry.yaml").write_bytes(SEED.read_bytes())
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONHASHSEED=seed, HOME=str(base))
        result = subprocess.run([sys.executable, "-P", "-m", "workspace", "gen", str(base / "ws")],
                                capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, result.stdout
        digest = plan_hash(result.stdout)
        result = subprocess.run([sys.executable, "-P", "-m", "workspace", "gen", str(base / "ws"),
                                 "--write", "--expect", digest],
                                capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, result.stdout
        outputs.append((base / "ws" / "AGENTS.md").read_bytes())
    assert outputs[0] == outputs[1] == GOLDEN.read_bytes()


def test_adopt_is_bound_into_the_plan_hash(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    code, plain = run(ws)
    code, adopted = run(ws, "--adopt")
    assert plan_hash(plain) != plan_hash(adopted)
    before = contents(ws.path)
    code, out = run(ws, "--adopt", expect=plan_hash(plain))
    refused(code, out, "plan changed")
    assert contents(ws.path) == before


def test_unwritable_settings_folder_stops_before_any_target_is_replaced(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", "<!-- workspace:keep begin top -->\nmine\n"
                          "<!-- workspace:keep end top -->\n")
    (ws.path / ".claude").mkdir()
    code, out = run(ws, "--adopt")
    digest = plan_hash(out)
    before = contents(ws.path)
    (ws.path / ".claude").chmod(0o500)
    try:
        code, out = run(ws, "--adopt", expect=digest)
    finally:
        (ws.path / ".claude").chmod(0o700)
    assert code == 1, out
    assert contents(ws.path) == before


# --------------------------------------------------------------------------
# C. The map's content (G2-G4, G10)
# --------------------------------------------------------------------------

def map_of(ws, *flags):
    generate(ws, *flags)
    return ws.read("AGENTS.md")


def test_seed_registry_output_passes_check(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write_registry(SEED.read_text(encoding="utf-8"))
    generate(ws)
    check_passes(ws)


def test_non_active_lifecycle_is_on_the_own_line(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][1]["lifecycle"] = "maintenance"
    ws.write_registry()
    text = map_of(ws)
    assert "- `ingest`: The ingest project. lifecycle: maintenance.\n" in text, text


def test_multi_cluster_project_has_one_full_line_and_a_see_line(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][0]["cluster"] = ["data", "shop"]
    ws.write_registry()
    text = map_of(ws)
    data = text.index("**Data**")
    shop = text.index("**Shop**")
    full = text.index("- `shop-web`: The shop-web project.")
    see = text.index("- `shop-web`: see Data.")
    # Clusters keep registry order (shop, then data); the full line sits under the first cluster.
    assert shop < see < data < full, text
    assert text.count("- `shop-web`:") == 2, text
    check_passes(ws)


def test_archived_project_in_an_active_cluster_is_on_the_compact_line(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"].append(project("retired", "shop", lifecycle="archived"))
    ws.write_registry()
    text = map_of(ws)
    lines = text.splitlines()
    heading = lines.index("**Shop** — Selling things.")
    compact = lines.index("Archived: `retired`.")
    assert heading < compact, text
    assert not any(line.startswith("#") for line in lines[heading:compact]), text
    assert "- `retired`" not in text, text
    check_passes(ws)


def test_archived_cluster_is_one_line(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    text = map_of(ws)
    assert "**Old team** (lifecycle: archived): `legacy`. What the old team ran.\n" in text, text


def test_uses_lists_outgoing_edges_in_registry_order(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"].append(project("api", "shop"))
    ws.document["edges"] = [
        {"from": "shop-web", "to": "ingest", "kind": "runtime", "evidence": "x", "approved_at": None},
        {"from": "shop-web", "to": "api", "kind": "source", "evidence": "y", "approved_at": None},
    ]
    ws.write_registry()
    text = map_of(ws)
    assert ("- `shop-web`: The shop-web project. lifecycle: active. uses: ingest (runtime), "
            "api (source)\n") in text, text


def test_reference_project_keeps_a_terse_line(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][0]["owned_or_reference"] = "reference"
    ws.document["edges"] = [{"from": "shop-web", "to": "ingest", "kind": "runtime",
                             "evidence": "x", "approved_at": None}]
    ws.write_registry()
    text = map_of(ws)
    assert "- `shop-web`: The shop-web project. lifecycle: active.\n" in text, text


def test_unclustered_project_goes_under_other_last(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"].append(project("loose", None))
    ws.write_registry()
    text = map_of(ws)
    assert text.index("**Other**") > text.index("**Old team**"), text
    assert text.index("- `loose`:") > text.index("**Other**"), text
    check_passes(ws)


EXCLUDED_FORMS = ["~/x{n}", "/abs/x{n}", "~/x{n}/", "/abs/x{n}/", "~/x{n}/*", "/abs/x{n}/*",
                  "~/x{n}/**", "/abs/x{n}/**"]


def random_registry(rng, n):
    hosts = ["desk", "lap-top"][:rng.choice([1, 2])]
    clusters = []
    for index in range(rng.randint(0, 3)):
        clusters.append({"id": "c%d" % index, "name": "Cluster %d" % index, "owner": "o",
                         "lifecycle": rng.choice(["active", "active", "archived"]),
                         "one_liner": rng.choice([None, "About c%d: things & #stuff" % index])})
    projects = []
    for index in range(rng.randint(0, 6)):
        lifecycle = rng.choice(["active", "maintenance", "deprecated", "sunset", "archived"])
        choices = [None]
        for cluster in clusters:
            if cluster["lifecycle"] != "archived" or lifecycle == "archived":
                choices.append(cluster["id"])
        cluster = rng.choice(choices)
        if cluster is not None and rng.random() < 0.3 and len(choices) > 2:
            cluster = [item for item in choices[1:] if item != cluster][:1] + [cluster]
        projects.append(project("p%d" % index, cluster, lifecycle=lifecycle,
                                one_liner=rng.choice([None, "Does p%d: a, b # c" % index]),
                                paths={host: "~/src/p%d" % index for host in hosts},
                                owned_or_reference=rng.choice(["owned", "reference"])))
    edges = []
    for _ in range(rng.randint(0, 4)):
        if len(projects) >= 2:
            a, b = rng.sample(projects, 2)
            edges.append({"from": a["name"], "to": b["name"],
                          "kind": rng.choice(["build", "source", "runtime", "protocol"]),
                          "evidence": "x", "approved_at": None})
    excluded = []
    for index in range(rng.randint(1, 4)):
        form = rng.choice(EXCLUDED_FORMS)
        excluded.append({"path": form.format(n="%d_%d" % (n, index)), "reason": "r"})
    return {"system": "Draw %d" % n, "code_roots": {host: "~/src" for host in hosts},
            "excluded": excluded, "clusters": clusters, "projects": projects,
            "edges": edges, "declined": [], "rejected_edges": []}


def test_gen_output_always_passes_check(tmp_path, monkeypatch):
    rng = random.Random(20261118)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "src").mkdir()
    for n in range(200):
        folder = tmp_path / ("ws%d" % n)
        folder.mkdir()
        document = random_registry(rng, n)
        (folder / "registry.yaml").write_text(yaml.safe_dump(document, sort_keys=False),
                                              encoding="utf-8")
        if n % 2:
            (folder / ".claude").mkdir()
            (folder / ".claude" / "settings.json").write_text(
                json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
        host = sorted(document["code_roots"])[0]
        buffer = io.StringIO()
        code = gen.main(str(folder), host=host, adopt=True, out=buffer)
        assert code == 0, (n, buffer.getvalue())
        digest = plan_hash(buffer.getvalue())
        buffer = io.StringIO()
        code = gen.main(str(folder), host=host, adopt=True, write=True, expect=digest, out=buffer)
        assert code == 0, (n, buffer.getvalue())
        code, out = run_cli("check", str(folder))
        assert code == 0, (n, document, out)
        deny = json.loads((folder / ".claude" / "settings.json").read_text())["permissions"]["deny"]
        for entry in document["excluded"]:
            base = gen.stripped_base(entry["path"])
            if "*" in base:
                for form in (base, base + "/**"):
                    rule = check.permission_form(form)
                    assert "Read(%s)" % rule in deny and "Edit(%s)" % rule in deny, (n, deny)


@pytest.mark.parametrize("change, text", [
    (lambda d: d["projects"].append(project("shop-web", "old", lifecycle="archived")),
     "project name `shop-web` is used twice"),
    (lambda d: d["projects"].append(project("ghostly", "ghost")),
     "project `ghostly` names cluster `ghost`, which is not in clusters"),
    (lambda d: d["projects"].append(project("alive", "old")),
     "project `alive` is active but its cluster `old` is archived"),
    (lambda d: d["projects"].append(project("a`b", "shop")),
     "holds a backtick"),
    (lambda d: d["clusters"][0].update(name="Sh*p"),
     "cluster `shop` name holds `*`"),
    (lambda d: d["projects"].append(project("bad", 5)),
     "project `bad` cluster must be a cluster id or a list of ids"),
    (lambda d: d["clusters"][0].update(one_liner="two\nlines"),
     "cluster `shop` one_liner must be one line of text"),
    (lambda d: d["clusters"][0].pop("id"), "a cluster has no id"),
    (lambda d: d["clusters"].append(dict(d["clusters"][1])), "cluster id `data` is repeated"),
])
def test_map_grammar_gate_refuses_and_writes_nothing(tmp_path, monkeypatch, change, text):
    ws = WS(tmp_path, monkeypatch)
    change(ws.document)
    ws.write_registry()
    before = contents(ws.path)
    code, out = run(ws)
    refused(code, out, text)
    assert contents(ws.path) == before


def test_gen_runs_no_subprocess_and_needs_no_path(tmp_path, monkeypatch):
    source = (REPO_ROOT / "workspace" / "gen.py").read_text(encoding="utf-8")
    assert "subprocess" not in source and "urllib" not in source and "socket" not in source
    ws = WS(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("PATH", "")
    generate(ws)
    assert calls == []


def test_claude_md_is_exactly_the_pointer(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    assert ws.file("CLAUDE.md").read_bytes() == b"@AGENTS.md\n"


def test_map_starts_with_the_generated_header(tmp_path, monkeypatch):
    text = map_of(WS(tmp_path, monkeypatch))
    assert text.splitlines()[0] == gen.HEADER


# --------------------------------------------------------------------------
# D. The lazy map and the budget (G5, G6)
# --------------------------------------------------------------------------

def keep_text(top="", bottom=""):
    return ("<!-- workspace:keep begin top -->\n%s<!-- workspace:keep end top -->\n"
            "<!-- workspace:keep begin bottom -->\n%s<!-- workspace:keep end bottom -->\n"
            % (top, bottom))


@pytest.mark.parametrize("where, value, ok", [
    ("one_liner", "see @~/src/shop-web/AGENTS.md", False),
    ("keep", "@~/src/ingest/README.md\n", False),
    ("keep", "@~/.claude/memory/MEMORY.md\n", True),
    ("one_liner", "`see @~/src/shop-web/x`", True),
    ("one_liner", "contact x@~/src/shop-web/AGENTS.md", True),
    ("one_liner", "see @./../src/shop-web/AGENTS.md", False),
    ("second-host", "see @~/lap/shop-web/AGENTS.md", False),
])
def test_imports_into_a_registered_project_are_refused(tmp_path, monkeypatch, where, value, ok):
    ws = WS(tmp_path, monkeypatch)
    if where == "keep":
        ws.write("AGENTS.md", keep_text(top=value))
        flags = ("--adopt",)
    else:
        ws.document["projects"][1]["one_liner"] = value
        flags = ()
    if where == "second-host":
        ws.document["code_roots"]["lap"] = "~/lap"
        ws.document["projects"][0]["paths"]["lap"] = "~/lap/shop-web"
    ws.write_registry()
    before = contents(ws.path)
    code, out = run(ws, *flags)
    if ok:
        assert code == 0, out
        if where == "keep":
            generate(ws, *flags)
            assert value in ws.read("AGENTS.md")
        return
    refused(code, out, "which resolves inside project `")
    assert contents(ws.path) == before


IMPORT_LINE = "which resolves inside project `"


def import_refused(tmp_path, argument, token, project_name):
    """gen's plan for WORKSPACE typed as `argument`: exit 1, one line for `token`, nothing written."""
    before = contents(tmp_path)
    buffer = io.StringIO()
    code = gen.main(argument, host=HOST, out=buffer)
    out = buffer.getvalue()
    refused(code, out, "the map imports @%s, which resolves inside project `%s`"
            % (token, project_name))
    assert out.count(IMPORT_LINE) == 1, out
    assert contents(tmp_path) == before


def test_an_import_inside_a_project_only_from_the_physical_folder_is_refused(tmp_path,
                                                                             monkeypatch):
    # WORKSPACE is typed `ws-a/link/..` with ws-a/link -> src/ws-b/sub: the kernel
    # opens src/ws-b, where the map is, so `@../shop-web` lands in the project;
    # from the typed folder, ws-a, it would land in ~/shop-web, which is no project.
    monkeypatch.setenv("HOME", str(tmp_path))
    document = registry()
    token = "../shop-web/AGENTS.md"
    document["projects"][1]["one_liner"] = "see @" + token
    ws_b = tmp_path / "src" / "ws-b"
    (ws_b / "sub").mkdir(parents=True)
    (ws_b / "registry.yaml").write_text(yaml.safe_dump(document, sort_keys=False),
                                        encoding="utf-8")
    (tmp_path / "ws-a").mkdir()
    (tmp_path / "ws-a" / "link").symlink_to(ws_b / "sub")
    monkeypatch.chdir(tmp_path)
    import_refused(tmp_path, os.path.join("ws-a", "link", os.pardir), token, "shop-web")


def test_an_import_inside_a_project_only_from_the_typed_folder_is_refused(tmp_path,
                                                                          monkeypatch):
    # WORKSPACE is typed as a plain symlink alias two folders down: from the alias
    # `@../../src/shop-web` lands in the project; from the real folder it would
    # land above HOME.
    ws = WS(tmp_path, monkeypatch)
    token = "../../src/shop-web/AGENTS.md"
    ws.document["projects"][1]["one_liner"] = "see @" + token
    ws.write_registry()
    (tmp_path / "links").mkdir()
    alias = tmp_path / "links" / "alias"
    os.symlink(ws.path, alias)
    import_refused(tmp_path, str(alias), token, "shop-web")


def test_an_import_both_folders_place_in_a_project_is_refused_once(tmp_path, monkeypatch):
    # From the alias src/alias, `@../ingest` lands in ingest (~/src/ingest); from
    # the real folder ~/ws it lands in legacy (~/ingest). One line for the token.
    ws = WS(tmp_path, monkeypatch)
    token = "../ingest/notes.md"
    ws.document["projects"][1]["one_liner"] = "see @" + token
    ws.document["projects"][2]["paths"] = {HOST: "~/ingest"}
    ws.write_registry()
    alias = tmp_path / "src" / "alias"
    os.symlink(ws.path, alias)
    import_refused(tmp_path, str(alias), token, "ingest")


def fill_to(ws, size):
    """Pad the top keep block so the rendered map is exactly `size` bytes."""
    ws.write("AGENTS.md", keep_text(top="x\n"))
    code, out = run(ws, "--adopt")
    assert code == 0, out
    rendered = gen.render_map(ws.document, {c["id"]: c for c in ws.document["clusters"]},
                              {"top": "x\n"})
    pad = size - len(rendered.encode("utf-8"))
    ws.write("AGENTS.md", keep_text(top="x" * pad + "x\n"))


@pytest.mark.parametrize("extra, ok", [(0, True), (1, False)])
def test_map_budget_counts_the_whole_file(tmp_path, monkeypatch, extra, ok):
    ws = WS(tmp_path, monkeypatch)
    fill_to(ws, gen.MAP_BUDGET_BYTES + extra)
    code, out = run(ws, "--adopt")
    if ok:
        assert code == 0, out
    else:
        refused(code, out, "AGENTS.md would be %d bytes, over the limit of %d bytes"
                % (gen.MAP_BUDGET_BYTES + 1, gen.MAP_BUDGET_BYTES))


@pytest.mark.parametrize("value, text", [
    ("x" * 161, "project `ingest` one_liner is 161 characters, over the limit of 160"),
    ("two\nlines", "project `ingest` one_liner must be one line of text"),
])
def test_one_liner_must_be_one_short_line(tmp_path, monkeypatch, value, text):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][1]["one_liner"] = value
    ws.write_registry()
    refused(*run(ws), text)


def test_one_liner_at_the_limit_is_accepted(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][1]["one_liner"] = "x" * 160
    ws.write_registry()
    assert run(ws)[0] == 0


def test_budget_is_fixed():
    assert gen.MAP_BUDGET_BYTES == 16384
    assert gen.ONE_LINER_LIMIT == 160


# --------------------------------------------------------------------------
# E. Keep blocks, divergence and adoption (G7-G9, W3a)
# --------------------------------------------------------------------------

def generated_with_keep(tmp_path, monkeypatch, top="mine\n", bottom=""):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", keep_text(top=top, bottom=bottom))
    generate(ws, "--adopt")
    return ws


def test_edit_inside_the_keep_block_is_kept(tmp_path, monkeypatch):
    ws = generated_with_keep(tmp_path, monkeypatch)
    ws.write("AGENTS.md", ws.read("AGENTS.md").replace("mine\n", "mine, edited\n"))
    ws.document["projects"].append(project("extra", "shop"))
    ws.write_registry()
    generate(ws)
    text = ws.read("AGENTS.md")
    assert "mine, edited\n" in text and "- `extra`:" in text, text


def test_edit_outside_the_keep_block_is_divergence(tmp_path, monkeypatch):
    ws = generated_with_keep(tmp_path, monkeypatch)
    ws.write("AGENTS.md", ws.read("AGENTS.md").replace("The ingest project.", "Hand edit."))
    before = contents(ws.path)
    for flags in ((), ("--adopt",)):
        code, out = run(ws, *flags)
        refused(code, out, "AGENTS.md was changed outside gen")
        assert "-- `ingest`: Hand edit." in out, out
        assert contents(ws.path) == before


@pytest.mark.parametrize("text, reason", [
    (keep_text() + "<!-- workspace:keep begin top -->\n<!-- workspace:keep end top -->\n",
     "keep block `top` appears twice"),
    ("<!-- workspace:keep begin middle -->\n<!-- workspace:keep end middle -->\n",
     "unknown keep block `middle`"),
    ("<!-- workspace:keep begin top -->\n<!-- workspace:keep end bottom -->\n",
     "`end bottom` closes `begin top`"),
    ("<!-- workspace:keep begin top -->\nopen\n", "keep block `top` has no end marker"),
    ("<!-- workspace:keep begin top -->\n<!-- workspace:keep begin bottom -->\n",
     "keep block `bottom` begins inside `top`"),
    ("<!--  workspace:keep begin top -->\n", "malformed keep marker"),
    ("<!-- workspace:keep end top -->\n", "keep block `top` ends without a begin"),
])
def test_keep_marker_grammar(tmp_path, monkeypatch, text, reason):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", text)
    refused(*run(ws, "--adopt"), reason)


def test_keep_blocks_have_fixed_places(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    # Bottom comes first in the source, to prove placement is fixed, not source order.
    ws.write("AGENTS.md", "<!-- workspace:keep begin bottom -->\nBOTTOM TEXT\n"
                          "<!-- workspace:keep end bottom -->\n"
                          "<!-- workspace:keep begin top -->\nTOP TEXT\n"
                          "<!-- workspace:keep end top -->\n")
    text = map_of(ws, "--adopt")
    lines = text.splitlines()
    assert lines.index("TOP TEXT") < lines.index("## Repos by cluster"), text
    assert lines[-2:] == ["BOTTOM TEXT", "<!-- workspace:keep end bottom -->"], text


def test_hand_kept_claude_md_without_adopt_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("CLAUDE.md", "# My map\n- `shop-web`: hand line\n")
    before = contents(ws.path)
    code, out = run(ws)
    refused(code, out, "CLAUDE.md exists and gen has no record of writing it")
    assert contents(ws.path) == before


def test_hand_kept_claude_md_without_markers_is_refused_on_adopt(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("CLAUDE.md", "# My map\n- `shop-web`: hand line\n")
    refused(*run(ws, "--adopt"), "CLAUDE.md: add workspace:keep markers")


def test_hand_kept_claude_md_with_markers_is_adopted(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("CLAUDE.md", "# My map\n<!-- workspace:keep begin top -->\nSEAT USE\n"
                          "<!-- workspace:keep end top -->\nold line\n"
                          "<!-- workspace:keep begin bottom -->\nHOOKS\n"
                          "<!-- workspace:keep end bottom -->\n")
    generate(ws, "--adopt")
    text = ws.read("AGENTS.md")
    assert "SEAT USE\n" in text and "HOOKS\n" in text, text
    assert ws.file("CLAUDE.md").read_bytes() == b"@AGENTS.md\n"
    check_passes(ws)


def test_file_equal_to_the_render_is_adopted_without_adopt(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.file(".workspace-gen.json").unlink()
    ws.file("state/gen-desk.json").unlink()
    written = generate(ws)
    assert "wrote" not in written, written
    assert ws.file(".workspace-gen.json").is_file()


class Fault:
    """os.replace that raises at the n-th call whose destination ends with `name`."""

    def __init__(self, monkeypatch, name, nth=1):
        self.original = os.replace
        self.count = 0
        monkeypatch.setattr(gen.os, "replace", self)
        self.name = name
        self.nth = nth

    def __call__(self, src, dst, **kwargs):
        if str(dst).endswith(self.name):
            self.count += 1
            if self.count == self.nth:
                raise OSError("fault injected")
        return self.original(src, dst, **kwargs)


def test_crash_after_targets_replaced_does_not_lock_out(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.document["projects"].append(project("second", "data"))
    ws.write_registry()
    code, out = run(ws)
    with monkeypatch.context() as patch:
        fault = Fault(patch, gen.MANIFEST_NAME, nth=2)
        code, out = run(ws, expect=plan_hash(out))
    assert code == 2, out
    assert "- `second`:" in ws.read("AGENTS.md")
    assert "pending" in ws.read(".workspace-gen.json")
    assert "pending" in ws.read("state/gen-desk.json")
    ws.document["projects"].append(project("third", "data"))
    ws.write_registry()
    generate(ws)
    assert "- `third`:" in ws.read("AGENTS.md")
    assert "pending" not in ws.read(".workspace-gen.json")


def test_crash_after_the_temps_leaves_no_temp_and_no_change(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.document["projects"].append(project("second", "data"))
    ws.write_registry()
    old = ws.read("AGENTS.md")
    code, out = run(ws)
    with monkeypatch.context() as patch:
        Fault(patch, gen.MANIFEST_NAME, nth=1)
        code, out = run(ws, expect=plan_hash(out))
    assert code == 2, out
    assert ws.read("AGENTS.md") == old
    leftovers = [name for name in contents(ws.path) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []
    generate(ws)


def test_a_leftover_temp_is_removed_only_after_the_confirm(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    leftover = ws.file("." + "AGENTS.md" + gen.TEMP_SUFFIX)
    leftover.write_text("half\n", encoding="utf-8")
    code, out = run(ws)
    assert leftover.exists()
    code, stale = run(ws, expect="0" * 64)
    assert code == 1 and leftover.exists(), stale
    code, out = run(ws, expect=plan_hash(out))
    assert code == 0, out
    assert not leftover.exists()


def test_interrupted_first_adoption_retries_against_the_original(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    hand = keep_text(top="mine\n") + "a hand line\n"
    ws.write("AGENTS.md", hand)
    code, out = run(ws, "--adopt")
    with monkeypatch.context() as patch:
        Fault(patch, "AGENTS.md", nth=1)
        code, out = run(ws, "--adopt", expect=plan_hash(out))
    assert code == 2, out
    assert ws.read("AGENTS.md") == hand
    assert "pending" in ws.read(".workspace-gen.json")
    written = generate(ws)
    assert "wrote  AGENTS.md" in written, written
    assert "mine\n" in ws.read("AGENTS.md")


def test_a_target_changed_during_the_write_is_not_overwritten(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.document["projects"].append(project("second", "data"))
    ws.write_registry()
    code, out = run(ws)
    original = gen.write_temp

    def racing(folder, name, data, *rest):
        temp = original(folder, name, data, *rest)
        if name == "AGENTS.md":
            ws.write("AGENTS.md", ws.read("AGENTS.md") + "a racing writer's line\n")
        return temp

    monkeypatch.setattr(gen, "write_temp", racing)
    code, out = run(ws, expect=plan_hash(out))
    refused(code, out, "AGENTS.md changed during the run")
    assert ws.read("AGENTS.md").endswith("a racing writer's line\n")
    assert "pending" not in ws.read(".workspace-gen.json")
    leftovers = [name for name in contents(ws.path) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []


CREDENTIAL = {"env": {"TOKEN_NAME": "ENV_VALUE_SENTINEL"}, "apiKeyHelper": "HELPER_SENTINEL"}


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def outside_folder(tmp_path):
    """A folder outside the workspace, beneath the excluded ~/private, holding settings."""
    outside = tmp_path / "private" / "elsewhere"
    outside.mkdir(parents=True)
    (outside / "settings.json").write_text(json.dumps(CREDENTIAL), encoding="utf-8")
    return outside


@pytest.mark.parametrize("folder", [".claude", "state"])
def test_symlinked_output_folder_is_refused_before_it_is_read(tmp_path, monkeypatch, folder):
    ws = WS(tmp_path, monkeypatch)
    os.symlink(outside_folder(tmp_path), ws.path / folder)
    before = contents(tmp_path)
    for expect in (None, "0" * 64):
        code, out = run(ws, expect=expect)
        refused(code, out, gen.OUTPUT_LINK % folder)
        assert "SENTINEL" not in out, out
    assert contents(tmp_path) == before


@pytest.mark.parametrize("folder", [".claude", "state"])
def test_output_folder_swapped_for_a_symlink_after_the_plan_is_not_written_through(
        tmp_path, monkeypatch, folder):
    ws = WS(tmp_path, monkeypatch)
    plan = gen.make_plan(str(ws.path), HOST, False)
    outside = outside_folder(tmp_path)
    # A file outside named like a gen temp: cleanup after the refusal must not reach it.
    (outside / (".settings.json" + gen.TEMP_SUFFIX)).write_text("theirs\n", encoding="utf-8")
    os.symlink(outside, ws.path / folder)
    before = contents(outside)
    buffer = io.StringIO()
    code = gen.write_plan(str(ws.path), plan, buffer)
    assert code == 1, buffer.getvalue()
    assert "REFUSED  could not write: " in buffer.getvalue(), buffer.getvalue()
    assert contents(outside) == before
    assert sorted(os.listdir(ws.path)) == sorted(["registry.yaml", folder])


def test_reads_below_the_workspace_never_follow_a_symlinked_folder(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    os.symlink(outside_folder(tmp_path), ws.path / ".claude")
    with pytest.raises(scan.Unverifiable, match="could not read .claude/settings.json: "):
        gen.read_bytes(str(ws.path), gen.SETTINGS_REL)


def test_first_run_refusal_shows_only_gen_owned_settings(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json",
             json.dumps(dict(CREDENTIAL, permissions={"deny": []}), indent=2))
    before = contents(ws.path)
    code, out = run(ws)
    refused(code, out, ".claude/settings.json exists and gen has no record of writing it")
    assert "(gen-owned entries only)" in out, out
    assert "+  deny: Read(~/private/**)" in out, out
    for secret in ("SENTINEL", "TOKEN_NAME", "apiKeyHelper", '"env"'):
        assert secret not in out, out
    assert contents(ws.path) == before


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_rewritten_settings_keep_their_mode(tmp_path, monkeypatch, umask_022, mode):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json", json.dumps(CREDENTIAL))
    ws.file(".claude/settings.json").chmod(mode)
    written = generate(ws, "--adopt")
    assert "wrote  .claude/settings.json" in written, written
    assert "Read(~/private/**)" in ws.read(".claude/settings.json")
    assert stat.S_IMODE(ws.file(".claude/settings.json").stat().st_mode) == mode


def test_a_replacing_temp_is_created_owner_only(tmp_path, monkeypatch, umask_022):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json", json.dumps(CREDENTIAL))
    ws.file(".claude/settings.json").chmod(0o644)
    code, out = run(ws, "--adopt")
    created = {}
    original = os.open

    def spying(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(gen.TEMP_SUFFIX):
            created[str(path)] = mode
        return original(path, flags, mode, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(gen.os, "open", spying)
        code, written = run(ws, "--adopt", expect=plan_hash(out))
    assert code == 0, written
    assert created[".settings.json" + gen.TEMP_SUFFIX] == 0o600, created
    assert created[".AGENTS.md" + gen.TEMP_SUFFIX] == 0o666, created


def test_new_files_get_the_usual_mode(tmp_path, monkeypatch, umask_022):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    for rel in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "desk.code-workspace",
                ".workspace-gen.json", "state/gen-desk.json"):
        assert stat.S_IMODE(ws.file(rel).stat().st_mode) == 0o644, rel


def test_deleted_editor_file_is_rewritten(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.file("desk.code-workspace").unlink()
    written = generate(ws)
    assert "wrote  desk.code-workspace" in written
    assert ws.file("desk.code-workspace").is_file()


def test_deleted_map_with_a_kept_block_is_refused(tmp_path, monkeypatch):
    ws = generated_with_keep(tmp_path, monkeypatch)
    ws.file("AGENTS.md").unlink()
    before = contents(ws.path)
    refused(*run(ws), "keep block source missing; restore AGENTS.md from git")
    assert contents(ws.path) == before


def test_hand_edited_editor_file_is_divergence(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.write("desk.code-workspace", '{"folders": []}\n')
    refused(*run(ws), "desk.code-workspace was changed outside gen")


def test_a_generated_header_without_a_record_is_not_adopted(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", gen.HEADER + "\n" + keep_text())
    refused(*run(ws, "--adopt"), "carries a GENERATED header")


# --------------------------------------------------------------------------
# F. Settings (S1-S3)
# --------------------------------------------------------------------------

def test_fresh_settings_hold_roots_and_deny_rules_and_no_sandbox(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    assert ws.settings() == {"permissions": {"additionalDirectories": ["~/src"],
                                             "deny": ["Read(~/private/**)", "Edit(~/private/**)"]}}


def test_person_keys_and_rules_pass_through(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["hooks"] = {"Stop": [{"command": "true"}]}
    settings["permissions"]["deny"].append("Read(~/mine/**)")
    ws.write(".claude/settings.json", json.dumps(settings, indent=2))
    ws.document["excluded"].append({"path": "~/more", "reason": "r"})
    ws.write_registry()
    generate(ws)
    after = ws.settings()
    assert after["hooks"] == {"Stop": [{"command": "true"}]}
    assert "Read(~/mine/**)" in after["permissions"]["deny"]
    assert "Read(~/more/**)" in after["permissions"]["deny"]


def test_declared_sandbox_gains_deny_read_entries(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json", json.dumps(
        {"sandbox": {"enabled": True, "filesystem": {"denyRead": [], "allowWrite": ["/tmp"]}}}))
    generate(ws, "--adopt")
    sandbox = ws.settings()["sandbox"]
    assert sandbox == {"enabled": True, "filesystem": {"denyRead": ["~/private"],
                                                       "allowWrite": ["/tmp"]}}
    check_passes(ws)


def test_sandbox_without_filesystem_gains_it(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json", json.dumps({"sandbox": {"enabled": True}}))
    generate(ws, "--adopt")
    assert ws.settings()["sandbox"]["filesystem"] == {"denyRead": ["~/private"]}


def test_removed_excluded_path_loses_only_gen_owned_rules(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"].append({"path": "~/gone", "reason": "r"})
    ws.write_registry()
    generate(ws)
    settings = ws.settings()
    settings["permissions"]["deny"].append("Read(~/gone)")
    ws.write(".claude/settings.json", json.dumps(settings, indent=2))
    ws.document["excluded"].pop()
    ws.write_registry()
    generate(ws)
    deny = ws.settings()["permissions"]["deny"]
    assert deny == ["Read(~/private/**)", "Edit(~/private/**)", "Read(~/gone)"], deny
    check_passes(ws)


def test_gen_owned_rule_deleted_by_hand_is_divergence(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["permissions"]["deny"].remove("Edit(~/private/**)")
    ws.write(".claude/settings.json", json.dumps(settings, indent=2))
    code, out = run(ws)
    refused(code, out, ".claude/settings.json was changed outside gen")
    assert "deny Edit(~/private/**)" in out, out


def test_two_hosts_give_both_roots_in_registry_order(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["code_roots"] = {"desk": "~/src", "lap": "~/lap"}
    ws.write_registry()
    generate(ws)
    assert ws.settings()["permissions"]["additionalDirectories"] == ["~/src", "~/lap"]


HOME_SETTINGS = """{
  "permissions": {
    "additionalDirectories": ["~/src"],
    "deny": ["Read(~/work/**)", "Edit(~/work/**)", "Read(~/rooms/**)", "Edit(~/rooms/**)"]
  },
  "sandbox": {"enabled": true, "filesystem": {"denyRead": ["~/work", "~/rooms"]}}
}
"""


def home_shaped(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"] = [{"path": "~/work", "reason": "r"}, {"path": "~/rooms", "reason": "r"}]
    ws.write_registry()
    ws.write(".claude/settings.json", HOME_SETTINGS)
    return ws


def test_matching_settings_are_not_rewritten_on_adopt(tmp_path, monkeypatch):
    ws = home_shaped(tmp_path, monkeypatch)
    info = ws.file(".claude/settings.json").stat()
    generate(ws, "--adopt")
    assert ws.read(".claude/settings.json") == HOME_SETTINGS
    assert ws.file(".claude/settings.json").stat().st_mtime_ns == info.st_mtime_ns


def test_adopted_rules_are_removed_when_the_path_leaves(tmp_path, monkeypatch):
    ws = home_shaped(tmp_path, monkeypatch)
    generate(ws, "--adopt")
    ws.document["excluded"] = [{"path": "~/rooms", "reason": "r"}]
    ws.write_registry()
    generate(ws)
    settings = ws.settings()
    assert settings["permissions"]["deny"] == ["Read(~/rooms/**)", "Edit(~/rooms/**)"]
    assert settings["sandbox"]["filesystem"]["denyRead"] == ["~/rooms"]
    check_passes(ws)


def deny_for(tmp_path, monkeypatch, paths, existing=None):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"] = [{"path": path, "reason": "r"} for path in paths]
    ws.write_registry()
    flags = ()
    if existing is not None:
        ws.write(".claude/settings.json", json.dumps({"permissions": {"deny": existing}}))
        flags = ("--adopt",)
    generate(ws, *flags)
    check_passes(ws)
    return ws.settings()["permissions"]["deny"]


def test_each_literal_form_gets_the_check_accepted_pair(tmp_path, monkeypatch):
    deny = deny_for(tmp_path, monkeypatch, ["~/private", "~/vault/**", "/var/private", "~/trail/"])
    assert deny == ["Read(~/private/**)", "Edit(~/private/**)", "Read(~/vault/**)",
                    "Edit(~/vault/**)", "Read(//var/private/**)", "Edit(//var/private/**)",
                    "Read(~/trail/**)", "Edit(~/trail/**)"], deny


def test_a_glob_base_gets_both_pairs(tmp_path, monkeypatch):
    paths = ["~/x/*", "/abs/y/*"]
    deny = deny_for(tmp_path, monkeypatch, paths)
    assert deny == ["Read(~/x/*)", "Edit(~/x/*)", "Read(~/x/*/**)", "Edit(~/x/*/**)",
                    "Read(//abs/y/*)", "Edit(//abs/y/*)", "Read(//abs/y/*/**)",
                    "Edit(//abs/y/*/**)"], deny
    for path in paths:
        forms = {"%s(%s)" % (verb, form) for form in check.permission_forms(path)
                 for verb in ("Read", "Edit")}
        assert set(gen.deny_rules(path)) <= forms


def test_a_pattern_exclusion_gets_no_deny_rule_and_is_refused(tmp_path, monkeypatch):
    # `~/z/priv-*` named a family of folders; an excluded entry now names one.
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"] = [{"path": "~/z/priv-*", "reason": "r"}]
    ws.write_registry()
    glob_refused(ws, "~/z/priv-*")


def test_existing_covering_pair_is_not_duplicated(tmp_path, monkeypatch):
    deny = deny_for(tmp_path, monkeypatch, ["~/private"],
                    existing=["Read(~/private)", "Edit(~/private)"])
    assert deny == ["Read(~/private)", "Edit(~/private)"], deny


def test_glob_base_with_only_the_beneath_pair_gains_the_children_pair(tmp_path, monkeypatch):
    deny = deny_for(tmp_path, monkeypatch, ["~/x/*"],
                    existing=["Read(~/x/*/**)", "Edit(~/x/*/**)"])
    assert deny == ["Read(~/x/*/**)", "Edit(~/x/*/**)", "Read(~/x/*)", "Edit(~/x/*)"], deny


def test_mixed_pair_is_completed_one_rule_at_a_time(tmp_path, monkeypatch):
    deny = deny_for(tmp_path, monkeypatch, ["~/x/*"],
                    existing=["Read(~/x/*)", "Edit(~/x/*/**)"])
    assert deny == ["Read(~/x/*)", "Edit(~/x/*/**)", "Edit(~/x/*)", "Read(~/x/*/**)"], deny


def test_plan_shows_only_gen_owned_settings(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["env"] = {"TOKEN_NAME": "ENV_VALUE_SENTINEL"}
    ws.write(".claude/settings.json", json.dumps(settings, indent=2))
    ws.document["excluded"].append({"path": "~/more", "reason": "r"})
    ws.write_registry()
    code, out = run(ws)
    assert code == 0, out
    assert "+  deny: Read(~/more/**)" in out, out
    assert "ENV_VALUE_SENTINEL" not in out and "TOKEN_NAME" not in out, out


def test_a_renderer_fault_is_caught_by_the_self_check(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"] = [{"path": "/var/private", "reason": "r"}]
    ws.write_registry()
    monkeypatch.setattr(gen, "deny_rules", lambda path: ["Read(/var/private/**)",
                                                         "Edit(/var/private/**)"])
    before = contents(ws.path)
    for expect in (None, "0" * 64):
        code, out = run(ws, expect=expect)
        refused(code, out, "gen output fails check: excluded path `/var/private` is not in "
                           "permissions.deny")
    assert contents(ws.path) == before


def test_keep_block_that_breaks_check_is_caught_by_the_self_check(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", keep_text(top="- `legacy`: still here\n"))
    before = contents(ws.path)
    refused(*run(ws, "--adopt"),
            "gen output fails check: archived project `legacy` has its own map line")
    assert contents(ws.path) == before


def test_sandbox_deny_read_keeps_the_raw_absolute_form(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"] = [{"path": "/var/private", "reason": "r"}]
    ws.write_registry()
    ws.write(".claude/settings.json", json.dumps({"sandbox": {"enabled": True}}))
    generate(ws, "--adopt")
    assert ws.settings()["sandbox"]["filesystem"]["denyRead"] == ["/var/private"]
    check_passes(ws)


@pytest.mark.parametrize("settings, text", [
    ({"permissions": []}, "settings.json `permissions` is not an object"),
    ({"permissions": {"deny": "Read(x)"}}, "settings.json `permissions.deny` is not a list"),
    ({"sandbox": True}, "settings.json `sandbox` is not an object"),
    ({"sandbox": {"filesystem": []}}, "settings.json `sandbox.filesystem` is not an object"),
    ({"sandbox": {"filesystem": {"denyRead": "x"}}},
     "settings.json `sandbox.filesystem.denyRead` is not a list"),
])
def test_settings_shape_gen_cannot_merge_into_is_refused(tmp_path, monkeypatch, settings, text):
    ws = WS(tmp_path, monkeypatch)
    ws.write(".claude/settings.json", json.dumps(settings))
    before = contents(ws.path)
    refused(*run(ws, "--adopt"), text)
    assert contents(ws.path) == before


@pytest.mark.parametrize("rel, data, text", [
    (".claude/settings.json", b"{not json", ".claude/settings.json is not parseable JSON"),
    (".claude/settings.json", b"[]", ".claude/settings.json root is not an object"),
    (".workspace-gen.json", b'{"version": 9, "files": {}}',
     ".workspace-gen.json is not a gen manifest this version reads"),
    ("AGENTS.md", b"\xff\xfe map", "AGENTS.md is not UTF-8"),
])
def test_unreadable_inputs_are_unknown(tmp_path, monkeypatch, rel, data, text):
    ws = WS(tmp_path, monkeypatch)
    target = ws.path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    before = contents(ws.path)
    code, out = run(ws, "--adopt")
    assert code == 2, out
    assert "UNKNOWN  " + text in out, out
    assert contents(ws.path) == before


def test_map_that_cannot_be_opened_is_unknown(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.write("AGENTS.md", keep_text())
    ws.file("AGENTS.md").chmod(0)
    try:
        code, out = run(ws, "--adopt")
    finally:
        ws.file("AGENTS.md").chmod(0o600)
    assert code == 2, out
    assert "UNKNOWN  could not read AGENTS.md: " in out, out


def glob_refused(ws, entry):
    """gen with `entry` excluded: exit 2 naming the entry as written, nothing written."""
    before = contents(ws.base)
    for expect in (None, "0" * 64):
        code, out = run(ws, expect=expect)
        assert code == 2, out
        assert "UNKNOWN  " + scan.EXCLUDED_GLOB % entry in out, out
    assert contents(ws.base) == before


@pytest.mark.parametrize("name", ["[priv]", "p[ri]v"])
def test_excluded_real_name_with_a_glob_class_is_refused(tmp_path, monkeypatch, name):
    ws = WS(tmp_path, monkeypatch)
    (tmp_path / name).mkdir()
    ws.document["excluded"].append({"path": "~/%s" % name, "reason": "r"})
    ws.write_registry()
    glob_refused(ws, "~/%s" % name)


def test_excluded_glob_class_with_no_real_entry_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"].append({"path": "~/[priv]", "reason": "r"})
    ws.write_registry()
    assert not (tmp_path / "[priv]").exists()
    glob_refused(ws, "~/[priv]")


def test_excluded_star_name_that_exists_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    (tmp_path / "pr*v").mkdir()
    ws.document["excluded"].append({"path": "~/pr*v", "reason": "r"})
    ws.write_registry()
    glob_refused(ws, "~/pr*v")


def test_a_relative_excluded_entry_is_unknown_and_nothing_is_written(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"].append({"path": "private", "reason": "r"})
    ws.write_registry()
    before = contents(tmp_path)
    for expect in (None, "0" * 64):
        code, out = run(ws, expect=expect)
        assert code == 2, out
        assert "UNKNOWN  excluded path `private` is not an absolute path" in out, out
    assert contents(tmp_path) == before


# --------------------------------------------------------------------------
# G. The per-host editor file and the host
# --------------------------------------------------------------------------

def two_hosts(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["code_roots"] = {"desk": "~/src", "laptop": "~/lap"}
    ws.document["projects"][0]["paths"]["laptop"] = "~/lap/shop-web"
    ws.document["projects"][2]["paths"]["laptop"] = "~/lap/legacy"
    ws.write_registry()
    return ws


def test_editor_file_lists_this_hosts_non_archived_projects(tmp_path, monkeypatch):
    ws = two_hosts(tmp_path, monkeypatch)
    generate(ws, host="laptop")
    data = json.loads(ws.read("laptop.code-workspace"))
    assert data["folders"] == [{"name": "shop-web", "path": str(tmp_path / "lap" / "shop-web")}]


def test_no_host_with_two_hosts_is_unknown(tmp_path, monkeypatch):
    ws = two_hosts(tmp_path, monkeypatch)
    code, out = run(ws, host=None)
    assert code == 2, out
    assert ("UNKNOWN  no --host given and code_roots names more than one host (desk, laptop); "
            "pass --host") in out, out


def test_unknown_host_is_unknown_with_the_scan_text(tmp_path, monkeypatch):
    ws = two_hosts(tmp_path, monkeypatch)
    before = contents(ws.path)
    code, out = run(ws, host="../x")
    assert code == 2, out
    assert "UNKNOWN  host `../x` is not in code_roots (known: desk, laptop); pass --host" in out
    assert contents(ws.path) == before


def test_host_key_that_is_not_a_file_name_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["code_roots"] = {"../x": "~/src"}
    ws.document["projects"] = []
    ws.write_registry()
    before = contents(tmp_path)
    refused(*run(ws, host="../x"), "code_roots host `../x` is not a plain file name")
    assert contents(tmp_path) == before


def test_project_under_an_excluded_path_is_refused(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["excluded"].append({"path": "~/src/ingest", "reason": "r"})
    ws.write_registry()
    refused(*run(ws), "project `ingest` path ~/src/ingest (host `desk`) is under an excluded path")


def test_subpath_joins_the_folder(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["projects"][0]["subpath"] = "pkg/a/"
    ws.write_registry()
    generate(ws)
    data = json.loads(ws.read("desk.code-workspace"))
    assert data["folders"][0]["path"] == str(tmp_path / "src" / "shop-web" / "pkg" / "a")


def test_readme_says_to_ignore_the_per_host_files():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "*.code-workspace" in readme and "state/" in readme


# --------------------------------------------------------------------------
# K. The README's promises and the template
# --------------------------------------------------------------------------

UT4_PROMISE = (
    "`scan` gives a verdict only for a repository in the plain shape it can read safely: a `.git` "
    "folder, or a worktree whose `.git` file names `<repo>/.git/worktrees/<name>` for a repository "
    "found directly in the code root or registered, reached from the code root without a symlink, "
    "with a single plain `config` file that has no include section and does not turn on "
    "per-worktree config. Anything else it meets (a symlink, an odd git folder layout, an include, "
    "per-worktree config, an excluded entry reached by another name, a file that changes while it "
    "is read) ends the scan with exit 2 and names the reason. It never reads beneath an excluded "
    "path, outside the code root, or inside a repository beyond its git files, and `git` only ever "
    "parses config bytes handed to it on standard input."
)

EDGES_PROMISE = (
    "The paragraph above is the promise of `scan`. `scan --edges` keeps it with two differences, "
    "both only inside registered repositories that are not archived. First, it also lists and "
    "reads source files there, walking each step from the repository the same way, judged by text "
    "and by identity against every excluded entry, never through a symlink, never into a nested "
    "repository, and never opening a file whose name starts with `.env`. Second, what it meets "
    "there does not end the scan with exit 2: a symlink or an excluded entry reached by another "
    "name is not followed or entered and is counted on one `INFO` line, and a file or folder that "
    "cannot be read safely, including one that changes while it is read, is not read and is named "
    "on a `WARN` line. Every repository-shape check, and everything outside those repositories, "
    "still ends the scan with exit 2 as above. It prints a candidate edge as a file path and line "
    "number, never the line. `gen`, `register` and `decline` print a plan and a plan hash and write "
    "nothing; run again with `--write --expect <hash>` to write exactly that plan. `gen` writes the "
    "map, the `CLAUDE.md` pointer, `.claude/settings.json` (only the code roots and the deny rules "
    "for excluded paths, and, only if you turn it on, one SessionStart hook that runs "
    "`workspace prompt`, never a `sandbox` block), the per-host `.code-workspace` file and its own "
    "manifest, and what it writes always passes `check`. `register` and `decline` change only "
    "`registry.yaml`. Nothing here opens a network connection."
)


def readme_section(title):
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("## " + title + "\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def test_readme_machine_section_names_what_it_touches():
    section = readme_section("What it does on your machine")
    for needle in ("code root", ".env", "~/rooms", "AGENTS.md", "CLAUDE.md",
                   ".claude/settings.json", ".code-workspace", ".workspace-gen.json",
                   "state/gen-<host>.json", "registry.yaml", "--write", "sandbox", "--edges"):
        assert needle in section, needle
    assert "another agent" in section.lower()


def test_readme_keeps_the_scan_promise_and_scopes_it():
    paragraphs = (REPO_ROOT / "README.md").read_text(encoding="utf-8").split("\n\n")
    assert UT4_PROMISE in paragraphs
    assert paragraphs[paragraphs.index(UT4_PROMISE) + 1] == EDGES_PROMISE
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for gone in ("two verbs, both read-only", "Later verbs", "**What it writes.** Nothing.",
                 "not in this release", "keeps every rule in it but one", "one per edge"):
        assert gone not in readme, gone
    assert "Without `--edges`, the contents of a repository" in readme
    assert "`register` do read source files inside repositories" in readme
    known_limits = " ".join(readme_section("Known limits").split())
    assert "so a family of folders is listed one by one" in known_limits
    assert "`~/src/app/priv-*` is refused with exit 2" in known_limits
    assert "A symlink inside the tree of a folder `gen` writes" in known_limits
    assert "by its text, not by its identity" not in known_limits
    assert "bind mount" not in (REPO_ROOT / "README.md").read_text(encoding="utf-8").replace(
        "a bind mount is not claimed", "")


def test_template_registry_follows_the_grammar():
    folder = REPO_ROOT / "docs" / "template"
    assert sorted(os.listdir(folder)) == ["README.md", "registry.yaml"]
    document = check.load_registry_yaml((folder / "registry.yaml").read_text(encoding="utf-8"))
    scan.validate(document)
    guide = (folder / "README.md").read_text(encoding="utf-8")
    assert "workspace gen" in guide and "workspace register" in guide
    assert "*.code-workspace" in guide and "workspace:keep" in guide


# --------------------------------------------------------------------------
# W. The write-path rule: one O_NOFOLLOW, O_NONBLOCK read of a regular file;
#    the mode comes only from that read
# --------------------------------------------------------------------------

OUTPUT_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", "desk.code-workspace",
                ".workspace-gen.json", "state/gen-desk.json")


def private_file(tmp_path, name, text):
    """A 0600 file outside the workspace, beneath the excluded ~/private."""
    folder = tmp_path / "private"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("rel", OUTPUT_FILES)
def test_a_linked_output_file_is_refused_and_never_followed(tmp_path, monkeypatch, umask_022,
                                                           rel):
    ws = WS(tmp_path, monkeypatch)
    secret = private_file(tmp_path, "secret.json", json.dumps(CREDENTIAL))
    link = ws.file(rel)
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, link)
    before = contents(tmp_path)
    for flags, expect in (((), None), (("--adopt",), None), (("--adopt",), "0" * 64)):
        code, out = run(ws, *flags, expect=expect)
        refused(code, out, scan.NOT_REGULAR % rel)
        assert "SENTINEL" not in out, out
    assert contents(tmp_path) == before
    assert os.readlink(link) == str(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


@pytest.mark.parametrize("verb", ["gen", "register", "decline"])
def test_a_linked_registry_is_refused_for_every_writing_verb(tmp_path, monkeypatch, verb):
    ws = WS(tmp_path, monkeypatch)
    secret = private_file(tmp_path, "registry-secret",
                          "REGISTRY_SENTINEL: [this is not yaml\n")
    ws.file("registry.yaml").unlink()
    os.symlink(secret, ws.file("registry.yaml"))
    before = contents(tmp_path)
    args = {"gen": ["gen", str(ws.path)],
            "register": ["register", str(ws.path), "~/src/x", "--cluster", "shop",
                         "--one-liner", "x"],
            "decline": ["decline", str(ws.path), "pat", "--reason", "r"]}[verb]
    for extra in ([], ["--write", "--expect", "0" * 64]):
        code, out = run_cli(*args, *extra)
        assert code == 1, out
        assert "REFUSED  " + scan.NOT_REGULAR % "registry.yaml" in out, out
        assert "SENTINEL" not in out, out
    assert contents(tmp_path) == before
    assert os.readlink(ws.file("registry.yaml")) == str(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_a_fifo_at_the_map_is_refused_not_waited_on(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    os.mkfifo(ws.file("AGENTS.md"))
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run([sys.executable, "-P", "-m", "workspace", "gen", str(ws.path),
                             "--adopt"], capture_output=True, text=True, env=env, check=False,
                            timeout=60)
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "REFUSED  " + scan.NOT_REGULAR % "AGENTS.md" in out, out
    assert stat.S_ISFIFO(os.lstat(ws.file("AGENTS.md")).st_mode)
    assert sorted(os.listdir(ws.path)) == ["AGENTS.md", "registry.yaml"]


def swapped_after_the_plan(tmp_path, monkeypatch):
    """A hand-kept map, a plan that adopts it, and the map's bytes outside the workspace."""
    ws = WS(tmp_path, monkeypatch)
    hand = keep_text(top="mine\n")
    ws.write("AGENTS.md", hand)
    plan = gen.make_plan(str(ws.path), HOST, True)
    return ws, hand, plan


def test_a_target_swapped_for_a_link_after_the_plan_is_refused_and_undone(tmp_path,
                                                                          monkeypatch):
    ws, hand, plan = swapped_after_the_plan(tmp_path, monkeypatch)
    copy = private_file(tmp_path, "AGENTS.md", hand)
    ws.file("AGENTS.md").unlink()
    os.symlink(copy, ws.file("AGENTS.md"))
    buffer = io.StringIO()
    code = gen.write_plan(str(ws.path), plan, buffer)
    out = buffer.getvalue()
    assert code == 1, out
    assert "REFUSED  could not write: " + scan.NOT_REGULAR % "AGENTS.md" in out, out
    # No temp left, and the .claude and state folders the write made are gone.
    assert sorted(os.listdir(ws.path)) == ["AGENTS.md", "registry.yaml"]
    assert os.readlink(ws.file("AGENTS.md")) == str(copy)
    assert copy.read_text(encoding="utf-8") == hand
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_a_target_swapped_for_a_copy_with_the_same_bytes_is_refused(tmp_path, monkeypatch):
    ws, hand, plan = swapped_after_the_plan(tmp_path, monkeypatch)
    swap = ws.file("swap.tmp")
    swap.write_text(hand, encoding="utf-8")
    swap.chmod(stat.S_IMODE(ws.file("AGENTS.md").stat().st_mode))
    os.replace(swap, ws.file("AGENTS.md"))
    buffer = io.StringIO()
    code = gen.write_plan(str(ws.path), plan, buffer)
    refused(code, buffer.getvalue(), "AGENTS.md changed during the run")
    assert sorted(os.listdir(ws.path)) == ["AGENTS.md", "registry.yaml"]
    assert ws.read("AGENTS.md") == hand


def test_a_target_swapped_just_before_its_rename_is_not_replaced(tmp_path, monkeypatch):
    ws, hand, plan = swapped_after_the_plan(tmp_path, monkeypatch)
    original = gen.write_atomic
    calls = []

    def swapping(folder, name, *rest):
        # After the recheck: the pending manifest is the first write, before any rename.
        identity = original(folder, name, *rest)
        calls.append(name)
        if len(calls) == 1:
            swap = ws.file("swap.tmp")
            swap.write_text(hand, encoding="utf-8")
            os.replace(swap, ws.file("AGENTS.md"))
        return identity

    monkeypatch.setattr(gen, "write_atomic", swapping)
    buffer = io.StringIO()
    code = gen.write_plan(str(ws.path), plan, buffer)
    out = buffer.getvalue()
    assert code == 2, out
    assert "AGENTS.md changed since the plan read it" in out, out
    assert ws.read("AGENTS.md") == hand
    leftovers = [name for name in contents(ws.path) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []


def test_a_keep_block_edited_in_place_just_before_its_rename_is_not_overwritten(
        tmp_path, monkeypatch):
    ws, hand, plan = swapped_after_the_plan(tmp_path, monkeypatch)
    before = os.stat(ws.file("AGENTS.md"))
    original = gen.write_atomic
    calls = []

    def editing(folder, name, *rest):
        # After the recheck: the pending manifest is the first write, before any rename.
        result = original(folder, name, *rest)
        calls.append(name)
        if len(calls) == 1:
            with open(ws.file("AGENTS.md"), "r+b") as handle:
                data = handle.read()
                handle.seek(0)
                handle.write(data.replace(b"mine\n", b"MINE\n"))
            edited = os.stat(ws.file("AGENTS.md"))
            # The same file, edited in place: same inode, same mode.
            assert (edited.st_ino, edited.st_mode) == (before.st_ino, before.st_mode)
        return result

    monkeypatch.setattr(gen, "write_atomic", editing)
    buffer = io.StringIO()
    code = gen.write_plan(str(ws.path), plan, buffer)
    out = buffer.getvalue()
    assert code == 2, out
    assert "AGENTS.md changed since the plan read it" in out, out
    assert ws.read("AGENTS.md") == hand.replace("mine\n", "MINE\n")
    leftovers = [name for name in contents(ws.path) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []


def test_a_stop_at_the_first_record_removes_the_folders_it_made(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    before = contents(ws.path)
    code, out = run(ws)
    with monkeypatch.context() as patch:
        Fault(patch, gen.MANIFEST_NAME, nth=1)
        code, out = run(ws, expect=plan_hash(out))
    assert code == 2, out
    assert contents(ws.path) == before, sorted(contents(ws.path))
    generate(ws)


def test_a_stop_after_the_pending_record_keeps_the_folder_that_holds_it(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    code, out = run(ws)
    with monkeypatch.context() as patch:
        Fault(patch, gen.MANIFEST_NAME, nth=2)
        code, out = run(ws, expect=plan_hash(out))
    assert code == 2, out
    assert "the write stopped part way" in out, out
    assert "pending" in ws.read("state/gen-desk.json")
    leftovers = [name for name in contents(ws.path) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []
    generate(ws)
    assert "pending" not in ws.read(".workspace-gen.json")


def test_a_new_map_takes_the_mode_of_its_keep_source(tmp_path, monkeypatch, umask_022):
    ws = WS(tmp_path, monkeypatch)
    ws.write("CLAUDE.md", keep_text(top="private notes\n"))
    ws.file("CLAUDE.md").chmod(0o600)
    written = generate(ws, "--adopt")
    assert "wrote  AGENTS.md" in written, written
    assert "private notes\n" in ws.read("AGENTS.md")
    assert stat.S_IMODE(ws.file("AGENTS.md").stat().st_mode) == 0o600
    assert stat.S_IMODE(ws.file("CLAUDE.md").stat().st_mode) == 0o600


def test_gen_through_a_symlinked_workspace_argument_writes(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    os.symlink(ws.path, alias)
    buffer = io.StringIO()
    code = gen.main(str(alias), host=HOST, out=buffer)
    assert code == 0, buffer.getvalue()
    written = io.StringIO()
    code = gen.main(str(alias), host=HOST, write=True, expect=plan_hash(buffer.getvalue()),
                    out=written)
    assert code == 0, written.getvalue()
    for rel in OUTPUT_FILES:
        assert ws.file(rel).is_file() and not ws.file(rel).is_symlink(), rel
    check_passes(ws)


# Each file call gen.py and register.py may make, and the only functions that
# make it. Every workspace file is read through scan.snapshot_at (via
# gen.read_snapshot or scan.registry_snapshot), never by these modules.
FILE_CALLS = {
    "gen.py": {
        "os.open": {"open_folder", "make_folder", "write_temp"},
        "os.fdopen": {"write_temp"},
        "os.lstat": {"output_folder_gate"},
        "os.listdir": {"remove_leftover_temps"},
    },
    "register.py": {},
}
WATCHED = ("open", "io.open", "os.open", "os.fdopen", "os.stat", "os.lstat", "os.listdir",
           "os.scandir", "os.path.isfile", "os.path.isdir", "os.path.exists",
           "os.path.islink", "os.path.getsize")
WATCHED_METHODS = ("open", "read_bytes", "read_text", "write_bytes", "write_text")


def file_calls(source):
    """(call, enclosing function) for every watched file call in `source`."""
    found = []

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return None if base is None else base + "." + node.attr
        return None

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name in WATCHED:
                found.append((name, function))
            elif (isinstance(node.func, ast.Attribute) and node.func.attr in WATCHED_METHODS
                  and not (name or "").startswith("os.")):
                found.append(("." + node.func.attr, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), None)
    return found


def test_workspace_files_are_read_only_through_the_snapshot_helpers():
    for module, allowed in FILE_CALLS.items():
        source = (REPO_ROOT / "workspace" / module).read_text(encoding="utf-8")
        for call, function in file_calls(source):
            assert function in allowed.get(call, set()), (
                "%s calls %s in %s; read a workspace file through the snapshot reader"
                % (module, call, function))
    assert scan.WORKSPACE_FILE_FLAGS & os.O_NOFOLLOW
    assert scan.WORKSPACE_FILE_FLAGS & os.O_NONBLOCK
