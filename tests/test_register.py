"""`workspace register` and `workspace decline`: drafting registry entries, on a confirm.

Every fixture is built in a temp dir with synthetic names; HOME is the temp
dir, so `~/src` is the code root:

    src/shop-web/, src/ingest/   registered repos
    src/new/                     an unregistered repo with an origin remote
    rooms/                       excluded
    ws/registry.yaml             written as text, with comments, so splices are byte-checked
"""
import datetime
import fcntl
import io
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from workspace import check, gen, register, scan
from test_scan import REPO_ROOT, Trace, fold_case, git, make_repo, reaches

TODAY = datetime.date(2026, 11, 18)
REMOTE = "https://example.invalid/acme/%s.git"

REGISTRY = """\
# Fixture registry: every name is invented.
system: Fixture
code_roots:
  desk: ~/src

excluded:
  - path: ~/rooms
    reason: live personal data

clusters:
  - id: shop
    name: Shop
    owner: web team
    lifecycle: active
    one_liner: Selling things.

projects:
  - name: shop-web
    remote: https://example.invalid/acme/shop-web.git
    paths: {desk: ~/src/shop-web}
    subpath: null
    one_liner: Storefront.
    owner: web team
    owned_or_reference: owned
    tier: project
    cluster: shop
    lifecycle: active
    doc: null
    confused_with: null
  - name: ingest
    remote: https://example.invalid/acme/ingest.git
    paths: {desk: ~/src/ingest}
    subpath: null
    one_liner: Nightly ingest.
    owner: web team
    owned_or_reference: owned
    tier: project
    cluster: shop
    lifecycle: active
    doc: null
    confused_with: null

edges:
  - {from: shop-web, to: ingest, kind: runtime, evidence: polls it, approved_at: 2026-10-01}

# Keep explicit decisions so discovery does not propose them again.
declined: []
rejected_edges: []
"""


class Reg:
    def __init__(self, base, monkeypatch, text=REGISTRY):
        monkeypatch.setenv("HOME", str(base))
        monkeypatch.setattr(register, "today", lambda: TODAY)
        self.base = base
        self.root = base / "src"
        self.ws = base / "ws"
        self.root.mkdir()
        (base / "rooms").mkdir()
        for name in ("shop-web", "ingest"):
            make_repo(self.root / name, REMOTE % name)
        make_repo(self.root / "new", REMOTE % "new")
        self.ws.mkdir()
        self.write(text)

    def write(self, text):
        (self.ws / "registry.yaml").write_text(text, encoding="utf-8")

    def text(self):
        return (self.ws / "registry.yaml").read_text(encoding="utf-8")

    def put(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def request(ws, path, cluster="shop", one_liner="A new thing.", name=None, lifecycle="active",
            host=None, expect=None):
    return {"workspace": str(ws), "path": path, "cluster": cluster, "one_liner": one_liner,
            "name": name, "lifecycle": lifecycle, "host": host, "write": expect is not None,
            "expect": expect}


def run_register(reg, path, **options):
    buffer = io.StringIO()
    code = register.register_main(request(reg.ws, path, **options), out=buffer)
    return code, buffer.getvalue()


def run_decline(reg, pattern, reason="not ours", expect=None):
    buffer = io.StringIO()
    code = register.decline_main({"workspace": str(reg.ws), "pattern": pattern, "reason": reason,
                                  "write": expect is not None, "expect": expect}, out=buffer)
    return code, buffer.getvalue()


def plan_hash(out):
    found = re.findall(r"^plan sha256=([0-9a-f]{64})$", out, re.M)
    assert len(found) == 1, out
    return found[0]


def registered(reg, path, **options):
    code, out = run_register(reg, path, **options)
    assert code == 0, out
    code, written = run_register(reg, path, expect=plan_hash(out), **options)
    assert code == 0, written
    return written


def refused(code, out, text):
    assert code == 1, out
    assert "REFUSED  " + text in out, "expected %r in:\n%s" % (text, out)


def load(reg):
    return check.load_registry_yaml(reg.text())


def run_scan(reg):
    buffer = io.StringIO()
    code = scan.main(str(reg.ws), None, out=buffer)
    return code, buffer.getvalue()


def traced(monkeypatch, call):
    trace = Trace()
    with monkeypatch.context() as patch:
        for module, name in Trace.FUNCTIONS:
            patch.setattr(module, name, trace.wrap(name, getattr(module, name)))
        result = call()
    return trace, result


NEW_ENTRY = """\
  - name: "new"
    remote: "https://example.invalid/acme/new.git"
    paths: {"desk": "~/src/new"}
    subpath: null
    one_liner: "A new thing."
    owner: "web team"
    owned_or_reference: "owned"
    tier: "project"
    cluster: "shop"
    lifecycle: "active"
    doc: null
    confused_with: null
"""


# --------------------------------------------------------------------------
# The plan, the confirm and the splice (W1-W4, R1, R2)
# --------------------------------------------------------------------------

def test_dry_run_prints_the_entry_and_writes_nothing(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    before = reg.text()
    code, out = run_register(reg, "~/src/new")
    assert code == 0, out
    assert '+  - name: "new"' in out, out
    assert '+  - from: "new"' in out and 'evidence: "run.sh:1"' in out, out
    plan_hash(out)
    assert reg.text() == before


def test_write_appends_the_entry_and_keeps_every_other_byte(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.ws / "AGENTS.md").write_text("map\n", encoding="utf-8")
    written = registered(reg, "~/src/new")
    assert "run: workspace gen %s" % reg.ws in written, written
    marker = "    confused_with: null\n\nedges:"
    assert reg.text() == REGISTRY.replace(
        marker, "    confused_with: null\n" + NEW_ENTRY + "\nedges:", 1)
    assert (reg.ws / "AGENTS.md").read_text(encoding="utf-8") == "map\n"
    scan.validate(load(reg))


def test_worktree_registers_its_parent(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    git("-C", reg.root / "new", "commit", "-q", "--allow-empty", "-m", "fixture")
    git("-C", reg.root / "new", "worktree", "add", "-q", reg.root / "new-wt")
    registered(reg, "~/src/new-wt")
    entry = load(reg)["projects"][-1]
    assert entry["name"] == "new" and entry["paths"] == {"desk": "~/src/new"}, entry
    assert entry["remote"] == REMOTE % "new"


def test_worktree_of_an_unknown_repo_below_the_root_is_unknown(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    deep = reg.root / "deep" / "new2"
    make_repo(deep, REMOTE % "new2", commit=True)
    git("-C", deep, "worktree", "add", "-q", reg.root / "new2-wt")
    before = reg.text()
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/new2-wt"))
    assert code == 2, out
    assert scan.NOT_WORKTREE in out, out
    gitdir = deep / ".git" / "worktrees" / "new2-wt"
    assert trace.opened(gitdir) == [], trace.opened(gitdir)
    assert reg.text() == before


def test_remote_already_registered_in_another_form_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "fork", "git@example.invalid:acme/shop-web.git")
    refused(*run_register(reg, "~/src/fork"),
            "remote example.invalid/acme/shop-web is already registered as project `shop-web`")


def test_registered_in_another_workspace_is_not_a_refusal(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    other = tmp_path / "ws2"
    other.mkdir()
    (other / "registry.yaml").write_text(REGISTRY.replace("~/src/ingest", "~/src/new")
                                         .replace("acme/ingest", "acme/new"), encoding="utf-8")
    code, out = run_register(reg, "~/src/new")
    assert code == 0, out


def test_name_collision_suggests_a_name_and_a_new_name_is_flagged(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "forks" / "shop-web", REMOTE % "shop-web-fork")
    refused(*run_register(reg, "~/src/forks/shop-web"),
            "project name `shop-web` is already in this registry; pass --name")
    code, out = run_register(reg, "~/src/forks/shop-web", name="shop-web-fork")
    assert code == 0, out
    assert "NOTE   look-alike: `shop-web` (same folder name)" in out, out
    assert '+  - name: "shop-web-fork"' in out, out


def test_underscore_variant_is_flagged(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "shop_web", REMOTE % "shop_web")
    code, out = run_register(reg, "~/src/shop_web")
    assert code == 0, out
    assert "NOTE   look-alike: `shop-web` (a -/_ variant)" in out, out


def test_registered_path_is_refused_as_already_registered(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src/shop-web"),
            "%s is already registered for host `desk` as project `shop-web`"
            % (reg.root / "shop-web"))


@pytest.mark.parametrize("one_liner, text", [
    ("x" * 161, "the one-liner is 161 characters, over the limit of 160"),
    ("two\nlines", "the one-liner must be one line"),
])
def test_one_liner_limits_are_refused(tmp_path, monkeypatch, one_liner, text):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src/new", one_liner=one_liner), text)


def test_path_that_is_a_file_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.root / "notes.txt").write_text("x\n", encoding="utf-8")
    refused(*run_register(reg, "~/src/notes.txt"),
            "path ~/src/notes.txt is not a folder; not registered")


def test_missing_registry_is_unknown(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.ws / "registry.yaml").unlink()
    code, out = run_register(reg, "~/src/new")
    assert code == 2, out
    assert "UNKNOWN  could not read registry: " in out, out


def test_unknown_cluster_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src/new", cluster="nope"), "cluster `nope` is not in clusters")


# --------------------------------------------------------------------------
# The path gate (G-R1)
# --------------------------------------------------------------------------

def test_path_outside_the_code_root_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(tmp_path / "elsewhere" / "x", REMOTE % "x")
    refused(*run_register(reg, "~/elsewhere/x"),
            "path ~/elsewhere/x, which is outside the code root; not read")


def test_path_under_an_excluded_glob_is_unknown_before_anything_is_read(tmp_path, monkeypatch):
    # `~/src/priv-*` is a pattern, not an excluded path: the registry is refused.
    text = REGISTRY.replace("  - path: ~/rooms\n", "  - path: ~/src/priv-*\n    reason: r\n"
                            "  - path: ~/rooms\n")
    reg = Reg(tmp_path, monkeypatch, text)
    make_repo(reg.root / "priv-repo", REMOTE % "priv-repo")
    before = reg.text()
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/priv-repo"))
    assert code == 2, out
    assert "UNKNOWN  " + scan.EXCLUDED_GLOB % "~/src/priv-*" in out.splitlines(), out
    assert trace.under(reg.root / "priv-repo") == [], trace.under(reg.root / "priv-repo")
    assert reg.text() == before


def test_symlink_into_an_excluded_repo_is_refused_and_not_followed(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    secret = tmp_path / "rooms" / "secret"
    make_repo(secret, REMOTE % "secret")
    (reg.root / "innocent").symlink_to(secret)
    before = reg.text()
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/innocent"))
    refused(code, out, "path ~/src/innocent, which passes through the symlink %s; not followed"
            % (reg.root / "innocent"))
    assert str(reg.root / "innocent" / ".git") not in trace.paths
    assert trace.beneath(secret) == [], trace.beneath(secret)
    assert reg.text() == before


def test_excluded_repo_in_another_letter_case_is_refused_by_identity(tmp_path, monkeypatch):
    text = REGISTRY.replace("  - path: ~/rooms\n", "  - path: ~/src/Secret\n    reason: r\n"
                            "  - path: ~/rooms\n")
    reg = Reg(tmp_path, monkeypatch, text)
    make_repo(reg.root / "secret", REMOTE % "secret")
    fold_case(monkeypatch, reg.root, "secret")
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/secret"))
    refused(code, out, "path ~/src/secret, which is excluded; not read")
    assert trace.beneath(reg.root / "secret") == []


def test_repo_is_identified_before_the_edge_walk_lists_it(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    new = str(reg.root / "new")
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/new"))
    assert code == 0, out
    opens = [path for name, path in trace.calls if name == "open"]
    config = opens.index(os.path.join(new, ".git", "config"))
    listing = opens.index(new)
    assert config < listing, opens


def test_folder_that_is_not_a_repo_is_unknown(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.root / "plain").mkdir()
    code, out = run_register(reg, "~/src/plain")
    assert code == 2, out
    assert "UNKNOWN  path ~/src/plain is not a git repository (no .git)" in out, out


def test_repo_without_an_origin_is_unknown(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "lonely")
    code, out = run_register(reg, "~/src/lonely")
    assert code == 2, out
    assert "has no origin remote" in out, out


@pytest.mark.parametrize("extra, reason", [
    ("[include]\n\tpath = ../../rooms/leak\n", "git config includes are not followed"),
    ("[extensions]\n\tworktreeConfig = true\n", "per-worktree config is not supported"),
])
def test_identify_refusals_are_unknown_never_a_draft(tmp_path, monkeypatch, extra, reason):
    reg = Reg(tmp_path, monkeypatch)
    with open(reg.root / "new" / ".git" / "config", "a", encoding="utf-8") as handle:
        handle.write(extra)
    leak = tmp_path / "rooms" / "leak"
    leak.write_text("[remote \"origin\"]\n\turl = x\n", encoding="utf-8")
    before = reg.text()
    trace, (code, out) = traced(monkeypatch, lambda: run_register(reg, "~/src/new"))
    assert code == 2, out
    assert reason in out, out
    assert trace.under(leak) == []
    assert reg.text() == before


def test_declined_path_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace(
        "declined: []", "declined:\n  - {remote: ~/src/ne*, date: 2026-10-01, reason: r}"))
    refused(*run_register(reg, "~/src/new"), "%s matches a declined pattern" % (reg.root / "new"))


def test_the_code_root_itself_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src"), "path ~/src is the code root; not registered")


def test_path_inside_an_unregistered_root_repo_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "outer", REMOTE % "outer")
    make_repo(reg.root / "outer" / "inner", REMOTE % "inner")
    before = reg.text()
    refused(*run_register(reg, "~/src/outer/inner"),
            "path ~/src/outer/inner, which is inside repo %s; not read" % (reg.root / "outer"))
    assert reg.text() == before


def nested_registered(tmp_path, monkeypatch, extra=()):
    names = ("c",) + tuple(extra)
    entries = "".join(
        "  - name: %s\n    remote: %s\n    paths: {desk: ~/src/a/b/%s}\n    lifecycle: active\n"
        % (name, REMOTE % name, name) for name in names)
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace("\nedges:", entries + "\nedges:", 1))
    make_repo(reg.root / "a" / "b", REMOTE % "b")
    for name in names:
        make_repo(reg.root / "a" / "b" / name, REMOTE % name)
    return reg


def test_path_holding_a_registered_path_is_refused(tmp_path, monkeypatch):
    reg = nested_registered(tmp_path, monkeypatch)
    scan_code, scan_out = run_scan(reg)
    assert scan_code != 2, scan_out
    before = reg.text()
    code, out = run_register(reg, "~/src/a/b")
    refused(code, out, "path ~/src/a/b, which holds the registered path %s; not registered"
            % (reg.root / "a" / "b" / "c"))
    code, out = run_register(reg, "~/src/a/b", expect="0" * 64)
    assert code == 1, out
    assert reg.text() == before
    assert run_scan(reg)[0] == scan_code


def test_every_held_registered_path_is_named_in_order(tmp_path, monkeypatch):
    reg = nested_registered(tmp_path, monkeypatch, extra=("d",))
    refused(*run_register(reg, "~/src/a/b"),
            "path ~/src/a/b, which holds the registered paths %s, %s; not registered"
            % (reg.root / "a" / "b" / "c", reg.root / "a" / "b" / "d"))


def test_plain_folder_at_depth_one_holding_a_registered_path_is_refused(tmp_path, monkeypatch):
    reg = nested_registered(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src/a"),
            "path ~/src/a, which holds the registered path %s; not registered"
            % (reg.root / "a" / "b" / "c"))


@pytest.mark.parametrize("shape", ["root-repo", "depth-2", "worktree", "inside-root-repo",
                                   "holds-registered"])
def test_register_never_makes_the_next_scan_unknown(tmp_path, monkeypatch, shape):
    reg = nested_registered(tmp_path, monkeypatch) if shape == "holds-registered" else Reg(
        tmp_path, monkeypatch)
    if shape == "root-repo":
        path = "~/src/new"
    elif shape == "depth-2":
        make_repo(reg.root / "team" / "deep", REMOTE % "deep")
        path = "~/src/team/deep"
    elif shape == "worktree":
        git("-C", reg.root / "new", "commit", "-q", "--allow-empty", "-m", "fixture")
        git("-C", reg.root / "new", "worktree", "add", "-q", reg.root / "new-wt")
        path = "~/src/new-wt"
    elif shape == "inside-root-repo":
        make_repo(reg.root / "new" / "inner", REMOTE % "inner")
        path = "~/src/new/inner"
    else:
        path = "~/src/a/b"
    before_code, _ = run_scan(reg)
    before = reg.text()
    code, out = run_register(reg, path)
    if code == 0:
        code, out = run_register(reg, path, expect=plan_hash(out))
        assert code == 0, out
        scan_code, scan_out = run_scan(reg)
        assert scan_code != 2, scan_out
        new = os.path.expanduser(path)
        bad = [line for line in scan_out.splitlines()
               if line.startswith(("UNKNOWN", "DRIFT")) and new in line]
        assert bad == [], scan_out
    else:
        assert code == 1, out
        assert reg.text() == before
        assert run_scan(reg)[0] == before_code
    assert (code == 0) == (shape in ("root-repo", "depth-2", "worktree")), (shape, out)


# --------------------------------------------------------------------------
# The splice (R1, R2) and the drafted edges
# --------------------------------------------------------------------------

def test_empty_flow_edges_becomes_a_block(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace(
        "edges:\n  - {from: shop-web, to: ingest, kind: runtime, evidence: polls it, "
        "approved_at: 2026-10-01}\n", "edges: []\n"))
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/new")
    assert ('edges:\n  - from: "new"\n    to: "shop-web"\n    kind: "source"\n'
            '    evidence: "run.sh:1"\n    approved_at: 2026-11-18\n') in reg.text()
    assert load(reg)["edges"] == [{"from": "new", "to": "shop-web", "kind": "source",
                                   "evidence": "run.sh:1", "approved_at": TODAY}]


def test_missing_edges_key_is_appended_at_the_end(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace(
        "edges:\n  - {from: shop-web, to: ingest, kind: runtime, evidence: polls it, "
        "approved_at: 2026-10-01}\n", ""))
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/new")
    assert reg.text().endswith('rejected_edges: []\n\nedges:\n  - from: "new"\n    to: "shop-web"\n'
                               '    kind: "source"\n    evidence: "run.sh:1"\n'
                               '    approved_at: 2026-11-18\n'), reg.text()


def test_new_edge_goes_after_the_last_item_not_after_the_comment(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/new")
    assert ("approved_at: 2026-10-01}\n  - from: \"new\"\n" in reg.text()), reg.text()
    assert ("approved_at: 2026-11-18\n\n# Keep explicit decisions so discovery does not propose "
            "them again.\ndeclined: []\n") in reg.text(), reg.text()


def test_four_space_items_keep_their_indent(tmp_path, monkeypatch):
    head, rest = REGISTRY.split("projects:\n", 1)
    block, tail = rest.split("\nedges:", 1)
    indented = "".join("  " + line if line.strip() else line
                       for line in block.splitlines(keepends=True))
    reg = Reg(tmp_path, monkeypatch, head + "projects:\n" + indented + "\nedges:" + tail)
    registered(reg, "~/src/new")
    assert '\n    - name: "new"\n      remote: ' in reg.text(), reg.text()
    scan.validate(load(reg))


def test_one_liner_with_yaml_characters_round_trips(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    registered(reg, "~/src/new", one_liner="Tool #1: *fast* & safe")
    assert load(reg)["projects"][-1]["one_liner"] == "Tool #1: *fast* & safe"


def test_non_ascii_one_liner_is_stored_as_ascii_and_round_trips(tmp_path, monkeypatch):
    one_liner = "Caf\u00e9 \U0001f600 \"quoted\" back\\slash"
    reg = Reg(tmp_path, monkeypatch)
    registered(reg, "~/src/new", one_liner=one_liner)
    assert reg.text().isascii(), reg.text()
    assert load(reg)["projects"][-1]["one_liner"] == one_liner


def test_nulls_and_dates_are_typed(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/new")
    document = load(reg)
    entry = document["projects"][-1]
    assert entry["subpath"] is None and entry["doc"] is None and entry["confused_with"] is None
    assert document["edges"][-1]["approved_at"] == TODAY
    assert type(document["edges"][-1]["approved_at"]) is datetime.date


def test_backtick_name_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_register(reg, "~/src/new", name="a`b"), "project name 'a`b' holds a backtick")


def test_splice_that_breaks_the_registry_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    original = register.render_item
    monkeypatch.setattr(register, "render_item",
                        lambda entry, indent: original(entry, indent).replace(
                            "    subpath: null\n", "    subpath: null\n    subpath: null\n"))
    before = reg.text()
    for expect in (None, "0" * 64):
        code, out = run_register(reg, "~/src/new", expect=expect)
        refused(code, out, "the spliced registry does not reparse")
    assert reg.text() == before


@pytest.mark.parametrize("fault, text", [
    (lambda item: item.replace("    tier: ", "    colour: \"blue\"\n    tier: "),
     "the spliced registry fails the grammar"),
    (lambda item: item.replace('one_liner: "A new thing."', 'one_liner: "Something else."'),
     "the spliced registry is not the old registry plus the new entries"),
])
def test_splice_that_changes_the_meaning_is_refused(tmp_path, monkeypatch, fault, text):
    reg = Reg(tmp_path, monkeypatch)
    original = register.render_item
    monkeypatch.setattr(register, "render_item",
                        lambda entry, indent: fault(original(entry, indent)))
    refused(*run_register(reg, "~/src/new"), text)
    assert reg.text() == REGISTRY


def test_non_empty_flow_list_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace(
        "edges:\n  - {from: shop-web, to: ingest, kind: runtime, evidence: polls it, "
        "approved_at: 2026-10-01}\n",
        "edges: [{from: shop-web, to: ingest, kind: runtime, evidence: x, approved_at: null}]\n"))
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    refused(*run_register(reg, "~/src/new"),
            "registry `edges` is written in flow form; write it as a block list first")


def test_projects_as_the_last_key_without_a_newline(tmp_path, monkeypatch):
    text = REGISTRY.split("\nedges:")[0].rstrip("\n")
    reg = Reg(tmp_path, monkeypatch, text)
    registered(reg, "~/src/new")
    assert reg.text() == text + "\n" + NEW_ENTRY, reg.text()


def test_rejected_pair_is_not_drafted(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch, REGISTRY.replace(
        "rejected_edges: []", "rejected_edges:\n  - {from: new, to: shop-web, date: 2026-10-01, "
        "reason: no}"))
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    code, out = run_register(reg, "~/src/new")
    assert code == 0, out
    assert '+  - from: "new"' not in out, out


def test_drafted_edges_carry_today_and_path_line_evidence_only(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/run.sh", "# header\nsource ~/src/shop-web/lib.sh # LINE_SENTINEL_42\n")
    reg.put("ingest/client.py", "NEW_URL = 'x'\n")
    code, out = run_register(reg, "~/src/new")
    assert "LINE_SENTINEL_42" not in out, out
    registered(reg, "~/src/new")
    edges = load(reg)["edges"][1:]
    assert edges == [
        {"from": "ingest", "to": "new", "kind": "runtime", "evidence": "client.py:1",
         "approved_at": TODAY},
        {"from": "new", "to": "shop-web", "kind": "source", "evidence": "run.sh:2",
         "approved_at": TODAY},
    ], edges
    assert "LINE_SENTINEL_42" not in reg.text()


def test_registry_changed_between_plan_and_write_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    code, out = run_register(reg, "~/src/new")
    reg.write(REGISTRY.replace("Storefront.", "Storefront!"))
    before = reg.text()
    refused(*run_register(reg, "~/src/new", expect=plan_hash(out)), "plan changed")
    assert reg.text() == before


def test_a_concurrent_registry_writer_is_not_overwritten(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    code, out = run_register(reg, "~/src/new")
    racer = REGISTRY.replace("Storefront.", "Storefront, edited meanwhile.")
    original = register.verified_splice

    def racing(text, document, additions):
        result = original(text, document, additions)
        reg.write(racer)
        return result

    monkeypatch.setattr(register, "verified_splice", racing)
    refused(*run_register(reg, "~/src/new", expect=plan_hash(out)), "plan changed")
    assert reg.text() == racer


def test_a_hash_that_shares_only_a_prefix_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    code, out = run_register(reg, "~/src/new")
    digest = plan_hash(out)
    near = digest[:8] + ("0" if digest[8] != "0" else "1") + digest[9:]
    refused(*run_register(reg, "~/src/new", expect=near), "plan changed")
    assert reg.text() == REGISTRY


def test_unknown_host_is_unknown_with_the_scan_text(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    code, out = run_register(reg, "~/src/new", host="../x")
    assert code == 2, out
    assert "UNKNOWN  host `../x` is not in code_roots (known: desk); pass --host" in out, out


def test_userinfo_is_not_written_to_the_registry(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "creds", "https://someone:TOKEN_SENTINEL@example.invalid/acme/creds.git")
    code, out = run_register(reg, "~/src/creds")
    assert code == 0, out
    assert "TOKEN_SENTINEL" not in out, out
    assert '"https://example.invalid/acme/creds.git"' in out, out


def test_cli_register_round_trip(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), HOME=str(tmp_path))
    args = [sys.executable, "-P", "-m", "workspace", "register", str(reg.ws), "~/src/new",
            "--cluster", "shop", "--one-liner", "A new thing."]
    dry = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    wet = subprocess.run(args + ["--write", "--expect", plan_hash(dry.stdout)],
                         capture_output=True, text=True, env=env, check=False)
    assert wet.returncode == 0, wet.stdout + wet.stderr
    assert load(reg)["projects"][-1]["name"] == "new"


# --------------------------------------------------------------------------
# decline (D1, D2)
# --------------------------------------------------------------------------

def declined_with(reg, pattern, reason="not ours"):
    code, out = run_decline(reg, pattern, reason)
    assert code == 0, out
    code, written = run_decline(reg, pattern, reason, expect=plan_hash(out))
    assert code == 0, written
    return written


def test_decline_turns_an_empty_flow_list_into_a_block(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    before = reg.text()
    code, out = run_decline(reg, "https://example.invalid/other/*")
    assert code == 0 and reg.text() == before, out
    declined_with(reg, "https://example.invalid/other/*", "r")
    assert reg.text() == REGISTRY.replace(
        "declined: []\n",
        'declined:\n  - remote: "https://example.invalid/other/*"\n    date: 2026-11-18\n'
        '    reason: "r"\n')


def test_declined_path_glob_is_stored_as_written_and_honoured(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    make_repo(reg.root / "tmp-x", REMOTE % "tmp-x")
    assert run_scan(reg)[0] == 1
    declined_with(reg, "~/src/tmp-*")
    assert load(reg)["declined"] == [{"remote": "~/src/tmp-*", "date": TODAY,
                                      "reason": "not ours"}]
    code, out = run_scan(reg)
    assert "INFO   declined repo at %s" % (reg.root / "tmp-x") in out, out
    assert "DRIFT  unregistered repo at %s " % (reg.root / "tmp-x") not in out, out


def test_decline_values_round_trip(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    declined_with(reg, "*/scratch-*", "see #12")
    assert load(reg)["declined"] == [{"remote": "*/scratch-*", "date": TODAY,
                                      "reason": "see #12"}]


@pytest.mark.parametrize("pattern, reason, text", [
    ("https://example.invalid/acme/shop-*", "r", "matches the registered project `shop-web`"),
    ("~/src/ing*", "r", "matches the registered project `ingest`"),
    ("https://example.invalid/acme/x", " ", "the reason is empty"),
])
def test_decline_refusals(tmp_path, monkeypatch, pattern, reason, text):
    reg = Reg(tmp_path, monkeypatch)
    refused(*run_decline(reg, pattern, reason), "pattern %s %s" % (pattern, text)
            if "matches" in text else text)
    assert reg.text() == REGISTRY


def test_decline_of_an_already_declined_pattern_is_refused(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    declined_with(reg, "~/src/tmp-*")
    refused(*run_decline(reg, "~/src/tmp-*"), "pattern ~/src/tmp-* is already declined")


def test_decline_writes_only_the_registry(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.ws / "AGENTS.md").write_text("map\n", encoding="utf-8")
    written = declined_with(reg, "~/src/tmp-*")
    assert "run: workspace gen %s" % reg.ws in written, written
    assert sorted(os.listdir(reg.ws)) == ["AGENTS.md", "registry.yaml"]
    assert (reg.ws / "AGENTS.md").read_text(encoding="utf-8") == "map\n"


def test_concurrent_confirmed_writes_never_lose_an_addition(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    patterns = {"A": "https://example.invalid/first/*", "B": "https://example.invalid/second/*"}
    digests = {}
    for name, pattern in patterns.items():
        code, out = run_decline(reg, pattern)
        assert code == 0, out
        digests[name] = plan_hash(out)
    b_may_run = threading.Event()
    b_at_lock = threading.Event()
    real_flock = fcntl.flock
    real_write = gen.write_atomic

    def flock(handle, operation):
        if threading.current_thread().name == "B":
            b_at_lock.set()
        return real_flock(handle, operation)

    def write_atomic(*args):
        if threading.current_thread().name == "A":
            # A has compared the registry bytes. B runs up to its lock (or, with
            # no lock, to its end) before A replaces the file.
            b_may_run.set()
            assert b_at_lock.wait(10)
        return real_write(*args)

    results = {}

    def confirm(name):
        if name == "B":
            b_may_run.wait(10)
        try:
            results[name] = run_decline(reg, patterns[name], expect=digests[name])
        finally:
            b_at_lock.set()

    monkeypatch.setattr(register.fcntl, "flock", flock)
    monkeypatch.setattr(register.gen, "write_atomic", write_atomic)
    threads = [threading.Thread(target=confirm, args=(name,), name=name) for name in "AB"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    declined = [entry["remote"] for entry in load(reg)["declined"]]
    for name in "AB":
        code, out = results[name]
        if code == 0:
            assert patterns[name] in declined, "%s reported success but its entry is lost:\n%s" % (
                name, reg.text())
    assert results["A"][0] == 0, results["A"][1]
    refused(*results["B"], register.PLAN_CHANGED)


def test_a_written_registry_keeps_its_mode(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    (reg.ws / "registry.yaml").chmod(0o600)
    old = os.umask(0o022)
    try:
        declined_with(reg, "~/src/tmp-*")
    finally:
        os.umask(old)
    assert stat.S_IMODE((reg.ws / "registry.yaml").stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# The write-path rule for registry.yaml, and raw evidence
# --------------------------------------------------------------------------

def test_registry_swapped_for_a_link_at_the_lock_is_not_written(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    registry = reg.ws / "registry.yaml"
    registry.chmod(0o600)
    code, out = run_decline(reg, "~/src/tmp-*")
    digest = plan_hash(out)
    moved = tmp_path / "rooms" / "registry.yaml"
    real_flock = fcntl.flock

    def flock(handle, operation):
        # Another writer swaps the name for a link to the same file once the lock is held.
        result = real_flock(handle, operation)
        if not os.path.islink(registry):
            os.replace(registry, moved)
            os.symlink(moved, registry)
        return result

    monkeypatch.setattr(register.fcntl, "flock", flock)
    temps = []
    real_write_temp = gen.write_temp

    def write_temp(*args):
        temps.append(args[1])
        return real_write_temp(*args)

    monkeypatch.setattr(gen, "write_temp", write_temp)
    refused(*run_decline(reg, "~/src/tmp-*", expect=digest), register.PLAN_CHANGED)
    # The check under the lock refuses before any write begins: no temp file is
    # made. write_atomic's own look-up would refuse too, but only after its temp.
    assert temps == []
    assert os.readlink(registry) == str(moved)
    assert moved.read_text(encoding="utf-8") == REGISTRY
    assert stat.S_IMODE(moved.stat().st_mode) == 0o600


def test_registry_swapped_after_the_locked_read_is_not_written(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    registry = reg.ws / "registry.yaml"
    code, out = run_decline(reg, "~/src/tmp-*")
    digest = plan_hash(out)
    moved = tmp_path / "rooms" / "registry.yaml"
    real_read = register.read_handle

    def read_handle(handle):
        # The swap lands after the locked identity check, just before the rename.
        data = real_read(handle)
        os.replace(registry, moved)
        os.symlink(moved, registry)
        return data

    monkeypatch.setattr(register, "read_handle", read_handle)
    refused(*run_decline(reg, "~/src/tmp-*", expect=digest), register.PLAN_CHANGED)
    assert os.readlink(registry) == str(moved)
    assert moved.read_text(encoding="utf-8") == REGISTRY
    leftovers = [name for name in os.listdir(reg.ws) if name.endswith(gen.TEMP_SUFFIX)]
    assert leftovers == []


def test_a_linked_registry_is_never_replaced_by_a_regular_file(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    real = tmp_path / "rooms" / "registry.yaml"
    real.write_text(REGISTRY, encoding="utf-8")
    real.chmod(0o600)
    (reg.ws / "registry.yaml").unlink()
    os.symlink(real, reg.ws / "registry.yaml")
    old = os.umask(0o022)
    try:
        for expect in (None, "0" * 64):
            refused(*run_decline(reg, "~/src/tmp-*", expect=expect),
                    scan.NOT_REGULAR % "registry.yaml")
            refused(*run_register(reg, "~/src/new", expect=expect),
                    scan.NOT_REGULAR % "registry.yaml")
    finally:
        os.umask(old)
    assert os.readlink(reg.ws / "registry.yaml") == str(real)
    assert real.read_text(encoding="utf-8") == REGISTRY
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


@pytest.mark.parametrize("char", ["\u0085", "\u007f", "\u009b", "\u2028", "\u202e"])
def test_evidence_with_yaml_breaks_and_controls_is_stored_escaped_and_parses_raw(
        tmp_path, monkeypatch, char):
    reg = Reg(tmp_path, monkeypatch)
    rel = "lib/a%sb.py" % char
    reg.put("new/" + rel, "import shop_web\n")
    registered(reg, "~/src/new")
    text = reg.text()
    assert char not in text, repr(text)
    assert "\\u%04x" % ord(char) in text, text
    drafted = [edge for edge in load(reg)["edges"] if edge["from"] == "new"]
    assert [edge["evidence"] for edge in drafted] == [rel + ":1"], drafted
    path, line = drafted[0]["evidence"].rsplit(":", 1)
    assert (reg.root / "new" / path).is_file()


def test_a_line_separator_name_leaves_later_registers_working(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/a\u2028b.py", "import shop_web\n")
    registered(reg, "~/src/new")
    make_repo(reg.root / "other", REMOTE % "other")
    reg.put("other/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/other")
    drafted = {edge["from"]: edge["evidence"] for edge in load(reg)["edges"]}
    assert drafted["new"] == "a\u2028b.py:1" and drafted["other"] == "run.sh:1", drafted


def test_a_raw_line_separator_already_in_the_registry_does_not_cut_the_splice(
        tmp_path, monkeypatch):
    # YAML reads U+2028 inside a quoted scalar as part of the value; the splice
    # must cut lines on the newline character only, or it splits that line.
    text = REGISTRY.replace(
        "evidence: polls it,", 'evidence: "a\u2028b.sh:1",')
    reg = Reg(tmp_path, monkeypatch, text)
    assert load(reg)["edges"][0]["evidence"] == "a\u2028b.sh:1"
    reg.put("new/run.sh", "source ~/src/shop-web/lib.sh\n")
    registered(reg, "~/src/new")
    found = load(reg)["edges"]
    assert [edge["evidence"] for edge in found] == ["a\u2028b.sh:1", "run.sh:1"], found


def test_the_plan_prints_evidence_escaped_and_stores_it_raw(tmp_path, monkeypatch):
    # The last edge already holds a raw U+202E (hand-written, or written by an
    # earlier version), so it is diff context in the plan.
    text = REGISTRY.replace("evidence: polls it,", 'evidence: "old\u202egnp.sh:1",')
    reg = Reg(tmp_path, monkeypatch, text)
    reg.put("new/lib/\u202egnp.py", "import shop_web\n")
    reg.put("new/\u2028x.py", "import ingest\n")
    code, out = run_register(reg, "~/src/new")
    assert code == 0, out
    for char in ("\u202e", "\u2028"):
        assert char not in out, repr(out)
    assert "old\\u202egnp.sh:1" in out, out
    assert "lib/\\\\u202egnp.py:1" in out and "\\\\u2028x.py:1" in out, out
    registered(reg, "~/src/new")
    stored = {edge["to"]: edge["evidence"] for edge in load(reg)["edges"]
              if edge["from"] == "new"}
    assert stored == {"shop-web": "lib/\u202egnp.py:1", "ingest": "\u2028x.py:1"}, stored


@pytest.mark.parametrize("verb", ["register", "decline"])
def test_an_unreadable_workspace_says_could_not_read_registry_once(tmp_path, monkeypatch,
                                                                     verb):
    reg = Reg(tmp_path, monkeypatch)
    reg.ws.chmod(0)
    try:
        if verb == "register":
            code, out = run_register(reg, "~/src/new")
        else:
            code, out = run_decline(reg, "~/src/tmp-*")
    finally:
        reg.ws.chmod(0o755)
    assert code == 2, out
    assert "UNKNOWN  could not read registry: " in out, out
    assert out.count("could not read registry") == 1, out


def test_stored_evidence_is_the_real_path(tmp_path, monkeypatch):
    reg = Reg(tmp_path, monkeypatch)
    reg.put("new/lib\\helpers.py", "import shop_web\n")
    registered(reg, "~/src/new")
    drafted = [edge for edge in load(reg)["edges"] if edge["from"] == "new"]
    assert drafted == [{"from": "new", "to": "shop-web", "kind": "source",
                        "evidence": "lib\\helpers.py:1", "approved_at": TODAY}], drafted
    path, line = drafted[0]["evidence"].rsplit(":", 1)
    assert (reg.root / "new" / path).is_file()
    buffer = io.StringIO()
    code = scan.main(str(reg.ws), None, out=buffer, edges=True)
    out = buffer.getvalue()
    assert code == 0, out
    assert "WARN   stale" not in out, out
