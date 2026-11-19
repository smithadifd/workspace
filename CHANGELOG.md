# Changelog

## [0.1.1](https://github.com/smithadifd/workspace/compare/v0.1.0...v0.1.1) (2026-11-19)


### Bug Fixes

* **gen:** refuse a project path that reaches an excluded folder ([#6](https://github.com/smithadifd/workspace/issues/6)) ([d10a1db](https://github.com/smithadifd/workspace/commit/d10a1db5da066c163733b2bf671ca76413b7f556))

## 0.1.0 (2026-11-19)

The first version: one command, `workspace`, with two read-only verbs and three that write only on a confirm.

- **`workspace check WORKSPACE`** proves that a workspace's `registry.yaml`, its map and `.claude/settings.json` agree: every active project has its own map line, archived projects appear only on their cluster's compact line, and every excluded path has a Read and an Edit deny rule, plus a `sandbox.filesystem.denyRead` entry when the settings declare a `sandbox` block. Duplicate YAML keys are a gap; an unhashable key, a missing or unreadable input, or missing PyYAML is UNKNOWN (exit 2). The map is `AGENTS.md`, or `CLAUDE.md` when no `AGENTS.md` exists.
- **`workspace scan WORKSPACE [--host HOST]`** lists this host's code root one level deep and reports unregistered repositories that are not declined (drift), registered repositories missing on this host (a warning, or drift when `required_on` names the host), and archived repositories still on disk (known). A `.git` file is a worktree and folds into its parent: scan resolves the git folders itself, walking each path from the code root, and `git` only reads config bytes on its standard input with includes off; a config with an include section is a could-not-verify. Excluded paths, the outside of the code root and the inside of registered repositories are never listed or opened. A registry field outside the grammar, a git error, or a missing code root exits 2 with the reason printed.
- `scan` now gives a verdict only for repositories in the plain shape it can read safely; a symlink in the code root, a repository that uses per-worktree config, a missing or hardlinked config, `.git` file or `commondir` (a copy made with `cp -al` included), an odd git folder layout, an empty or repeated origin URL, a worktree of an unregistered repository below the code root, and a registered repository below the code root that is not in the plain shape are now could-not-verify (exit 2).
- CI runs both test suites on `ubuntu-latest`.
- **`gen`, `register` and `decline`** are new verbs that print a plan and write only on `--write --expect <plan hash>`; `gen`'s output always passes `check` (absolute excluded paths are written as `//path/**`), and **`scan --edges`** proposes edges from files inside registered repositories, walked like every other scan path and never opening a `.env*` file.
- **`gen` can add a SessionStart hook that runs `workspace prompt`**, off by default: with `session_prompt: on` at the top level of `registry.yaml`, `gen` adds one hook entry it owns, whose command is exactly `workspace prompt`, and removes only that entry once the key is gone or holds any other value; a SessionStart hook you wrote is never touched.

### Features

* add gen, register, decline and scan --edges ([#2](https://github.com/smithadifd/workspace/issues/2)) ([05d6342](https://github.com/smithadifd/workspace/commit/05d63420820c8621c068d682bebefca04c85c926))
* add the opt-in workspace prompt session-start line ([#3](https://github.com/smithadifd/workspace/issues/3)) ([ad62698](https://github.com/smithadifd/workspace/commit/ad626987bcffae6f9e24541f7ddd849c85d9a252))
* add the workspace check and scan verbs ([#1](https://github.com/smithadifd/workspace/issues/1)) ([9ddb44c](https://github.com/smithadifd/workspace/commit/9ddb44c80ba761954bb26f37d7a1f988ca808944))
