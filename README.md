# workspace

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read-only checks for a workspace: one folder you launch a coding agent from when
work spans several repositories. The workspace holds a registry of your projects,
a short map an agent reads to get its bearings, and the agent settings that keep
it out of folders it must not touch. One command, `workspace`, checks that the
three agree and that the repositories on disk match the registry.

## Why

Launching an agent inside one repository misses its neighbours. Launching it in
your home folder hands it everything, including folders that hold personal data
or other people's work. A workspace sits between the two: a small folder outside
every code folder, holding a one-line-per-project map and settings that grant
the code folders and deny the rest. The agent reads the map, picks the
repositories a task needs, and reads a repository's own instructions only when
the work reaches it.

That only works while the map is true. Repositories get cloned, retired and
moved; a folder that must stay private gets added to the registry but not to the
settings; a project is archived in one file and still listed in another. Nothing
fails loudly when that happens. The agent just works from a wrong picture, or
gets read access it should not have. `workspace` makes those gaps visible.

## What this is

One Python package and one command with two verbs, both read-only:

- `workspace check WORKSPACE` proves that the registry, the map and
  `.claude/settings.json` agree: every active project has its own map line,
  archived projects appear only on their cluster's compact line, and every
  excluded path has a Read and an Edit deny rule. When the settings declare a
  `sandbox` block, it also checks that every excluded path is in
  `sandbox.filesystem.denyRead`.
- `workspace scan WORKSPACE` lists this host's code root one level deep and
  reports what the registry does not account for: repositories it does not name
  and has not declined, registered repositories missing on this host, and
  archived repositories still on disk.

It needs Python 3.11 or newer and PyYAML, its one dependency. It does not
depend on any agent runtime: the registry and the map are plain files, and any
agent can read the map. Another agent reads the map, but it does not get the
confinement that Claude Code applies from `.claude/settings.json`; only Claude
Code enforces those deny rules.

## How it fits together

A workspace folder holds three files:

| File | What it is |
|------|------------|
| `registry.yaml` | The machine-readable list: code roots per host, excluded paths, clusters, projects (identity is the git remote; paths are recorded per host), edges between projects, and the repositories and edges you declined. |
| `AGENTS.md` | The map an agent reads: one line per project and one per edge. `CLAUDE.md` is a one-line shim (`@AGENTS.md`) so Claude Code loads the same map. A workspace whose hand-kept map is still in `CLAUDE.md` works too: `check` reads `AGENTS.md` when it exists and `CLAUDE.md` otherwise. |
| `.claude/settings.json` | Claude Code's settings: the code roots as additional directories, and a Read and an Edit deny rule for every excluded path. |

The design rests on a few decisions, in plain words:

- Each system you work on (home, work, a team) gets its own workspace, and a
  person may keep more than one. Every verb takes the workspace path; nothing
  works across workspaces.
- The registry is the source; the map and the settings must agree with it.
  Identity is the git remote, worktrees fold into their parent, and a project
  may be a folder inside a larger repository.
- Discovery is automatic and only proposes. `scan` reports; it never registers,
  declines or edits anything.
- The map stays lazy: one line per project, and a project's own instructions
  are read only when the work touches it.

Later verbs (`gen` to render the map and settings from the registry,
`register` and `decline`) are planned and are not in this release.

## What it does on your machine

**What it reads.**

- The workspace folder's `registry.yaml`, its map (`AGENTS.md`, or `CLAUDE.md`)
  and `.claude/settings.json`. `scan` reads only `registry.yaml`.
- The code root that the registry names for this host, one level deep: the
  names in that folder, and for each entry whether it holds a `.git` folder or a
  `.git` file. For each repository it finds, `git` reads the remote URL from the
  repository's config, and for a worktree `git rev-parse --git-common-dir` finds
  its parent. `git` runs with the system and global git config switched off.
- Symlinks in the code root are followed hop by hop, and only while every hop
  stays inside the code root, outside every excluded path and outside every
  repository (registered, or found directly under the code root).

**What it never reads.**

- Any path the registry lists under `excluded`, nor anything under one. A path
  is checked against the excluded list by its text before any file call reaches
  it. That is where you list agent scratch folders, worktree copies, and folders
  that hold live personal data, such as a rooms folder.
- Anything outside the code root, through a symlink or otherwise.
- The contents of a repository. Nothing inside a repository is listed or opened
  apart from its `.git` marker and, through `git`, its config, so `.env` files
  and other secrets in a repository are never read.

Paths are compared as written, after `~` expands to your home folder. Write the
code root and the excluded paths in the same form (both through `~`, or both
absolute through the same folder names), because a path that reaches the same
folder by another name, through a symlinked parent, is not recognised as the
same path.

**What it writes.** Nothing. `check` and `scan` only read, and neither opens a
network connection.

## Install

You need Python 3.11 or newer. Install with [pipx](https://pipx.pypa.io/), which
puts `workspace` on your `PATH` in an environment of its own:

```sh
pipx install git+https://github.com/smithadifd/workspace
workspace --version
```

To work on it, install from a clone into a virtual environment:

```sh
git clone https://github.com/smithadifd/workspace
cd workspace
python3 -m venv .venv
.venv/bin/python -m pip install . pytest
.venv/bin/workspace --version
```

From a clone with PyYAML available, `python3 -m workspace <verb>` is the same as
`workspace <verb>`.

## Usage

```sh
workspace check ~/workspaces/main
workspace scan ~/workspaces/main
workspace scan ~/workspaces/main --host desk
```

`check` prints one `GAP` line per inconsistency, or a `PASS` line:

```text
== check-workspace-registry ==
GAP   active project `shop-web` has no map line
GAP   excluded path `~/scratch` is not in permissions.deny
== result: 2 gap ==
```

`scan` picks this host's code root from the registry's `code_roots`. The host
name is the machine's short host name unless `--host` names another key of
`code_roots`. It prints one line per finding:

| Line | Meaning |
|------|---------|
| `DRIFT  unregistered repo at PATH (REMOTE)` | A repository the registry neither names nor declines. |
| `DRIFT  project NAME is missing on host HOST, which its required_on names: PATH` | A registered repository that `required_on` says must be on this host, and is not. |
| `WARN   project NAME is missing on host HOST: PATH` | A registered repository not on this host. |
| `KNOWN  archived project NAME is present at PATH` | An archived repository still on disk: known, not drift. |
| `INFO   declined repo at PATH (REMOTE)` | A repository you declined. |
| `INFO   worktree PATH folds into PARENT` | A worktree, counted as its parent repository. |
| `SKIP   PATH: REASON` | An entry not followed: an excluded path, a symlink out of the code root, or a symlink into a repository. |

A repository counts as registered when its remote matches a project's `remote`
(after normalising `https://`, `ssh://` and `user@host:` forms and a trailing
`.git`) or its folder is a project's path for this host. A `declined` entry's
`remote` matches a repository's remote, and may be a glob; a value that starts
with `/` or `~` is a path glob matched against the repository's folder.

### Exit codes

| Code | `check` | `scan` |
|------|---------|--------|
| 0 | consistent | no drift (`WARN` lines may be present) |
| 1 | at least one `GAP` | at least one `DRIFT` |
| 2 | could not verify | could not verify |
| 3 | usage error | usage error |

Exit 2 always prints `UNKNOWN` and the reason, and it is never a pass. `scan`
exits 2 on a registry it cannot read or parse, a field outside the registry's
grammar, a host missing from `code_roots`, a code root it cannot list, a
registered path outside the code root or under an excluded path, and any `git`
error. An unknown field is refused rather than skipped, so a typo cannot quietly
turn a rule off.

## How it is built

The standard library plus PyYAML; the CLI is the only interface. Each promise
above is a test that runs in CI on hosted runners: the `check` self-test
(`tests/check-workspace-registry.test.sh`) proves each property against a
conforming fixture and a failing one, and mutates each guarded line on a scratch
copy to show the test fails without it. The `scan` tests build a code root in a
temp folder with a worktree, a repository nested inside a registered one, a
declined repository, an archived one on disk, a missing one, a symlink out of
the root and an excluded folder, and record every path the scan hands to a file
call or to `git` to prove it never touches the excluded folder, the outside of
the root, or the inside of a repository.

```sh
python3 -m pytest -q
bash tests/check-workspace-registry.test.sh
```

## Built on

- [Claude Code settings](https://docs.anthropic.com/en/docs/claude-code/settings):
  `permissions.additionalDirectories`, the Read and Edit deny rules and their
  path forms (`~/...`, `//absolute`), and the `sandbox` block that `check`
  reads when it is present.
- [AGENTS.md](https://agents.md/): one plain Markdown file any agent can read,
  which is why the map is `AGENTS.md` and `CLAUDE.md` is only a pointer to it.
- [git worktrees](https://git-scm.com/docs/git-worktree) and
  `git rev-parse --git-common-dir`: how a worktree is folded into the
  repository it belongs to.
- [YAML](https://yaml.org/) through [PyYAML](https://pyyaml.org/), loaded with
  the safe loader and a constructor that refuses duplicate keys, because a
  repeated key in a plain YAML load silently keeps only the last value.

## Contributing and support

To report a problem or ask a question, open an issue. Say which verb it
concerns, what you ran, what you expected and what happened; the output and the
exit code help most. Report a security problem privately instead, as
[SECURITY.md](SECURITY.md) describes.

To propose a change, open a pull request from a branch. Keep it to one change,
and add or update a test that fails without it. Both test commands above must
pass.

## License

MIT. See [LICENSE](LICENSE).
