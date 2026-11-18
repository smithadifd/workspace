#!/usr/bin/env bash
# Self-test for `workspace check WORKSPACE` (workspace/check.py).
#
# Proves the properties the check exists to have:
#   1. every active project has its own map line
#   2. archived projects appear only in the compact line
#   3. every excluded path is in permissions.deny (absolute paths as `//x/**`, never a
#      single-slash rule) and, when the settings declare a sandbox block, in
#      sandbox.filesystem.denyRead
#   4. duplicate YAML keys are rejected; an unhashable key is UNKNOWN (exit 2)
#   5. an archived project is on the compact line of its own cluster
#
# Each case assembles a workspace folder in a temp dir from fixture parts
# (registry.yaml, the map as AGENTS.md, .claude/settings.json) and runs the
# package's CLI against it. The committed settings fixtures carry no sandbox
# block; the cases that need one add a minimal sandbox.filesystem.denyRead block
# to a temp copy. One GREEN conforming fixture and one RED fixture per property.
# Each property's production guard is mutated on a scratch copy of the package,
# then restored. Everything happens in a temp dir. No network. No writes outside
# $TMPDIR.
#
# usage: tests/check-workspace-registry.test.sh
# exit:  0 all pass · 1 one or more failures
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
FIX="$here/fixtures/workspace-registry"
GREEN_REG="$FIX/conforming/registry.yaml"
GREEN_MAP="$FIX/conforming/map.md"
SEED_REG="$FIX/example-seed/registry.yaml"
SEED_MAP="$FIX/example-seed/map.md"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Settings with a sandbox block exist only here, in temp copies.
sandboxed() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

base, out, deny_read = sys.argv[1:4]
with open(base, encoding="utf-8") as handle:
    data = json.load(handle)
data["sandbox"] = {} if deny_read == "-" else {"filesystem": {"denyRead": json.loads(deny_read)}}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
PY
}
FULL_DENY_READ='["~/private/**", "~/scratch", "/var/private"]'
mkdir -p "$tmp/settings"
GREEN_SET="$tmp/settings/conforming.json"
sandboxed "$FIX/conforming/settings.json" "$GREEN_SET" "$FULL_DENY_READ"
SEED_SET="$tmp/settings/example-seed.json"
sandboxed "$FIX/example-seed/settings.json" "$SEED_SET" '["~/scratch-lanes", "~/vault/**"]'
for deny_case in deny-missing deny-read-only deny-edit-only single-slash-deny; do
  sandboxed "$FIX/$deny_case/settings.json" "$tmp/settings/$deny_case.json" "$FULL_DENY_READ"
done
sandboxed "$FIX/excluded-unenforced/settings.json" "$tmp/settings/excluded-unenforced.json" \
  '["~/private/**", "/var/private"]'
sandboxed "$FIX/sandbox-no-denyread/settings.json" "$tmp/settings/sandbox-no-denyread.json" "-"

OPEN_COUNT_WRAPPER="$tmp/count-path-opens.py"
cat >"$OPEN_COUNT_WRAPPER" <<'PY'
from pathlib import Path
import runpy
import sys

package_root = sys.argv[1]
workspace = Path(sys.argv[2])
inputs = (
    workspace / "registry.yaml",
    workspace / "AGENTS.md",
    workspace / ".claude" / "settings.json",
)
labels = ("registry", "map", "settings")
paths = {str(path.resolve()): label for label, path in zip(labels, inputs)}
counts = {label: 0 for label in labels}
original_open = Path.open


def counted_open(self, *args, **kwargs):
    label = paths.get(str(self.resolve()))
    if label is not None:
        counts[label] += 1
    return original_open(self, *args, **kwargs)


Path.open = counted_open
sys.path.insert(0, package_root)
sys.argv = ["workspace", "check", str(workspace)]
exit_code = 0
try:
    runpy.run_module("workspace", run_name="__main__", alter_sys=True)
except SystemExit as exc:
    exit_code = exc.code if isinstance(exc.code, int) else 1
finally:
    print(
        "OPEN_COUNTS registry=%d map=%d settings=%d"
        % (counts["registry"], counts["map"], counts["settings"])
    )
raise SystemExit(exit_code)
PY

pass=0
fail=0
ok() { printf 'ok    %s\n' "$1"; pass=$((pass + 1)); }
no() { printf 'FAIL  %s\n' "$1"; fail=$((fail + 1)); }

# mkws REGISTRY MAP SETTINGS: a fresh workspace folder in $WS. A part whose
# source does not exist is left out, so its absence is what the case tests.
ws_count=0
mkws() {
  ws_count=$((ws_count + 1))
  WS="$tmp/ws-$ws_count"
  mkdir -p "$WS/.claude"
  if [ -e "$1" ]; then cp "$1" "$WS/registry.yaml"; fi
  if [ -e "$2" ]; then cp "$2" "$WS/AGENTS.md"; fi
  if [ -e "$3" ]; then cp "$3" "$WS/.claude/settings.json"; fi
}

# run PACKAGE_ROOT REGISTRY MAP SETTINGS
run() {
  mkws "$2" "$3" "$4"
  OUT="$(PYTHONPATH="$1" python3 -P -m workspace check "$WS" 2>&1)"
  RC=$?
}

run_check() {
  run "$repo" "$1" "$2" "$3"
}

run_counted() {
  mkws "$2" "$3" "$4"
  OUT="$(python3 -P "$OPEN_COUNT_WRAPPER" "$1" "$WS" 2>&1)"
  RC=$?
}

contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

hash_of() { shasum -a 256 "$1" | awk '{print $1}'; }

ws_hash() {
  printf '%s-%s-%s' "$(hash_of "$1/registry.yaml")" "$(hash_of "$1/AGENTS.md")" \
    "$(hash_of "$1/.claude/settings.json")"
}

mutate() {
  python3 - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
old, new = sys.argv[2], sys.argv[3]
text = path.read_text()
if text.count(old) != 1:
    raise SystemExit("mutation target count was %s, expected 1" % text.count(old))
path.write_text(text.replace(old, new))
PY
}

# mutant_copy NAME: a scratch copy of the package; sets MUTANT_ROOT.
mutant_copy() {
  MUTANT_ROOT="$tmp/mutant-$1"
  mkdir -p "$MUTANT_ROOT"
  cp -R "$repo/workspace" "$MUTANT_ROOT/workspace"
}

echo "== check-workspace-registry self-test =="
echo

run_check "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 0 ] && contains "$OUT" "PASS  registry, map, and settings are consistent"; then
  ok "conforming fixture exits 0"
else
  no "conforming fixture exits 0 (rc=$RC out=$OUT)"
fi

run_check "$SEED_REG" "$SEED_MAP" "$SEED_SET"
if [ "$RC" -eq 0 ] && contains "$OUT" "PASS  registry, map, and settings are consistent"; then
  ok "example seed exits 0"
else
  no "example seed exits 0 (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$FIX/active-missing/map.md" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   active project \`alpha\` has no map line"; then
  ok "active project without a map line is a named GAP"
else
  no "active project without a map line is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$FIX/archived-own-line/map.md" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   archived project \`legacy\` has its own map line"; then
  ok "archived project with its own map line is a named GAP"
else
  no "archived project with its own map line is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$FIX/archived-arrow-own-line/map.md" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   archived project \`legacy\` has its own map line"; then
  ok "archived own line with a migration arrow is a named GAP"
else
  no "archived own line with a migration arrow is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/missing-lifecycle/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   project \`alpha\` is missing lifecycle"; then
  ok "project with missing lifecycle is a named GAP"
else
  no "project with missing lifecycle is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/unknown-lifecycle/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   project \`alpha\` has unknown lifecycle \`bogon\`"; then
  ok "project with unknown lifecycle is a named GAP"
else
  no "project with unknown lifecycle is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/unknown-lifecycle/list.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   project \`alpha\` has unknown lifecycle \`['bogon']\`" &&
  ! contains "$OUT" "Traceback"; then
  ok "project with list lifecycle is a named GAP without a traceback"
else
  no "project with list lifecycle is a named GAP without a traceback (rc=$RC out=$OUT)"
fi

run_check "$FIX/unknown-lifecycle/mapping.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   project \`alpha\` has unknown lifecycle \`{'a': 1}\`" &&
  ! contains "$OUT" "Traceback"; then
  ok "project with mapping lifecycle is a named GAP without a traceback"
else
  no "project with mapping lifecycle is a named GAP without a traceback (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/excluded-unenforced.json"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   excluded path \`~/scratch\` is not in sandbox.filesystem.denyRead" &&
  ! contains "$OUT" "is not in permissions.deny"; then
  ok "excluded path missing from denyRead is a named GAP (denyRead half only)"
else
  no "excluded path missing from denyRead is a named GAP (rc=$RC out=$OUT)"
fi

# The permissions.deny half: denyRead is complete in all three settings, so only
# the deny gap can fire. deny-missing has neither rule for ~/scratch, deny-read-only
# has Read but no Edit, deny-edit-only has Edit but no Read.
for deny_case in deny-missing deny-read-only deny-edit-only; do
  run_check "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/$deny_case.json"
  if [ "$RC" -eq 1 ] &&
    contains "$OUT" "GAP   excluded path \`~/scratch\` is not in permissions.deny" &&
    ! contains "$OUT" "sandbox.filesystem.denyRead" &&
    contains "$OUT" "== result: 1 gap =="; then
    ok "excluded path absent from permissions.deny is a named GAP ($deny_case)"
  else
    no "excluded path absent from permissions.deny is a named GAP ($deny_case; rc=$RC out=$OUT)"
  fi
done

run_check "$GREEN_REG" "$FIX/archived-compact-missing/map.md" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   archived project \`legacy\` is missing from the compact line" &&
  ! contains "$OUT" "has its own map line"; then
  ok "archived project missing from the compact line is a named GAP"
else
  no "archived project missing from the compact line is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/project-no-name/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   project entry is missing name"; then
  ok "project entry without a name is a named GAP"
else
  no "project entry without a name is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/project-not-mapping/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   project entry is not a mapping" &&
  ! contains "$OUT" "Traceback"; then
  ok "project entry that is not a mapping is a named GAP without a traceback"
else
  no "project entry that is not a mapping is a named GAP without a traceback (rc=$RC out=$OUT)"
fi

run_check "$FIX/excluded-no-path/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   excluded entry is missing path" &&
  ! contains "$OUT" "Traceback"; then
  ok "excluded entry without a path is a named GAP without a traceback"
else
  no "excluded entry without a path is a named GAP without a traceback (rc=$RC out=$OUT)"
fi

run_check "$FIX/duplicate-keys/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 1 ] && contains "$OUT" "GAP   duplicate YAML key: 'system'"; then
  ok "duplicate YAML keys are a named GAP"
else
  no "duplicate YAML keys are a named GAP (rc=$RC out=$OUT)"
fi

safe_load_rc="$(python3 - "$FIX/duplicate-keys/registry.yaml" <<'PY'
import sys
import yaml
yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print("safe_load kept the last key")
PY
)"
if [ "$?" -eq 0 ] && [ "$safe_load_rc" = "safe_load kept the last key" ]; then
  ok "yaml.safe_load silently accepts the duplicate-key fixture"
else
  no "yaml.safe_load silently accepts the duplicate-key fixture"
fi

# ---------------------------------------------------------------------------
# Unhashable keys, single-slash denies, per-cluster compact lines, and denyRead
# required only when the settings declare a sandbox block
# ---------------------------------------------------------------------------
run_check "$FIX/unhashable-key/registry.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 2 ] &&
  contains "$OUT" "UNKNOWN  registry has an unhashable mapping key (line 2, column 3)" &&
  ! contains "$OUT" "Traceback"; then
  ok "unhashable YAML key is UNKNOWN (exit 2) without a traceback"
else
  no "unhashable YAML key is UNKNOWN (exit 2) without a traceback (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/single-slash-deny.json"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   excluded path \`/var/private\` is not in permissions.deny" &&
  ! contains "$OUT" "sandbox.filesystem.denyRead" &&
  contains "$OUT" "== result: 1 gap =="; then
  ok "single-slash Read(/x/**) deny does not cover an absolute exclusion"
else
  no "single-slash Read(/x/**) deny does not cover an absolute exclusion (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 0 ] && contains "$OUT" "PASS  registry, map, and settings are consistent"; then
  ok "the //x/** deny form covers the same absolute exclusion"
else
  no "the //x/** deny form covers the same absolute exclusion (rc=$RC out=$OUT)"
fi

run_check "$FIX/archived-wrong-cluster/registry.yaml" "$FIX/archived-wrong-cluster/map.md" "$GREEN_SET"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   archived project \`legacy\` is missing from the compact line" &&
  ! contains "$OUT" "has its own map line" &&
  contains "$OUT" "== result: 1 gap =="; then
  ok "archived project listed only on another cluster's compact line is a named GAP"
else
  no "archived project listed only on another cluster's compact line is a named GAP (rc=$RC out=$OUT)"
fi

run_check "$FIX/template-settings/registry.yaml" "$GREEN_MAP" "$FIX/template-settings/settings.json"
if [ "$RC" -eq 0 ] && contains "$OUT" "PASS  registry, map, and settings are consistent"; then
  ok "shipped settings template (no sandbox block) with its deny pair passes"
else
  no "shipped settings template (no sandbox block) with its deny pair passes (rc=$RC out=$OUT)"
fi

run_check "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/sandbox-no-denyread.json"
if [ "$RC" -eq 1 ] &&
  contains "$OUT" "GAP   excluded path \`~/private/**\` is not in sandbox.filesystem.denyRead" &&
  contains "$OUT" "GAP   excluded path \`~/scratch\` is not in sandbox.filesystem.denyRead" &&
  contains "$OUT" "GAP   excluded path \`/var/private\` is not in sandbox.filesystem.denyRead" &&
  ! contains "$OUT" "is not in permissions.deny" &&
  contains "$OUT" "== result: 3 gap =="; then
  ok "settings with a sandbox block missing the path from denyRead is a named GAP"
else
  no "settings with a sandbox block missing the path from denyRead is a named GAP (rc=$RC out=$OUT)"
fi

# ---------------------------------------------------------------------------
# READ-ONLY — inputs byte-identical after PASS and GAP runs
# ---------------------------------------------------------------------------
run_check "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
ro_ws="$WS"
h1="$(ws_hash "$ro_ws")"
mkws "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
h0="$(ws_hash "$WS")"
if [ "$h0" = "$h1" ] && [ "$RC" -eq 0 ]; then
  ok "conforming run leaves inputs byte-identical"
else
  no "conforming run leaves inputs byte-identical"
fi
run_check "$GREEN_REG" "$FIX/active-missing/map.md" "$GREEN_SET"
h3="$(ws_hash "$WS")"
mkws "$GREEN_REG" "$FIX/active-missing/map.md" "$GREEN_SET"
h2="$(ws_hash "$WS")"
if [ "$h2" = "$h3" ] && [ "$RC" -eq 1 ]; then
  ok "GAP run leaves inputs byte-identical"
else
  no "GAP run leaves inputs byte-identical"
fi

# ---------------------------------------------------------------------------
# UNKNOWN must never masquerade as conforms
# ---------------------------------------------------------------------------
run_check "$tmp/missing.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN"; then
  ok "missing registry is UNKNOWN, not clean"
else
  no "missing registry is UNKNOWN, not clean (rc=$RC out=$OUT)"
fi
: >"$tmp/empty.yaml"
run_check "$tmp/empty.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN"; then
  ok "empty registry is UNKNOWN, not clean"
else
  no "empty registry is UNKNOWN, not clean (rc=$RC out=$OUT)"
fi
printf 'this: [is not\n  valid: yaml\n' >"$tmp/bad.yaml"
run_check "$tmp/bad.yaml" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN"; then
  ok "unparseable YAML is UNKNOWN, not clean"
else
  no "unparseable YAML is UNKNOWN, not clean (rc=$RC out=$OUT)"
fi
printf '{not json' >"$tmp/bad.json"
run_check "$GREEN_REG" "$GREEN_MAP" "$tmp/bad.json"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN"; then
  ok "unparseable JSON is UNKNOWN, not clean"
else
  no "unparseable JSON is UNKNOWN, not clean (rc=$RC out=$OUT)"
fi

run_counted "$repo" "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 0 ] &&
  contains "$OUT" "OPEN_COUNTS registry=1 map=1 settings=1"; then
  ok "each input is opened exactly once"
else
  no "each input is opened exactly once (rc=$RC out=$OUT)"
fi

cp "$GREEN_MAP" "$tmp/bad-utf8.md"
printf '\xff' >>"$tmp/bad-utf8.md"
run_check "$GREEN_REG" "$tmp/bad-utf8.md" "$GREEN_SET"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN" && contains "$OUT" "could not read map"; then
  ok "invalid UTF-8 in the map is UNKNOWN, not a traceback"
else
  no "invalid UTF-8 in the map is UNKNOWN, not a traceback (rc=$RC out=$OUT)"
fi

mkdir -p "$tmp/shadow"
printf 'raise ImportError("hidden")\n' >"$tmp/shadow/yaml.py"
mkws "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
OUT="$(PYTHONPATH="$tmp/shadow:$repo" python3 -P -m workspace check "$WS" 2>&1)"
RC=$?
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN  PyYAML is unavailable — cannot verify"; then
  ok "missing PyYAML is UNKNOWN, never pass"
else
  no "missing PyYAML is UNKNOWN, never pass (rc=$RC out=$OUT)"
fi

OUT="$(PYTHONPATH="$repo" python3 -P -m workspace 2>&1)"
RC=$?
if [ "$RC" -eq 3 ] && contains "$OUT" "Usage: workspace check WORKSPACE"; then
  ok "missing arguments are USAGE (3)"
else
  no "missing arguments are USAGE (3) (rc=$RC out=$OUT)"
fi

# ---------------------------------------------------------------------------
# Mutation proofs — scratch copy of the package, never the live worktree
# ---------------------------------------------------------------------------
prove_mutation() {
  local number="$1" property="$2" old="$3" new="$4"
  local red_reg="$5" red_map="$6" red_set="$7" needle="$8"
  mutant_copy "$number"
  if ! mutate "$MUTANT_ROOT/workspace/check.py" "$old" "$new"; then
    no "mutation $number could not be applied ($property)"
    return
  fi
  run "$MUTANT_ROOT" "$red_reg" "$red_map" "$red_set"
  if [ "$RC" -ne 1 ] || ! contains "$OUT" "$needle"; then
    ok "mutation $number RED ($property)"
    printf 'RED   %s: expected %s; mutant exited %s\n' "$property" "$needle" "$RC"
  else
    no "mutation $number stayed GREEN ($property; exit $RC out=$OUT)"
  fi
  run "$repo" "$red_reg" "$red_map" "$red_set"
  if [ "$RC" -eq 1 ] && contains "$OUT" "$needle"; then
    ok "mutation $number GREEN after restore ($property)"
    printf 'GREEN %s: %s\n' "$property" "$needle"
  else
    no "restored checker is RED ($property; exit $RC out=$OUT)"
  fi
}

prove_mutation 1 "active map line" \
  'if project_lifecycle == "active" and name not in own:' \
  'if False and project_lifecycle == "active" and name not in own:' \
  "$GREEN_REG" "$FIX/active-missing/map.md" "$GREEN_SET" \
  "GAP   active project \`alpha\` has no map line"

prove_mutation 2 "archived compact-only" \
  'if project_lifecycle == "archived":' \
  'if False and project_lifecycle == "archived":' \
  "$GREEN_REG" "$FIX/archived-own-line/map.md" "$GREEN_SET" \
  "GAP   archived project \`legacy\` has its own map line"

prove_mutation 3 "excluded deny and denyRead" \
  '        record_exclusion_gaps(gaps, path, deny_rules, deny_read, sandbox_declared)' \
  '        pass' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/excluded-unenforced.json" \
  "GAP   excluded path \`~/scratch\` is not in sandbox.filesystem.denyRead"

prove_mutation 4 "duplicate YAML keys" \
  '    return yaml.load(text, Loader=UniqueKeyLoader)' \
  '    return yaml.load(text, Loader=yaml.SafeLoader)' \
  "$FIX/duplicate-keys/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   duplicate YAML key: 'system'"

prove_mutation 5 "archived own line with arrow" \
  '        if match:
            names.add(match.group(1))' \
  '        if match and "->" not in line:
            names.add(match.group(1))' \
  "$GREEN_REG" "$FIX/archived-arrow-own-line/map.md" "$GREEN_SET" \
  "GAP   archived project \`legacy\` has its own map line"

prove_mutation 6 "required project lifecycle" \
  '            if lifecycle is None or lifecycle == "":' \
  '            if False:' \
  "$FIX/missing-lifecycle/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   project \`alpha\` is missing lifecycle"

prove_mutation 7 "non-string lifecycle" \
  '        if not isinstance(lifecycle, str) or lifecycle not in PROJECT_LIFECYCLES:' \
  '        if lifecycle not in PROJECT_LIFECYCLES:' \
  "$FIX/unknown-lifecycle/list.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   project \`alpha\` has unknown lifecycle \`['bogon']\`"

prove_mutation 8 "excluded path in permissions.deny" \
  '    if not permission_covers(deny_rules, path):' \
  '    if False:' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/deny-missing.json" \
  "GAP   excluded path \`~/scratch\` is not in permissions.deny"

prove_mutation 9 "excluded path in sandbox.filesystem.denyRead" \
  '    if sandbox_declared and not denyread_covers(deny_read, path):' \
  '    if False:' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/excluded-unenforced.json" \
  "GAP   excluded path \`~/scratch\` is not in sandbox.filesystem.denyRead"

prove_mutation 10 "Edit deny required (return has_read)" \
  '    return has_read and has_edit' \
  '    return has_read' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/deny-read-only.json" \
  "GAP   excluded path \`~/scratch\` is not in permissions.deny"

prove_mutation 11 "Read deny required (return has_edit)" \
  '    return has_read and has_edit' \
  '    return has_edit' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/deny-edit-only.json" \
  "GAP   excluded path \`~/scratch\` is not in permissions.deny"

prove_mutation 12 "Read and Edit both required (and to or)" \
  '    return has_read and has_edit' \
  '    return has_read or has_edit' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/deny-read-only.json" \
  "GAP   excluded path \`~/scratch\` is not in permissions.deny"

prove_mutation 13 "archived project in the compact line" \
  '            if not compact_listed(compact, declared, name):' \
  '            if False:' \
  "$GREEN_REG" "$FIX/archived-compact-missing/map.md" "$GREEN_SET" \
  "GAP   archived project \`legacy\` is missing from the compact line"

prove_mutation 14 "project entry has a name" \
  '        if not isinstance(name, str) or not name:' \
  '        if False:' \
  "$FIX/project-no-name/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   project entry is missing name"

prove_mutation 15 "project entry is a mapping" \
  '        if not isinstance(project, dict):' \
  '        if False:' \
  "$FIX/project-not-mapping/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   project entry is not a mapping"

prove_mutation 16 "excluded entry has a path" \
  '        if not path:' \
  '        if False:' \
  "$FIX/excluded-no-path/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  "GAP   excluded entry is missing path"

prove_mutation_exit() {
  local number="$1" property="$2" old="$3" new="$4"
  local red_reg="$5" red_map="$6" red_set="$7" want_rc="$8" needle="$9"
  mutant_copy "$number"
  if ! mutate "$MUTANT_ROOT/workspace/check.py" "$old" "$new"; then
    no "mutation $number could not be applied ($property)"
    return
  fi
  run "$MUTANT_ROOT" "$red_reg" "$red_map" "$red_set"
  if [ "$RC" -ne "$want_rc" ] || ! contains "$OUT" "$needle"; then
    ok "mutation $number RED ($property)"
    printf 'RED   %s: expected exit %s with %s; mutant exited %s: %s\n' \
      "$property" "$want_rc" "$needle" "$RC" "$(printf '%s\n' "$OUT" | tail -n 1)"
  else
    no "mutation $number stayed GREEN ($property; exit $RC out=$OUT)"
  fi
  run "$repo" "$red_reg" "$red_map" "$red_set"
  if [ "$RC" -eq "$want_rc" ] && contains "$OUT" "$needle"; then
    ok "mutation $number GREEN after restore ($property)"
    printf 'GREEN %s: exit %s with %s\n' "$property" "$RC" "$needle"
  else
    no "restored checker is RED ($property; exit $RC out=$OUT)"
  fi
}

prove_mutation_exit 17 "unhashable key hashability guard" \
  '        if not is_hashable(key):' \
  '        if False:' \
  "$FIX/unhashable-key/registry.yaml" "$GREEN_MAP" "$GREEN_SET" \
  2 "UNKNOWN  registry has an unhashable mapping key"

prove_mutation 18 "single-slash permission rule never covers an absolute path" \
  '    return {permission_form(form) for form in exclusion_forms(path)}' \
  '    return {permission_form(form) for form in exclusion_forms(path)} | exclusion_forms(path)' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/single-slash-deny.json" \
  "GAP   excluded path \`/var/private\` is not in permissions.deny"

prove_mutation 19 "archived project on its own cluster's compact line (not a union)" \
  '            if not compact_listed(compact, declared, name):' \
  '            if not any(name in names for names in compact.values()):' \
  "$FIX/archived-wrong-cluster/registry.yaml" "$FIX/archived-wrong-cluster/map.md" "$GREEN_SET" \
  "GAP   archived project \`legacy\` is missing from the compact line"

prove_mutation_exit 20 "denyRead required only when a sandbox block is declared" \
  '    if sandbox_declared and not denyread_covers(deny_read, path):' \
  '    if not denyread_covers(deny_read, path):' \
  "$FIX/template-settings/registry.yaml" "$GREEN_MAP" "$FIX/template-settings/settings.json" \
  0 "PASS  registry, map, and settings are consistent"

prove_mutation 21 "a declared sandbox block requires denyRead" \
  '    sandbox_declared = "sandbox" in settings' \
  '    sandbox_declared = False' \
  "$GREEN_REG" "$GREEN_MAP" "$tmp/settings/sandbox-no-denyread.json" \
  "GAP   excluded path \`~/scratch\` is not in sandbox.filesystem.denyRead"

mutant_copy single-read
if mutate "$MUTANT_ROOT/workspace/check.py" \
  '        document = load_registry_yaml(registry_text)' \
  '        document = load_registry_yaml(registry_path.read_text(encoding="utf-8"))'; then
  run_counted "$MUTANT_ROOT" "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
  if [ "$RC" -eq 0 ] &&
    contains "$OUT" "OPEN_COUNTS registry=2 map=1 settings=1"; then
    ok "mutation single-read RED (registry input is not reopened)"
    printf '%s\n' \
      "RED   single-read inputs: expected each input to open once; mutant reported registry=2 map=1 settings=1"
  else
    no "mutation single-read stayed GREEN (rc=$RC out=$OUT)"
  fi
else
  no "mutation single-read could not be applied"
fi
run_counted "$repo" "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
if [ "$RC" -eq 0 ] &&
  contains "$OUT" "OPEN_COUNTS registry=1 map=1 settings=1"; then
  ok "mutation single-read GREEN after restore"
  printf '%s\n' \
    "GREEN single-read inputs: OPEN_COUNTS registry=1 map=1 settings=1"
else
  no "restored checker is RED for single-read inputs (rc=$RC out=$OUT)"
fi

mutant_copy utf8
if mutate "$MUTANT_ROOT/workspace/check.py" \
  '    except (OSError, UnicodeDecodeError) as exc:' \
  '    except OSError as exc:'; then
  run "$MUTANT_ROOT" "$GREEN_REG" "$tmp/bad-utf8.md" "$GREEN_SET"
  if [ "$RC" -ne 2 ] && contains "$OUT" "UnicodeDecodeError"; then
    ok "mutation UTF-8 RED (invalid UTF-8 never a named gap)"
    printf 'RED   invalid UTF-8: expected UNKNOWN exit 2; mutant exited %s\n' "$RC"
  else
    no "mutation UTF-8 stayed GREEN (rc=$RC out=$OUT)"
  fi
else
  no "mutation UTF-8 could not be applied"
fi
run "$repo" "$GREEN_REG" "$tmp/bad-utf8.md" "$GREEN_SET"
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN" && contains "$OUT" "could not read map"; then
  ok "mutation UTF-8 GREEN after restore (invalid UTF-8 is UNKNOWN)"
  printf 'GREEN invalid UTF-8: UNKNOWN could not read map\n'
else
  no "restored checker is RED for invalid UTF-8 (rc=$RC out=$OUT)"
fi


mutant_copy pyyaml
if mutate "$MUTANT_ROOT/workspace/check.py" \
  '    sys.stdout.write("UNKNOWN  PyYAML is unavailable — cannot verify\n")
    sys.exit(2)' \
  '    sys.stdout.write("UNKNOWN  PyYAML is unavailable — cannot verify\n")
    sys.exit(0)'; then
  mkws "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
  OUT="$(PYTHONPATH="$tmp/shadow:$MUTANT_ROOT" python3 -P -m workspace check "$WS" 2>&1)"
  RC=$?
  if [ "$RC" -eq 0 ]; then
    ok "mutation PyYAML RED (missing PyYAML never pass)"
    printf 'RED   missing PyYAML never pass: expected exit 2; mutant exited %s\n' "$RC"
  else
    no "mutation PyYAML stayed GREEN (rc=$RC out=$OUT)"
  fi
else
  no "mutation PyYAML could not be applied"
fi
mkws "$GREEN_REG" "$GREEN_MAP" "$GREEN_SET"
OUT="$(PYTHONPATH="$tmp/shadow:$repo" python3 -P -m workspace check "$WS" 2>&1)"
RC=$?
if [ "$RC" -eq 2 ] && contains "$OUT" "UNKNOWN  PyYAML is unavailable — cannot verify"; then
  ok "mutation PyYAML GREEN after restore (missing PyYAML never pass)"
  printf 'GREEN missing PyYAML never pass: UNKNOWN  PyYAML is unavailable — cannot verify\n'
else
  no "restored checker is RED for missing PyYAML (rc=$RC out=$OUT)"
fi

echo
echo "== result: $pass pass / $fail fail =="
[ "$fail" -eq 0 ]
