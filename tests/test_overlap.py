"""The excluded-overlap rule (workspace/overlap.py), through `gen` and `check`.

A project folder that is not archived, on any host, must not be an excluded
folder, hold one, or sit inside one, under any spelling: a symlink, a case or
Unicode variant, a subpath, a dangling symlink, or a spelling realpath leaves
alone but the kernel reaches (a macOS firmlink, a bind mount, volfs). `gen`
refuses such a registry with exit 1 and writes nothing; `check` reports it as
a gap. A step that cannot be stat'd for a reason other than "does not exist"
is a could-not-verify (exit 2).

Every fixture is built in tmp_path, which is HOME. Spellings that realpath
leaves alone are made portable by a stand-in: a symlink to `/` that a patched
os.path.realpath keeps as written, the way it keeps /System/Volumes/Data. The
real /System/Volumes/Data spelling runs on every platform: on macOS it exists
and both the node walk and the firmlink rule see it; elsewhere it does not
exist and the firmlink rule alone refuses it.
"""
import errno
import io
import json
import os
import subprocess
import sys

import pytest

from workspace import check, gen, overlap, scan
from test_gen import WS, contents, generate, plan_hash, run
from test_scan import REPO_ROOT

DATA_VOLUME = "/System/Volumes/Data"
ROOMS = {"path": "~/rooms/**", "reason": "live room instances"}
REAL_REALPATH = os.path.realpath


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def home_ws(tmp_path, monkeypatch, shape="resolved", excluded=(ROOMS,)):
    """A WS whose HOME is a real folder ("resolved") or a symlink to one ("linked")."""
    real = tmp_path / "home-real"
    real.mkdir()
    home = real
    if shape == "linked":
        home = tmp_path / "home"
        home.symlink_to(real)
    ws = WS(home, monkeypatch)
    ws.document["excluded"] = ws.document["excluded"] + [dict(entry) for entry in excluded]
    ws.write_registry()
    return ws


def set_ingest(ws, path, subpath=None):
    ws.document["projects"][1]["paths"] = {"desk": path}
    ws.document["projects"][1]["subpath"] = subpath
    ws.write_registry()


def line(name, folder, entry, host="desk"):
    return ("REFUSED  project `%s` folder %s (host `%s`) reaches the excluded path `%s`: it is, "
            "holds or sits inside that folder" % (name, folder, host, entry))


def gen_refused(ws):
    """gen's plan for ws: exit 1, nothing written. Returns the output."""
    before = contents(ws.path)
    code, out = run(ws)
    assert code == 1, out
    assert "nothing written" in out, out
    assert contents(ws.path) == before
    return out


def gen_unknown(ws):
    before = contents(ws.path)
    code, out = run(ws)
    assert code == 2, out
    assert contents(ws.path) == before
    return out


def keep_realpath(*kept):
    """os.path.realpath that leaves every path at or under one of `kept` as written.

    It resolves symlinks one component at a time, the way the real one does,
    so a symlink to a kept spelling resolves to that spelling: the way a
    firmlink or volfs spelling behaves on macOS.
    """
    def realpath(path, *args, **kwargs):
        text = os.fspath(path)
        if not text.startswith("/"):
            text = os.path.join(os.getcwd(), text)
        parts, out, hops = text.split("/")[1:], "/", 0
        while parts:
            part = parts.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                out = os.path.dirname(out)
                continue
            step = os.path.join(out, part)
            held = any(step == k or step.startswith(k + "/") for k in kept)
            if not held and os.path.islink(step) and hops < 40:
                hops += 1
                target = os.readlink(step)
                if target.startswith("/"):
                    out = "/"
                parts = target.split("/") + parts
                continue
            out = step
        return out
    return realpath


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """(alias, ws): alias(real) is another spelling of `real` that realpath leaves alone."""
    root = os.path.join(REAL_REALPATH(str(tmp_path)), "data")
    os.symlink("/", root)
    ws = home_ws(tmp_path, monkeypatch)
    home = REAL_REALPATH(str(ws.base))
    for folder in ("rooms/secret/sub", "src/alpha", "src/sibling", "rooms2"):
        os.makedirs(os.path.join(home, folder), exist_ok=True)
    monkeypatch.setattr(os.path, "realpath", keep_realpath(root))
    return (lambda real: root + real), ws, home


# --------------------------------------------------------------------------
# Bar 1: a symlink into an excluded folder (forward `reaches`)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["resolved", "linked"])
def test_a_symlink_into_an_excluded_folder_is_refused_naming_project_path_and_entry(
        tmp_path, monkeypatch, shape):
    ws = home_ws(tmp_path, monkeypatch, shape)
    (ws.base / "rooms" / "private").mkdir(parents=True)
    (ws.base / "src" / "room-link").symlink_to(ws.base / "rooms" / "private")
    set_ingest(ws, "~/src/room-link")
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / "src" / "room-link"), "~/rooms/**") + "\n" in out, out


def test_a_dangling_symlink_into_a_missing_excluded_folder_is_refused(tmp_path, monkeypatch):
    # Neither side exists, so no node: only the forward name clause sees it.
    ws = home_ws(tmp_path, monkeypatch, excluded=[{"path": "~/rooms/gone", "reason": "r"}])
    (ws.base / "rooms").mkdir()
    (ws.base / "src" / "room-link").symlink_to(ws.base / "rooms" / "gone" / "x")
    set_ingest(ws, "~/src/room-link")
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / "src" / "room-link"), "~/rooms/gone") in out, out


@pytest.mark.parametrize("shape", ["resolved", "linked"])
def test_a_folder_under_a_dangling_excluded_symlink_is_refused(tmp_path, monkeypatch, shape):
    # Retired: ~/work -> ~/code/gone, project ~/work/x (lexically refused);
    # here the project names the target's spelling, ~/src/gone/x.
    ws = home_ws(tmp_path, monkeypatch, shape, excluded=[{"path": "~/work", "reason": "r"}])
    (ws.base / "work").symlink_to(ws.base / "src" / "gone")
    set_ingest(ws, "~/src/gone/x")
    gen_refused(ws)
    set_ingest(ws, "~/work/x")
    gen_refused(ws)


# --------------------------------------------------------------------------
# Bar 2: a project that is or holds an excluded folder (reverse `reaches`)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("project", ["~/rooms", "~", "~/rooms/private/"])
def test_a_project_that_is_or_holds_an_excluded_folder_is_refused(tmp_path, monkeypatch,
                                                                    project):
    ws = home_ws(tmp_path, monkeypatch, excluded=[{"path": "~/rooms/private", "reason": "r"}])
    (ws.base / "rooms" / "private").mkdir(parents=True)
    set_ingest(ws, project)
    gen_refused(ws)


def test_a_dangling_project_link_to_a_folder_holding_a_missing_excluded_folder_is_refused(
        tmp_path, monkeypatch):
    # Neither side exists: only the reverse name clause sees it.
    ws = home_ws(tmp_path, monkeypatch,
                 excluded=[{"path": "~/rooms/gone/secret", "reason": "r"}])
    (ws.base / "rooms").mkdir()
    (ws.base / "src" / "up-link").symlink_to(ws.base / "rooms" / "gone")
    set_ingest(ws, "~/src/up-link")
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / "src" / "up-link"), "~/rooms/gone/secret") in out, out


@pytest.mark.parametrize("shape", ["resolved", "linked"])
def test_home_is_refused_when_the_excluded_folders_link_outside_it(tmp_path, monkeypatch, shape):
    ws = home_ws(tmp_path, monkeypatch, shape, excluded=[{"path": "~/work", "reason": "r"}])
    outside = tmp_path / "outside"
    for name in ("work", "rooms"):
        (outside / name).mkdir(parents=True)
        (ws.base / name).symlink_to(outside / name)
    set_ingest(ws, "~")
    gen_refused(ws)


# --------------------------------------------------------------------------
# Bar 3: the same node under a spelling realpath leaves alone (`shares_node`)
# --------------------------------------------------------------------------

def test_a_link_to_another_spelling_of_a_folder_under_an_excluded_folder_is_refused(stand_in):
    # node(E) is in nodes(P): the kernel's `..` from rooms/secret is rooms.
    alias, ws, home = stand_in
    mixed = os.path.join(home, "src", "mixed-link")
    os.symlink(alias(home + "/rooms/secret"), mixed)
    assert os.path.realpath(mixed) == alias(home + "/rooms/secret")
    set_ingest(ws, mixed)
    gen_refused(ws)


def test_a_folder_holding_an_excluded_link_to_another_spelling_is_refused(stand_in):
    # node(P) is in nodes(E): the excluded link reaches rooms/secret, whose
    # `..` is the project ~/rooms.
    alias, ws, home = stand_in
    os.symlink(alias(home + "/rooms/secret"), os.path.join(home, "excluded-link"))
    ws.document["excluded"] = [{"path": "~/excluded-link", "reason": "r"}]
    set_ingest(ws, "~/rooms")
    gen_refused(ws)


@pytest.mark.parametrize("child", ["sub", "not-yet"])
def test_a_child_of_a_link_to_another_spelling_is_refused(stand_in, child):
    alias, ws, home = stand_in
    mixed = os.path.join(home, "src", "mixed-link")
    os.symlink(alias(home + "/rooms/secret"), mixed)
    set_ingest(ws, mixed + "/" + child)
    gen_refused(ws)


def test_a_two_hop_link_ending_at_another_spelling_is_refused(stand_in):
    alias, ws, home = stand_in
    mixed = os.path.join(home, "src", "mixed-link")
    os.symlink(alias(home + "/rooms/secret"), mixed)
    two_hop = os.path.join(home, "src", "two-hop")
    os.symlink(mixed, two_hop)
    assert os.path.realpath(two_hop) == alias(home + "/rooms/secret")
    set_ingest(ws, two_hop)
    gen_refused(ws)


def test_another_spelling_of_the_excluded_folder_and_of_home_is_refused(stand_in):
    alias, ws, home = stand_in
    for project in (alias(home + "/rooms/secret"), alias(home + "/rooms"), alias(home)):
        set_ingest(ws, project)
        gen_refused(ws)


def test_volfs_spellings_on_macos_and_their_stand_in_are_refused(stand_in):
    # macOS names a folder /.vol/<device>/<inode>; elsewhere the stand-in runs.
    alias, ws, home = stand_in
    projects = [alias(home + "/rooms/secret")]
    if sys.platform == "darwin":
        for name in ("rooms/secret", "rooms"):
            status = os.stat(os.path.join(home, name))
            projects.append("/.vol/%d/%d" % (status.st_dev, status.st_ino))
    for project in projects:
        set_ingest(ws, project)
        gen_refused(ws)


def test_a_sibling_a_name_prefix_and_a_link_to_another_spelling_of_a_sibling_are_written(
        stand_in):
    alias, ws, home = stand_in
    linked = os.path.join(home, "src", "sibling-link")
    os.symlink(alias(home + "/src/sibling"), linked)
    for project in (home + "/src/sibling", home + "/rooms2", linked):
        set_ingest(ws, project)
        code, out = run(ws)
        assert code == 0, out


def test_the_firmlink_spelling_of_an_excluded_folder_is_refused(tmp_path, monkeypatch):
    # On macOS this spelling exists (same device and inode as the folder);
    # elsewhere it does not, and the firmlink rule alone refuses it.
    ws = home_ws(tmp_path, monkeypatch)
    home = REAL_REALPATH(str(ws.base))
    os.makedirs(os.path.join(home, "rooms", "private"))
    for real in (home + "/rooms/private", home + "/rooms", home):
        spelled = DATA_VOLUME + real
        if sys.platform == "darwin":
            assert os.stat(spelled).st_ino == os.stat(real).st_ino
            assert os.path.realpath(spelled) != os.path.realpath(real)
        set_ingest(ws, spelled)
        gen_refused(ws)


def test_the_firmlink_spelling_of_missing_folders_is_refused(tmp_path, monkeypatch):
    # Neither folder exists, so no node on any platform: only the rule that
    # drops a leading /System/Volumes/Data sees it.
    ws = home_ws(tmp_path, monkeypatch, excluded=[{"path": "~/rooms/gone", "reason": "r"}])
    home = REAL_REALPATH(str(ws.base))
    set_ingest(ws, DATA_VOLUME + home + "/rooms/gone/x")
    gen_refused(ws)
    assert overlap.identity(DATA_VOLUME + home + "/x") == overlap.identity(home + "/x")


# --------------------------------------------------------------------------
# Bar 4: case and Unicode variants (`fold`), on folders that do not exist
# --------------------------------------------------------------------------

@pytest.mark.parametrize("excluded, project", [
    ("~/rooms/Gone", "~/rooms/gone/x"),
    ("~/work", "~/WORK"),
    ("~/work", "~/Work/link"),
    ("~/café", "~/café/notes"),
    ("~/café", "~/café"),
])
def test_a_case_or_unicode_variant_of_a_missing_excluded_folder_is_refused(
        tmp_path, monkeypatch, excluded, project):
    ws = home_ws(tmp_path, monkeypatch, excluded=[{"path": excluded, "reason": "r"}])
    set_ingest(ws, project)
    gen_refused(ws)


@pytest.mark.parametrize("shape", ["resolved", "linked"])
def test_a_case_variant_folder_that_links_out_is_refused(tmp_path, monkeypatch, shape):
    ws = home_ws(tmp_path, monkeypatch, shape, excluded=[{"path": "~/work", "reason": "r"}])
    (ws.base / "src" / "foo").mkdir()
    (ws.base / "Work").mkdir()
    (ws.base / "Work" / "link").symlink_to(ws.base / "src" / "foo")
    set_ingest(ws, "~/Work/link")
    gen_refused(ws)


# --------------------------------------------------------------------------
# Bar 5: a subpath into an excluded folder (one shared folder function)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path, subpath, folder", [
    ("~", "rooms/private", "rooms/private"),
    ("~/src/ingest", "../../rooms/x", "rooms/x"),
])
def test_a_subpath_into_an_excluded_folder_is_refused(tmp_path, monkeypatch, path, subpath,
                                                      folder):
    ws = home_ws(tmp_path, monkeypatch)
    set_ingest(ws, path, subpath)
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / folder), "~/rooms/**") in out, out


def test_the_editor_file_lists_the_folder_the_rule_judged(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch)
    set_ingest(ws, "~/src/ingest", "../shop-web/pkg")
    generate(ws)
    folders = json.loads(ws.read("desk.code-workspace"))["folders"]
    judged = overlap.project_folder(ws.document["projects"][1], "desk")
    assert folders[1] == {"name": "ingest", "path": judged}
    assert judged == str(ws.base / "src" / "shop-web" / "pkg")


# --------------------------------------------------------------------------
# Bar 6: the excluded entry's forms
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["~/rooms/**", "~/rooms/*", "~/rooms/", "~/rooms", "ABS/**",
                                   "ABS/*"])
def test_every_form_of_an_excluded_entry_is_judged_as_its_folder(tmp_path, monkeypatch, entry):
    ws = home_ws(tmp_path, monkeypatch, excluded=[])
    entry = entry.replace("ABS", str(ws.base / "rooms"))
    ws.document["excluded"].append({"path": entry, "reason": "r"})
    (ws.base / "rooms" / "private").mkdir(parents=True)
    (ws.base / "src" / "room-link").symlink_to(ws.base / "rooms" / "private")
    set_ingest(ws, "~/src/room-link")
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / "src" / "room-link"), entry) in out, out


# --------------------------------------------------------------------------
# Every host, and archived projects
# --------------------------------------------------------------------------

def test_another_hosts_path_is_judged_too(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch)
    ws.document["code_roots"]["laptop"] = "~/lap"
    (ws.base / "rooms" / "private").mkdir(parents=True)
    (ws.base / "src" / "room-link").symlink_to(ws.base / "rooms" / "private")
    ws.document["projects"][1]["paths"]["laptop"] = "~/src/room-link"
    ws.write_registry()
    out = gen_refused(ws)
    assert line("ingest", str(ws.base / "src" / "room-link"), "~/rooms/**", "laptop") in out


def test_an_archived_project_is_not_judged(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch)
    (ws.base / "rooms" / "private").mkdir(parents=True)
    (ws.base / "src" / "room-link").symlink_to(ws.base / "rooms" / "private")
    ws.document["projects"][2]["paths"] = {"desk": "~/src/room-link"}
    ws.write_registry()
    code, out = run(ws)
    assert code == 0, out


# --------------------------------------------------------------------------
# Fail closed: a step os.stat cannot read is a could-not-verify (exit 2)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["resolved", "linked"])
def test_a_symlink_loop_project_is_unknown_not_crashed(tmp_path, monkeypatch, shape):
    ws = home_ws(tmp_path, monkeypatch, shape)
    (ws.base / "src" / "a").symlink_to(ws.base / "src" / "b")
    (ws.base / "src" / "b").symlink_to(ws.base / "src" / "a")
    set_ingest(ws, "~/src/a")
    out = gen_unknown(ws)
    assert "UNKNOWN  cannot stat %s" % (ws.base / "src" / "a") in out, out


def test_a_symlink_loop_excluded_entry_is_unknown(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch, excluded=[{"path": "~/loop", "reason": "r"}])
    (ws.base / "loop").symlink_to(ws.base / "loop")
    out = gen_unknown(ws)
    assert "UNKNOWN  cannot stat %s" % (ws.base / "loop") in out, out


def deny_stat(monkeypatch, wanted):
    real_stat = os.stat

    def fake(path, *args, **kwargs):
        if wanted(os.fspath(path)):
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", fake)


def test_a_project_step_that_cannot_be_stated_is_unknown(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch)
    target = str(ws.base / "src" / "ingest")
    deny_stat(monkeypatch, lambda text: text == target)
    out = gen_unknown(ws)
    assert "UNKNOWN  cannot stat %s: " % target in out, out


@pytest.mark.parametrize("folder", ["src/ingest", "rooms"])
def test_a_parent_walk_step_that_cannot_be_stated_is_unknown(tmp_path, monkeypatch, folder):
    # The step itself stats; its `..` does not. Project side, then excluded side.
    ws = home_ws(tmp_path, monkeypatch)
    for made in ("src/ingest", "rooms"):
        (ws.base / made).mkdir(parents=True, exist_ok=True)
    target = str(ws.base / folder)
    deny_stat(monkeypatch,
              lambda text: text.endswith("/..") and os.path.normpath(text[:-3]) == target)
    out = gen_unknown(ws)
    assert "UNKNOWN  cannot stat %s/.." % target in out, out


# --------------------------------------------------------------------------
# Bar 7: check reports the overlap as a gap, and a step it cannot stat as UNKNOWN
# --------------------------------------------------------------------------

FIX = REPO_ROOT / "tests" / "fixtures" / "workspace-registry" / "conforming"


def check_ws(tmp_path):
    """A hand-written workspace from the conforming fixture, HOME = tmp_path/home."""
    home = tmp_path / "home"
    (home / "src").mkdir(parents=True)
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / "registry.yaml").write_bytes((FIX / "registry.yaml").read_bytes())
    (ws / "AGENTS.md").write_bytes((FIX / "map.md").read_bytes())
    (ws / ".claude" / "settings.json").write_bytes((FIX / "settings.json").read_bytes())
    return home, ws


def run_check(home, ws):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), HOME=str(home))
    result = subprocess.run([sys.executable, "-P", "-m", "workspace", "check", str(ws)],
                            capture_output=True, text=True, env=env, check=False)
    return result.returncode, result.stdout + result.stderr


def test_check_passes_a_clean_hand_written_workspace(tmp_path):
    home, ws = check_ws(tmp_path)
    (home / "src" / "alpha").mkdir()
    code, out = run_check(home, ws)
    assert code == 0, out


def test_check_reports_a_symlink_into_an_excluded_folder_as_a_gap(tmp_path):
    home, ws = check_ws(tmp_path)
    (home / "private" / "secret").mkdir(parents=True)
    (home / "src" / "alpha").symlink_to(home / "private" / "secret")
    code, out = run_check(home, ws)
    assert code == 1, out
    assert ("GAP   project `alpha` folder %s (host `laptop`) reaches the excluded path "
            "`~/private/**`" % (home / "src" / "alpha")) in out, out


def test_check_reports_a_step_it_cannot_stat_as_unknown(tmp_path):
    home, ws = check_ws(tmp_path)
    (home / "src" / "alpha").symlink_to(home / "src" / "alpha")
    code, out = run_check(home, ws)
    assert code == 2, out
    assert "UNKNOWN  cannot stat %s" % (home / "src" / "alpha") in out, out


# --------------------------------------------------------------------------
# Bar 8: a clean registry renders as before
# --------------------------------------------------------------------------

def test_a_clean_registry_with_real_folders_and_links_renders_unchanged(tmp_path, monkeypatch):
    ws = home_ws(tmp_path, monkeypatch)
    for folder in ("rooms/private", "rooms2", "src/shop-web", "src/other"):
        (ws.base / folder).mkdir(parents=True)
    (ws.base / "src" / "ingest").symlink_to(ws.base / "src" / "other")
    out = generate(ws)
    assert "REFUSED" not in out and "UNKNOWN" not in out, out
    assert ws.read("desk.code-workspace") == json.dumps({
        "folders": [{"name": "shop-web", "path": str(ws.base / "src" / "shop-web")},
                    {"name": "ingest", "path": str(ws.base / "src" / "ingest")}],
        "settings": {}}, indent=2) + "\n"
    code, out = run_check(ws.base, ws.path)
    assert code == 0, out
