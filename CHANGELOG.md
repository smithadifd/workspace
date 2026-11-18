# Changelog

## Unreleased

The first version: one command, `workspace`, with two read-only verbs.

- **`workspace check WORKSPACE`** proves that a workspace's `registry.yaml`, its map and `.claude/settings.json` agree: every active project has its own map line, archived projects appear only on their cluster's compact line, and every excluded path has a Read and an Edit deny rule, plus a `sandbox.filesystem.denyRead` entry when the settings declare a `sandbox` block. Duplicate YAML keys are a gap; an unhashable key, a missing or unreadable input, or missing PyYAML is UNKNOWN (exit 2). The map is `AGENTS.md`, or `CLAUDE.md` when no `AGENTS.md` exists.
- **`workspace scan WORKSPACE [--host HOST]`** lists this host's code root one level deep and reports unregistered repositories that are not declined (drift), registered repositories missing on this host (a warning, or drift when `required_on` names the host), and archived repositories still on disk (known). A `.git` file is a worktree and folds into its parent: scan resolves the git folders itself, walking each path from the code root, and `git` only reads config bytes on its standard input with includes off; a config with an include section is a could-not-verify. Excluded paths, the outside of the code root and the inside of registered repositories are never listed or opened; symlinks are followed only while every hop stays clear of them. A registry field outside the grammar, a git error, or a missing code root exits 2 with the reason printed.
- CI runs both test suites on `ubuntu-latest`.
