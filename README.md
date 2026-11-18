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

`scan` gives a verdict only for a repository in the plain shape it can read safely: a `.git` folder, or a worktree whose `.git` file names `<repo>/.git/worktrees/<name>` for a repository found directly in the code root or registered, reached from the code root without a symlink, with a single plain `config` file that has no include section and does not turn on per-worktree config. Anything else it meets (a symlink, an odd git folder layout, an include, per-worktree config, an excluded entry reached by another name, a file that changes while it is read) ends the scan with exit 2 and names the reason. It never reads beneath an excluded path, outside the code root, or inside a repository beyond its git files, and `git` only ever parses config bytes handed to it on standard input.

**What it reads.**

- The workspace folder's `registry.yaml`, its map (`AGENTS.md`, or `CLAUDE.md`)
  and `.claude/settings.json`. `scan` reads only `registry.yaml`.
- Which host it is on: the `code_roots` key that `--host` names. Without
  `--host`, `scan` uses the registry's only host when `code_roots` names exactly
  one; when it names several, `scan` exits 2 and names them. It never guesses
  from the machine's host name.
- The code root that the registry names for this host, one level deep: the
  names in that folder, and for each entry whether it holds a `.git` folder or a
  `.git` file. For each repository it finds, `scan` reads three git files
  itself: a worktree's `.git` file, the `commondir` file that names the common
  folder, and the `config` there. A worktree folds into the repository its
  common folder belongs to. A repository's own `.git` folder must not hold a
  `commondir` file (only a worktree's git folder has one), a `.git` file must
  hold only its `gitdir:` line, and a repository must have its `config`; each
  of these ends the scan with exit 2 otherwise.
- Each of those files is reached by walking its path from the code root one
  step at a time, as written in the file that names it, the way the operating
  system walks it. Every step must exist, must not be a symlink, must stay
  inside the code root (a `..` step never climbs above it) and must not be an
  excluded entry. A file is opened without following a symlink, and read only
  when it is the same regular file the walk found, with one link and at most
  1 MiB, so a FIFO, a hard link (which could name a file in an excluded folder)
  or a file swapped in after the walk is refused before any byte is read. A
  file that changes while it is read is refused too. Any of these ends the scan
  with exit 2.
- A worktree's git folder must be `<repo>/.git/worktrees/<name>`, where
  `<repo>` is a repository found directly in the code root or registered. This
  is judged before anything in that folder is read, so a worktree of an
  unregistered repository below the code root ends the scan with exit 2.
- `git` never runs on a repository and is never given a repository path. It
  only reads the bytes of a `config` file passed on its standard input, with
  includes switched off, with the system and global git config switched off,
  with every `GIT_` variable from your environment
  removed, and from the filesystem root, so the folder you run `scan` from
  plays no part. First `git`'s own parser lists any `[include]` or
  `[includeIf]` key in those bytes: a config with one ends the scan with exit
  2, and the file it names is never opened. A repository that uses
  per-worktree config (`extensions.worktreeConfig` true) is a could-not-verify:
  the scan ends with exit 2. Then `git` lists every `remote.origin.url` value:
  none means the repository has no remote, one non-empty value is its remote,
  and an empty value or more than one ends the scan with exit 2.
- A symlink in the code root is never followed: unless it is itself an
  excluded entry, it ends the scan with exit 2. To scan a code root that holds
  one, list the symlink under `excluded`. A registered path is walked the same
  way, with no symlink, and a registered repository at any depth below the
  code root passes the same shape check as one found directly in it.

**What it never reads.**

- Anything beneath a path the registry lists under `excluded`. An exclusion
  written as a bare folder, as `folder/*` or as `folder/**` covers that folder
  and everything beneath it. That is where you list agent scratch folders,
  worktree copies, and folders that hold live personal data, such as a rooms
  folder.
- Anything outside the code root, through a symlink or otherwise.
- The contents of a repository. Nothing inside a repository is listed or opened
  apart from its `.git` marker and the git files above, so `.env` files and
  other secrets in a repository are never read.

An excluded path is recognised two ways. By its text, after `~` expands to your
home folder: a path is checked against the excluded list before any file call
reaches it. And by identity: `scan` looks up each excluded entry that exists
(the entry itself, nothing beneath it) and refuses any step of a walk that
lands on the same folder or file, whatever it is called. So an exclusion
written `~/Code/private` still covers `~/code/private` on a case-insensitive
disk, and an exclusion reached through a symlinked parent still covers the
folder it names. An excluded path is also matched by its literal text, so a
folder whose real name holds a glob character, such as `[priv]`, is covered by
its text and by its identity. Any other entry that a glob character other than
the trailing `/*` or `/**` matches is matched by its text only.

The code root itself is compared by its text. Write it as its real path, with
no symlinked folder above it: `git` records a worktree's git folder by its real
path, and a worktree whose git folder does not sit under the code root as
written ends the scan with exit 2.

**What it writes.** Nothing. `check` and `scan` only read, and neither opens a
network connection.

## Known limits

These are not covered:

- A concurrent writer that swaps an intermediate step of a path between its
  check and the next step's check. The last step is covered: the file is
  opened without following a symlink and must be the file the walk found.
- An entry matched only by a glob other than a trailing `/*` or `/**`, not by
  the exclusion's literal text, is matched by its text only, so another letter
  case of it is not recognised.
- Each excluded entry that exists is looked up, including entries outside the
  code root, to learn its identity. It is never opened.
- A rewrite of a file that keeps its inode and size is caught only by its
  modification and change times. On file systems with coarse timestamps (Linux
  ext4 and similar, about 1 to 4 ms), a rewrite in the same tick as the file's
  previous change, or a write through a memory map that has not updated the
  times yet, can go unseen. A rewrite after the read is a later read.
- The code root is compared by its text, so a symlinked code root, or a
  symlinked folder above it (such as `/tmp` on macOS), is followed. Write the
  code root as its real path.

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

`scan` picks this host's code root from the registry's `code_roots`, as
[What it does on your machine](#what-it-does-on-your-machine) describes. It
prints one line per finding:

| Line | Meaning |
|------|---------|
| `DRIFT  unregistered repo at PATH (REMOTE)` | A repository the registry neither names nor declines. |
| `DRIFT  project NAME is missing on host HOST, which its required_on names: PATH` | A registered repository that `required_on` says must be on this host, and is not. |
| `DRIFT  project NAME is required on host HOST but has no path for it` | A project whose `required_on` names this host, but which records no path for this host. |
| `WARN   project NAME is missing on host HOST: PATH` | A registered repository not on this host. |
| `KNOWN  archived project NAME is present at PATH` | An archived repository still on disk: known, not drift. |
| `INFO   declined repo at PATH (REMOTE)` | A repository you declined. |
| `INFO   worktree PATH folds into PARENT` | A worktree, counted as its parent repository. |
| `SKIP   PATH: REASON` | An excluded entry in the code root, never entered. |

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
grammar, a host missing from `code_roots`, no `--host` when `code_roots` names
several hosts, a code root it cannot list, a symlink in the code root that is
not excluded, a registered path outside the code root, under an excluded path,
inside another repository or through a symlink, a git file whose path is
excluded, leaves the code root, passes through a symlink or a missing step, or
that is not a regular file, has more than one link, is larger than 1 MiB or
changes while it is read, a repository `.git` folder that holds a `commondir`,
a `.git` file with more than its `gitdir:` line, a worktree git folder that is
not `<repo>/.git/worktrees/<name>` for a repository found directly in the code
root or registered, a missing git config, a git config with an include section
or with per-worktree config, an empty or repeated `remote.origin.url`, a
registered repository at any depth that is not in the plain shape, and any
`git` error. An unknown field is
refused rather than skipped, so a typo cannot quietly turn a rule off.

## How it is built

The standard library plus PyYAML; the CLI is the only interface. Each promise
above is a test that runs in CI on hosted runners: the `check` self-test
(`tests/check-workspace-registry.test.sh`) proves each property against a
conforming fixture and a failing one, and mutates each guarded line on a scratch
copy to show the test fails without it. The `scan` tests build a code root in a
temp folder with a worktree, a repository nested inside a registered one, a
declined repository, an archived one on disk, a missing one, a symlink out of
the root and an excluded folder, and record every path the scan hands to a file
call, and every `git` run, to prove it never touches anything beneath the
excluded folder (by text and by identity), the outside of the root, or the
inside of a repository, and that `git` is never handed a path.

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
- [git worktrees](https://git-scm.com/docs/git-worktree) and the
  [repository layout](https://git-scm.com/docs/gitrepository-layout) (the
  `.git` file and the `commondir` file): how a worktree is folded into the
  repository it belongs to.
- [git config](https://git-scm.com/docs/git-config) with `--file -` and
  `--no-includes`: reading one setting from config bytes without following an
  include.
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
