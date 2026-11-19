"""`workspace check` fails when the generated files are stale against the registry.

Every case builds a temp workspace with `gen`, then breaks it the way a real
restore or edit would, and runs the real CLI. `check` is never given a host, so
the cases also pin what it compares (the host-independent files) and what it
leaves out (the per-host files).
"""
import json
import os
import re

from workspace import gen

from test_gen import HOST, WS, contents, generate, run, run_cli

NEW_LINER = "Rewritten for the new registry."
OLD_LINER = "The shop-web project."


def fix_line(ws, host=HOST):
    return "run `workspace gen %s --host %s`" % (ws.path, host)


def stale_files(out):
    return re.findall(r"^GAP   generated file `([^`]+)` is stale against registry\.yaml; ",
                      out, re.M)


def change_liner(ws, text=NEW_LINER):
    ws.document["projects"][0]["one_liner"] = text
    ws.write_registry()


def tree_state(ws):
    """Every file's bytes and mtime: a write, even of the same bytes, shows."""
    return {rel: (data, None if data is None else os.stat(ws.path / rel).st_mtime_ns)
            for rel, data in contents(ws.path).items()}


def test_old_map_after_a_registry_one_liner_change_fails_check(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out
    change_liner(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "a map older than the registry passed check:\n" + out
    assert stale_files(out) == ["AGENTS.md", ".workspace-gen.json"], out
    assert fix_line(ws) in out, out


def test_the_regenerated_tree_passes(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    change_liner(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, out
    generate(ws)
    assert NEW_LINER in ws.read("AGENTS.md")
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out
    assert "PASS  registry, map, and settings are consistent" in out, out


def test_hand_reverted_line_with_a_matching_manifest_hash_fails(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    change_liner(ws)
    generate(ws)
    reverted = ws.read("AGENTS.md").replace(NEW_LINER, OLD_LINER)
    assert reverted != ws.read("AGENTS.md")
    ws.write("AGENTS.md", reverted)
    manifest = json.loads(ws.read(".workspace-gen.json"))
    manifest["files"]["AGENTS.md"]["hash"] = gen.sha256(gen.blank_keep(reverted))
    ws.write(".workspace-gen.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # gen itself sees a file that matches its record, and plans to rewrite it.
    code, plan = run(ws)
    assert code == 0 and "change AGENTS.md" in plan, plan
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "a hand-reverted line with a matching manifest passed check:\n" + out
    assert "AGENTS.md" in stale_files(out), out


def test_previous_map_and_manifest_restored_together_fail(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    old_map = ws.read("AGENTS.md")
    old_manifest = ws.read(".workspace-gen.json")
    change_liner(ws)
    generate(ws)
    ws.write("AGENTS.md", old_map)
    ws.write(".workspace-gen.json", old_manifest)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "the previous map and manifest restored together passed check:\n" + out
    assert stale_files(out) == ["AGENTS.md", ".workspace-gen.json"], out


def test_previous_manifest_alone_fails(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    old_manifest = ws.read(".workspace-gen.json")
    change_liner(ws)
    generate(ws)
    ws.write(".workspace-gen.json", old_manifest)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "the previous manifest restored alone passed check:\n" + out
    assert stale_files(out) == [".workspace-gen.json"], out


def test_previous_settings_fail(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    old_settings = ws.read(".claude/settings.json")
    old_manifest = ws.read(".workspace-gen.json")
    ws.document["code_roots"] = {HOST: "~/src2"}
    ws.write_registry()
    generate(ws)
    assert ws.read(".claude/settings.json") != old_settings
    ws.write(".claude/settings.json", old_settings)
    ws.write(".workspace-gen.json", old_manifest)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "previous settings and manifest passed check:\n" + out
    assert stale_files(out) == [".claude/settings.json", ".workspace-gen.json"], out


def test_hand_edit_that_matches_no_record_is_reported(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    ws.write("AGENTS.md", ws.read("AGENTS.md").replace(OLD_LINER, "Edited by hand."))
    code, out = run_cli("check", str(ws.path))
    assert code == 1, out
    assert "GAP   gen would refuse to render the generated files" in out, out
    assert "AGENTS.md was changed outside gen" in out, out


def test_deleted_manifest_does_not_hide_a_stale_map(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    change_liner(ws)
    os.remove(ws.file(".workspace-gen.json"))
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "a stale map with its manifest deleted passed check:\n" + out
    assert "GAP   gen would refuse to render the generated files" in out, out
    assert "AGENTS.md exists and gen has no record of writing it" in out, out


def test_a_generated_map_without_its_manifest_fails(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    os.remove(ws.file(".workspace-gen.json"))
    code, out = run_cli("check", str(ws.path))
    assert code == 1, out
    assert stale_files(out) == [".workspace-gen.json"], out


def test_a_map_stripped_of_its_header_is_still_compared_through_the_manifest(tmp_path,
                                                                           monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    change_liner(ws)
    lines = ws.read("AGENTS.md").splitlines(keepends=True)
    assert lines[0].startswith(gen.HEADER_PREFIX)
    ws.write("AGENTS.md", "".join(lines[1:]))
    code, out = run_cli("check", str(ws.path))
    assert code == 1, "a stale map without its header passed check:\n" + out
    assert "GAP   gen would refuse to render the generated files" in out, out


def test_per_host_files_are_not_compared(tmp_path, monkeypatch):
    # <host>.code-workspace is an editor file for one machine; check has no host,
    # so a workspace folder committed without it still passes.
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    os.remove(ws.file("%s.code-workspace" % HOST))
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out


def two_hosts(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    ws.document["code_roots"] = {"desk": "~/src", "laptop": "~/src"}
    ws.write_registry()
    return ws


def test_workspace_generated_for_another_host_passes_without_a_host(tmp_path, monkeypatch):
    ws = two_hosts(tmp_path, monkeypatch)
    generate(ws, host="laptop")
    # An editor file for the other host that gen never wrote must not fail check.
    ws.write("desk.code-workspace", "{}\n")
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out


def test_with_several_hosts_the_fix_line_leaves_the_host_to_the_person(tmp_path, monkeypatch):
    ws = two_hosts(tmp_path, monkeypatch)
    generate(ws, host="laptop")
    change_liner(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, out
    assert stale_files(out) == ["AGENTS.md", ".workspace-gen.json"], out
    assert fix_line(ws, "<host>") in out, out


def test_check_writes_nothing(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    before = tree_state(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 0, out
    assert tree_state(ws) == before
    change_liner(ws)
    before = tree_state(ws)
    code, out = run_cli("check", str(ws.path))
    assert code == 1, out
    assert tree_state(ws) == before
