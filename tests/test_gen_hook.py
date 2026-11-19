"""`gen`'s opt-in SessionStart hook: one manifest-owned entry, `workspace prompt`.

The opt-in is the top-level registry key `session_prompt: on`. Absent, or any
other value, is off. gen adds exactly one SessionStart entry it owns while the
opt-in is on, removes only that owned entry when it turns off, and never
touches a SessionStart hook it did not add.
"""
import json

import pytest
import yaml

from workspace import gen
from test_gen import WS, check_passes, generate, plan_hash, run

OWNED = {"hooks": [{"type": "command", "command": "workspace prompt"}]}
HAND = {"matcher": "startup", "hooks": [{"type": "command", "command": "~/bin/hand-hook.sh"}]}
STOP = {"hooks": [{"type": "command", "command": "~/bin/on-stop.sh"}]}


def set_prompt(ws, line):
    """Rewrite the registry with `line` appended at the top level (None: no key)."""
    text = yaml.safe_dump(ws.document, sort_keys=False, allow_unicode=True)
    if line is not None:
        text += line + "\n"
    ws.write_registry(text)


def hand_settings(ws, hooks):
    ws.write(".claude/settings.json", json.dumps({"hooks": hooks}, indent=2) + "\n")


def session_start(ws):
    return ws.settings().get("hooks", {}).get("SessionStart")


def hook_owned(ws):
    manifest = json.loads(ws.read(".workspace-gen.json"))
    return manifest["files"][".claude/settings.json"]["owned"].get("hooks.SessionStart")


def test_the_hook_command_is_the_constant_with_no_path_or_registry_text():
    assert gen.HOOK_COMMAND == "workspace prompt"
    assert gen.hook_entry() == OWNED


# --------------------------------------------------------------------------
# Off by default: a hand-written SessionStart hook survives
# --------------------------------------------------------------------------

def test_off_by_default_adds_nothing_and_a_hand_hook_survives(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    hand_settings(ws, {"SessionStart": [HAND], "Stop": [STOP]})
    generate(ws, "--adopt")
    assert ws.settings()["hooks"] == {"SessionStart": [HAND], "Stop": [STOP]}
    assert "workspace prompt" not in ws.read(".claude/settings.json")
    assert hook_owned(ws) is None
    check_passes(ws)


def test_off_with_no_hooks_writes_no_hooks_key(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    assert "hooks" not in ws.settings()
    manifest = json.loads(ws.read(".workspace-gen.json"))
    assert set(manifest["files"][".claude/settings.json"]["owned"]) == {
        "additionalDirectories", "deny", "denyRead"}


# --------------------------------------------------------------------------
# On: one owned entry; on then off: only the owned entry goes
# --------------------------------------------------------------------------

def test_on_adds_exactly_one_owned_entry_beside_a_hand_hook(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    hand_settings(ws, {"SessionStart": [HAND], "Stop": [STOP]})
    generate(ws, "--adopt")
    assert ws.settings()["hooks"] == {"SessionStart": [HAND, OWNED], "Stop": [STOP]}
    assert hook_owned(ws) == ["workspace prompt"]
    check_passes(ws)
    code, out = run(ws)
    assert code == 0, out
    assert "same   .claude/settings.json" in out
    generate(ws)
    assert session_start(ws) == [HAND, OWNED]


def test_on_then_off_removes_only_the_owned_entry(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    hand_settings(ws, {"SessionStart": [HAND], "Stop": [STOP]})
    generate(ws, "--adopt")
    assert session_start(ws) == [HAND, OWNED]
    set_prompt(ws, None)
    code, out = run(ws)
    assert code == 0, out
    assert "-  hooks.SessionStart: workspace prompt" in out
    generate(ws)
    assert ws.settings()["hooks"] == {"SessionStart": [HAND], "Stop": [STOP]}
    assert hook_owned(ws) is None
    check_passes(ws)


def test_on_then_off_with_no_hand_hooks_leaves_no_hooks_key(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    before = ws.settings()
    set_prompt(ws, "session_prompt: on")
    out = generate(ws)
    assert session_start(ws) == [OWNED]
    set_prompt(ws, "session_prompt: off")
    generate(ws)
    assert ws.settings() == before


def test_an_identical_hand_written_entry_is_never_owned_or_removed(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["hooks"] = {"SessionStart": [OWNED]}
    ws.write(".claude/settings.json", json.dumps(settings, indent=2) + "\n")
    set_prompt(ws, "session_prompt: on")
    generate(ws)
    assert session_start(ws) == [OWNED]  # present already: not added twice
    assert hook_owned(ws) is None
    set_prompt(ws, None)
    generate(ws)
    assert session_start(ws) == [OWNED]  # gen did not add it, so it stays


def turn_off_and_keep_bytes(ws):
    """Turn the opt-in off; the plan names no hook and the settings keep every byte."""
    before = ws.read(".claude/settings.json")
    set_prompt(ws, None)
    code, out = run(ws)
    assert code == 0, out
    assert "hooks.SessionStart" not in out
    generate(ws)
    assert ws.read(".claude/settings.json") == before
    assert hook_owned(ws) is None


def test_adopt_never_claims_an_equal_hand_entry_on_first_adoption(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    hand_settings(ws, {"SessionStart": [OWNED], "Stop": [STOP]})
    code, out = run(ws, "--adopt")
    assert code == 0, out
    assert "hooks.SessionStart" not in out
    generate(ws, "--adopt")
    assert ws.settings()["hooks"] == {"SessionStart": [OWNED], "Stop": [STOP]}
    assert hook_owned(ws) is None
    turn_off_and_keep_bytes(ws)
    assert ws.settings()["hooks"] == {"SessionStart": [OWNED], "Stop": [STOP]}


def test_adopt_never_claims_an_equal_hand_entry_with_a_manifest(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["hooks"] = {"SessionStart": [OWNED], "Stop": [STOP]}
    ws.write(".claude/settings.json", json.dumps(settings, indent=2) + "\n")
    set_prompt(ws, "session_prompt: on")
    generate(ws, "--adopt")
    assert ws.settings()["hooks"] == {"SessionStart": [OWNED], "Stop": [STOP]}
    assert hook_owned(ws) is None
    turn_off_and_keep_bytes(ws)
    assert ws.settings()["hooks"] == {"SessionStart": [OWNED], "Stop": [STOP]}


MATCHED_PROMPT = {"matcher": "startup",
                  "hooks": [{"type": "command", "command": "workspace prompt"}]}
GROUP_PROMPT = {"hooks": [{"type": "command", "command": "~/bin/hand-hook.sh"},
                          {"type": "command", "command": "workspace prompt"}]}


@pytest.mark.parametrize("hand", [MATCHED_PROMPT, GROUP_PROMPT], ids=["matcher", "group"])
@pytest.mark.parametrize("flags", [(), ("--adopt",)], ids=["plain", "adopt"])
def test_no_second_copy_beside_a_hand_hook_that_runs_the_prompt(tmp_path, monkeypatch, hand,
                                                                  flags):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    settings = ws.settings()
    settings["hooks"] = {"SessionStart": [hand], "Stop": [STOP]}
    ws.write(".claude/settings.json", json.dumps(settings, indent=2) + "\n")
    set_prompt(ws, "session_prompt: on")
    code, out = run(ws, *flags)
    assert code == 0, out
    assert "hooks.SessionStart" not in out
    generate(ws, *flags)
    assert ws.settings()["hooks"] == {"SessionStart": [hand], "Stop": [STOP]}
    assert hook_owned(ws) is None
    check_passes(ws)
    turn_off_and_keep_bytes(ws)
    assert ws.settings()["hooks"] == {"SessionStart": [hand], "Stop": [STOP]}


@pytest.mark.parametrize("hand", [MATCHED_PROMPT, GROUP_PROMPT], ids=["matcher", "group"])
def test_no_second_copy_on_first_adoption(tmp_path, monkeypatch, hand):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    hand_settings(ws, {"SessionStart": [hand], "Stop": [STOP]})
    generate(ws, "--adopt")
    assert ws.settings()["hooks"] == {"SessionStart": [hand], "Stop": [STOP]}
    assert hook_owned(ws) is None
    turn_off_and_keep_bytes(ws)


def test_an_owned_entry_removed_by_hand_is_refused_while_on(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    generate(ws)
    settings = ws.settings()
    del settings["hooks"]
    ws.write(".claude/settings.json", json.dumps(settings, indent=2) + "\n")
    ws.document["excluded"].append({"path": "~/other", "reason": "another"})
    set_prompt(ws, "session_prompt: on")
    code, out = run(ws)
    assert code == 1, out
    assert "gen-owned entries removed by hand: hooks.SessionStart workspace prompt" in out


@pytest.mark.parametrize("edit", [{"timeout": 10}, {"matcher": "startup"}],
                         ids=["timeout", "matcher"])
def test_an_owned_entry_edited_by_hand_is_refused_not_dropped(tmp_path, monkeypatch, edit):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "session_prompt: on")
    generate(ws)
    assert session_start(ws) == [OWNED]
    assert hook_owned(ws) == ["workspace prompt"]
    settings = ws.settings()
    edited = dict(OWNED, **edit)
    settings["hooks"]["SessionStart"] = [edited]
    ws.write(".claude/settings.json", json.dumps(settings, indent=2) + "\n")
    settings_before = ws.read(".claude/settings.json")
    manifest_before = ws.read(".workspace-gen.json")
    code, out = run(ws)
    assert code == 1, out
    assert "gen-owned entries removed by hand: hooks.SessionStart workspace prompt" in out
    code, out = run(ws, "--adopt")
    assert code == 1, out
    assert ws.read(".claude/settings.json") == settings_before
    assert ws.read(".workspace-gen.json") == manifest_before
    assert hook_owned(ws) == ["workspace prompt"]


# --------------------------------------------------------------------------
# A hooks shape gen cannot merge into
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hooks, reason", [
    ("not an object", "settings.json `hooks` is not an object; gen cannot merge into it"),
    ({"SessionStart": {"command": "x"}},
     "settings.json `hooks.SessionStart` is not a list; gen cannot merge into it"),
])
def test_a_hooks_shape_gen_cannot_merge_into(tmp_path, monkeypatch, hooks, reason):
    ws = WS(tmp_path, monkeypatch)
    hand_settings(ws, hooks)
    generate(ws, "--adopt")  # off: gen has no part in hooks, so it passes through
    assert ws.settings()["hooks"] == hooks
    set_prompt(ws, "session_prompt: on")
    code, out = run(ws)
    assert code == 1, out
    assert "REFUSED  " + reason in out


# --------------------------------------------------------------------------
# The grammar: `on` is on; absent or anything else is off
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "session_prompt: on",
    "session_prompt: 'on'",
    'session_prompt: "on"',
    "session_prompt: on   # the daily prompt",
])
def test_on_forms(tmp_path, monkeypatch, line):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, line)
    out = generate(ws)
    assert session_start(ws) == [OWNED], out


@pytest.mark.parametrize("line", [
    "session_prompt: On",
    "session_prompt: ON",
    "session_prompt: true",
    "session_prompt: yes",
    "session_prompt: off",
    "session_prompt: 1",
    "session_prompt: 'on '",
    "session_prompt: onn",
    "session_prompt:",
    "session_prompt: [on]",
    "session_prompt: {on: on}",
])
def test_near_misses_are_off_and_named(tmp_path, monkeypatch, line):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, line)
    code, out = run(ws)
    assert code == 0, out
    assert ("note   registry `session_prompt` is set but not to `on`, so the SessionStart "
            "prompt stays off") in out
    generate(ws)
    assert "hooks" not in ws.settings()


def test_absent_is_off_without_a_note(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    code, out = run(ws)
    assert code == 0, out
    assert "session_prompt" not in out


def test_another_case_of_the_key_is_outside_the_grammar(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    set_prompt(ws, "Session_prompt: on")
    code, out = run(ws)
    assert code == 2, out
    assert "unknown field(s): Session_prompt" in out


# --------------------------------------------------------------------------
# The plan hash and check
# --------------------------------------------------------------------------

def test_the_plan_hash_changes_when_the_opt_in_flips(tmp_path, monkeypatch):
    ws = WS(tmp_path, monkeypatch)
    generate(ws)
    code, out = run(ws)
    off = plan_hash(out)
    set_prompt(ws, "session_prompt: on")
    code, out = run(ws)
    assert code == 0, out
    on = plan_hash(out)
    assert on != off
    assert "+  hooks.SessionStart: workspace prompt" in out
    code, written = run(ws, expect=off)
    assert code == 1, written
    assert "REFUSED  " + gen.PLAN_CHANGED in written
    code, written = run(ws, expect=on)
    assert code == 0, written
    check_passes(ws)
    set_prompt(ws, None)
    code, out = run(ws)
    assert plan_hash(out) == off  # the same files as before it was turned on
