# workspace

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Checks, and a generator that writes only on a confirm, for a workspace: one
folder you launch a coding agent from when work spans several repositories. The
workspace holds a registry of your projects, a short map an agent reads to get
its bearings, and the agent settings that keep it out of folders it must not
touch. One command, `workspace`, checks that the three agree and that the
repositories on disk match the registry, and renders the map and the settings
from the registry when you confirm the plan it shows.

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

One Python package and one command with six verbs. `check` and `scan` only
read; `gen`, `register` and `decline` print a plan and a plan hash, and write
only when you run them again with `--write --expect <hash>`; `prompt`, which
is off unless you turn it on, writes only its own state file:

- `workspace check WORKSPACE` proves that the registry, the map and
  `.claude/settings.json` agree: every active project has its own map line,
  archived projects appear only on their cluster's compact line, and every
  excluded path has a Read and an Edit deny rule. When the settings declare a
  `sandbox` block, it also checks that every excluded path is in
  `sandbox.filesystem.denyRead`. It also checks that no project folder
  reaches an excluded folder, as described under "What it does on your
  machine". In a workspace `gen` has written, it also renders the plan in
  memory and fails when `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json` or
  `.workspace-gen.json` differs from it (a stale map), naming each file and
  telling you to run `workspace gen WORKSPACE --host HOST`. It compares only
  the files that do not depend on a host, and writes nothing.
- `workspace scan WORKSPACE` lists this host's code root one level deep and
  reports what the registry does not account for: repositories it does not name
  and has not declined, registered repositories missing on this host, and
  archived repositories still on disk.
- `workspace scan WORKSPACE --edges` also proposes edges between registered
  repositories from their source files, and names approved edges whose evidence
  is gone.
- `workspace gen WORKSPACE` renders the map (`AGENTS.md`), the `CLAUDE.md`
  pointer, the code roots and deny rules in `.claude/settings.json`, and this
  host's `.code-workspace` file from the registry. What it writes always passes
  `check`. When the registry says `session_prompt: on`, it also adds a
  SessionStart hook that runs `workspace prompt`.
- `workspace register WORKSPACE PATH --cluster ID --one-liner TEXT` adds the
  repository at PATH to the registry, with the edges found between it and the
  registered repositories.
- `workspace decline WORKSPACE PATTERN --reason TEXT` records a remote or path
  pattern the registry should not propose again.
- `workspace prompt`, run by that hook from the workspace folder, prints at
  most one line when the code root holds repositories the registry does not
  know yet (see [The session prompt](#the-session-prompt-optional-off-by-default)).

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
| `AGENTS.md` | The map an agent reads: one line per project, with the projects it uses (its outgoing edges) as `uses:` on that line. `CLAUDE.md` is a one-line shim (`@AGENTS.md`) so Claude Code loads the same map. A workspace whose hand-kept map is still in `CLAUDE.md` works too: `check` reads `AGENTS.md` when it exists and `CLAUDE.md` otherwise. |
| `.claude/settings.json` | Claude Code's settings: the code roots as additional directories, a Read and an Edit deny rule for every excluded path, and, only if you turn it on, one SessionStart hook that runs `workspace prompt`. |

To start a workspace, copy [`docs/template/registry.yaml`](docs/template/registry.yaml),
fill it in, and run `workspace gen` (see [`docs/template/README.md`](docs/template/README.md)).

The design rests on a few decisions, in plain words:

- Each system you work on (home, work, a team) gets its own workspace, and a
  person may keep more than one. Every verb takes the workspace path; nothing
  works across workspaces.
- The registry is the source; the map and the settings must agree with it.
  Identity is the git remote, worktrees fold into their parent, and a project
  may be a folder inside a larger repository.
- Discovery is automatic and only proposes. `scan` reports; it never registers,
  declines or edits anything. `register` and `decline` change `registry.yaml`
  only on a confirm, and only `gen` writes the map.
- The map stays lazy: one line per project, and a project's own instructions
  are read only when the work touches it.

## What it does on your machine

`scan` gives a verdict only for a repository in the plain shape it can read safely: a `.git` folder, or a worktree whose `.git` file names `<repo>/.git/worktrees/<name>` for a repository found directly in the code root or registered, reached from the code root without a symlink, with a single plain `config` file that has no include section and does not turn on per-worktree config. Anything else it meets (a symlink, an odd git folder layout, an include, per-worktree config, an excluded entry reached by another name, a file that changes while it is read) ends the scan with exit 2 and names the reason. It never reads beneath an excluded path, outside the code root, or inside a repository beyond its git files, and `git` only ever parses config bytes handed to it on standard input.

The paragraph above is the promise of `scan`. `scan --edges` keeps it with two differences, both only inside registered repositories that are not archived. First, it also lists and reads source files there, walking each step from the repository the same way, judged by text and by identity against every excluded entry, never through a symlink, never into a nested repository, and never opening a file whose name starts with `.env`. Second, what it meets there does not end the scan with exit 2: a symlink or an excluded entry reached by another name is not followed or entered and is counted on one `INFO` line, and a file or folder that cannot be read safely, including one that changes while it is read, is not read and is named on a `WARN` line. Every repository-shape check, and everything outside those repositories, still ends the scan with exit 2 as above. It prints a candidate edge as a file path and line number, never the line. `gen`, `register` and `decline` print a plan and a plan hash and write nothing; run again with `--write --expect <hash>` to write exactly that plan. `gen` writes the map, the `CLAUDE.md` pointer, `.claude/settings.json` (only the code roots and the deny rules for excluded paths, and, only if you turn it on, one SessionStart hook that runs `workspace prompt`, never a `sandbox` block), the per-host `.code-workspace` file and its own manifest, and what it writes always passes `check`. `register` and `decline` change only `registry.yaml`. Nothing here opens a network connection.

**What it reads.**

- The workspace folder's `registry.yaml`, its map (`AGENTS.md`, or `CLAUDE.md`)
  and `.claude/settings.json`. `scan` reads only `registry.yaml`. `gen` reads
  `registry.yaml`, the map, `.claude/settings.json`, its manifest
  `.workspace-gen.json`, this host's `state/gen-<host>.json` and
  `<host>.code-workspace`; it never reads a repository, and it looks up (never
  opens) each excluded entry whose text holds a glob character.
- `register` reads the repository at PATH the way `scan` reads a repository:
  PATH is walked from the code root one step at a time and refused, before
  anything under it is touched, when a step is excluded, outside the code root,
  inside a registered repository, a symlink or missing; it then lists the code
  root and walks every registered path as `scan` does, and reads the
  repository's git files with the same reader. To draft edges it also reads
  source files inside PATH's repository and inside every registered, present,
  non-archived repository, exactly as `scan --edges` does.
- `decline` reads only `registry.yaml`.
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
  folder (`~/rooms`).
- Anything outside the code root, through a symlink or otherwise.
- Without `--edges`, the contents of a repository: `scan` lists or opens
  nothing inside a repository apart from its `.git` marker and the git files
  above, so `.env` files and other secrets in a repository are never read.
  `scan --edges` and `register` do read source files inside repositories, as
  described above; they never open a file whose name starts with `.env` (the
  name is judged before any file call), and they print evidence as a path and
  a line number, never the line.

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

**A project folder never reaches an excluded folder.** `gen` refuses, with exit
1 and nothing written, and `check` reports as a gap, a registry in which a
project that is not archived has a folder, on any host, that is an excluded
folder, holds one, or sits inside one. The folder is the project's path with
`~` expanded and its `subpath` joined, the same folder `gen` writes to the
`.code-workspace` file, so a `subpath` such as `../../rooms/x` is judged as the
folder it reaches. An excluded entry is judged as its folder, with a trailing
`/**`, `/*` or `/` dropped. The two are compared by identity, both ways: by
real path (symlinks resolved, a dangling symlink by the text of its target),
compared with letter case and Unicode composition folded and with a leading
`/System/Volumes/Data` (the macOS firmlink spelling) dropped; and by device and
inode, of the folder and of every parent folder the system reaches from it with
`..`. So a symlink such as `~/code/room-link` pointing into `~/rooms/private`, a
project folder that holds an excluded folder, another letter case, a firmlink, a
bind mount or volfs spelling of the same folder are all refused. A folder that
does not exist on this host is not refused for that alone; it is still judged by
its real path and by the folders above it that do exist. A folder or parent that
cannot be looked up for any other reason (no permission, a symlink loop) is a
could-not-verify: exit 2, nothing written. Code roots are not judged this way: an
excluded folder inside a code root is the usual layout.
`check` skips a project path that is null on a host, and a placeholder path
written as `<...>` (the template's), while `gen` refuses both with exit 2; any
other path that is neither absolute nor starts with `~` is a could-not-verify
in `check` too.

The code root itself is compared by its text. Write it as its real path, with
no symlinked folder above it: `git` records a worktree's git folder by its real
path, and a worktree whose git folder does not sit under the code root as
written ends the scan with exit 2.

**What it writes.** `check` and `scan` write nothing. `gen` writes, in the
workspace folder, `AGENTS.md`, `CLAUDE.md` (exactly `@AGENTS.md`),
`.claude/settings.json`, `<host>.code-workspace`, its manifest
`.workspace-gen.json` and `state/gen-<host>.json`. `register` and `decline`
write only `registry.yaml`, adding text and keeping every other byte, comments
included. Each writes only on `--write --expect <hash>`, and only the plan
whose hash that is: a plan that changed since you saw it is refused. Every
refusal is decided before the first write, and each file is replaced whole
(written beside it, then renamed). None of the verbs opens a network
connection or runs any program other than `git` reading config bytes.

The workspace folder you name is used as you typed it, even when it is a
symlink. Below it, every file `gen`, `register` or `decline` reads or replaces,
`registry.yaml` included, is opened once, without following a symlink and
without waiting on a FIFO, and must be a regular file; a symlink, folder, FIFO
or other special file there is refused with exit 1, naming it, before anything
is written, and is never read through or replaced. `scan` reads
`registry.yaml` the same way, and ends with exit 2 when it is not a regular
file. A file written takes its mode from that one read: it is never more
permissive than the file it replaces, and a new `AGENTS.md` whose keep blocks
come from `CLAUDE.md` takes `CLAUDE.md`'s mode. A file with neither gets the
usual mode for your umask. Edge evidence is stored in `registry.yaml` as the
real path and line number, quoted with every character outside printable ASCII
escaped so that it reads back as that path; what is printed to the terminal,
`register`'s plan included, has every backslash and every character that is not
printable escaped.

In `.claude/settings.json`, `gen` owns only the code roots in
`permissions.additionalDirectories`, a Read and an Edit deny rule for each
excluded path, and, when you have declared a `sandbox` block yourself, each
excluded path in `sandbox.filesystem.denyRead`; and, while the registry says
`session_prompt: on`, one entry in `hooks.SessionStart` whose command is
exactly `workspace prompt`. It never writes a `sandbox` block, and every other
key, rule and hook passes through. It removes only entries its manifest
records it owning, once their source leaves the registry. Another agent
reads `AGENTS.md` but does not get these deny rules: only Claude Code enforces
them.

Keep your own text in the map inside the two keep blocks, `<!-- workspace:keep
begin top -->` … `<!-- workspace:keep end top -->` and the same with `bottom`;
`gen` copies them byte for byte and rewrites everything else. A file `gen` did
not write is replaced only with `--adopt`, and a hand-kept map only when it
holds keep markers. A file changed outside `gen` since it last wrote it is
refused, `--adopt` or not.

Commit `registry.yaml`, the map, `CLAUDE.md`, `.claude/settings.json` and
`.workspace-gen.json`. The editor file and the per-host state hold this
machine's absolute paths: add `*.code-workspace` and `state/` to the
workspace's `.gitignore`.

## Known limits

These are not covered:

- A concurrent writer that swaps an intermediate step of a path between its
  check and the next step's check. The last step is covered: the file is
  opened without following a symlink and must be the file the walk found. With
  `--edges`, and in `register`, this holds inside repositories too; a folder is
  listed only when it is still the folder the walk found.
- An entry matched only by a glob other than a trailing `/*` or `/**`, not by
  the exclusion's literal text, is matched by its text only, so another letter
  case of it is not recognised. This holds inside repositories too, for
  `scan --edges` and `register`: an exclusion such as `~/src/app/priv-*` prunes
  `priv-notes` by its text, not by its identity.
- Edge detectors are name heuristics: a candidate can be wrong, and a real
  dependency can be missed.
- An approved edge whose evidence is prose, rather than `path:LINE`, is never
  checked for staleness; such edges are counted on one `INFO` line.
- A source file with more than one hard link, or larger than 400,000 bytes, is
  not read for edges.
- A project folder is compared with the excluded folders when `gen` or `check`
  runs; a symlink changed afterwards is not seen. A hard link to a file inside
  an excluded folder has no parent folder to compare, so it is not recognised.
- `gen`'s check that the map imports nothing inside a registered repository
  compares paths by their text, so an `@` import through a symlinked folder into
  a registered repository is not recognised.
- An excluded entry whose real name holds a `[` glob class (such as `[priv]`)
  cannot be denied by a Claude Code rule that matches its literal name; `gen`
  refuses such a workspace with exit 2 and names the entry, and an entry
  created with such a name after `gen` ran is not denied.
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
- Just before `gen`, `register` or `decline` renames a new file into place, it
  looks the name up again, without following a symlink, reads it again, and
  refuses unless it is still the file it read, with the same bytes, identity
  and mode (or still absent), so an edit in place is refused too. This byte
  recheck narrows the window between that recheck and the rename, which was
  there before it; a writer that changes the file inside that window is still
  not caught. Only a concurrent edit by the same user can do that. It cannot
  redirect the write or bring other bytes in, because a rename replaces the
  folder entry and never writes through a symlink, and the folder is held
  open; it can only lose its own change to that file.

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
workspace scan ~/workspaces/main --edges
workspace gen ~/workspaces/main                       # prints the plan and its hash
workspace gen ~/workspaces/main --write --expect HASH # writes exactly that plan
workspace register ~/workspaces/main ~/src/new-tool --cluster tools --one-liner "Builds things."
workspace decline ~/workspaces/main 'https://example.invalid/acme/scratch-*' --reason "throwaway"
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

With `--edges`, `scan` adds these lines after the ones above and before the
summary. Edge `WARN` lines never change the exit code and are not counted in
the summary's `warn` figure:

| Line | Meaning |
|------|---------|
| `WARN   candidate FROM → TO (KIND) PATH:LINE` | A build, source or runtime signal in FROM's files naming TO, with no approved and no rejected edge for the pair. PATH is relative to FROM's folder. |
| `WARN   stale FROM → TO (KIND) EVIDENCE` | An approved edge whose `path:LINE` evidence no detector finds any more. |
| `WARN   could not read PATH (REASON)` | A file or folder inside a repository that could not be read safely; it was not read. |
| `INFO   edge FROM → TO (KIND) not checked: REASON` | An approved edge whose FROM repository is not on this host, or is archived. |
| `INFO   N edges have prose evidence and are not checked for staleness` | Approved edges whose evidence is prose. |
| `INFO   edges: N symlinks not followed, M excluded entries not entered` | What the edge pass pruned, so nothing is skipped silently. |

`gen`, `register` and `decline` print the plan (a diff per file; for
`.claude/settings.json`, only the entries `gen` owns), then
`plan sha256=<hash>`. `register` also prints a `NOTE   look-alike:` line for a
project with the same folder name, a `-`/`_` variant or a near spelling,
suggesting `confused_with`. After `register --write` or `decline --write`, run
`workspace gen` to update the map.

### The session prompt (optional, off by default)

`workspace prompt` tells you, when a Claude Code session starts in the
workspace, that the code root holds repositories the registry does not know
yet. It is off by default, and `gen` offers it only through this one setting.
When it has something to say, it prints one line, such as:

```text
3 unregistered repos (newest: new-tool). Say 'register new-tool' or 'decline new-tool'
```

To turn it on, add this line at the top level of `registry.yaml`, then run
`workspace gen` and confirm its plan with `--write --expect HASH`:

```yaml
session_prompt: on
```

`gen` then adds one entry to `hooks.SessionStart` in `.claude/settings.json`,
whose command is exactly `workspace prompt`, with no path or registry text in
it. Claude Code runs it from the workspace folder, so `workspace` must be on
your `PATH`. Only the value `on` turns it on (quoted or not); any other value,
`On`, `true` and `yes` included, leaves it off, and `gen`'s plan says so on a
`note` line. To turn it off, remove the line, or set it to anything else, and
run `gen` again: it removes the entry it added and nothing else. A
SessionStart hook you wrote yourself is never changed or removed, even one that
runs the same command, and `gen` does not add a second copy beside it.

What `prompt` does:

- It prints at most one line. It counts the repositories in this host's code
  root that the registry neither names nor declines, and names the most
  recently active one. A name that is not plain letters, digits, `.`, `_` and
  `-`, up to 64 characters, is left out of the line rather than printed,
  because what a SessionStart hook prints becomes part of the session's
  context.
- It shows that line at most once a day, and not while it is snoozed. When this
  host's code root cannot be read, it prints one self-check line saying so
  instead. Its state records the day of the last finished scan, whether or not
  a line printed, so a second session on the same day stays quiet; a scan that
  fails or runs out of time records nothing, and the next session tries again.
- `workspace prompt --snooze DAYS` (1 to 365) keeps the line quiet until DAYS
  days from today. It prints nothing, and it only works while the opt-in is on.
- It never fails a session start: it exits 0 and writes nothing to standard
  error, and on any problem it prints nothing. Its only state is
  `state/prompt-<host>.json` in the workspace, written the way `gen` writes its
  files, and it prints its line only after that state is saved. Keep `state/`
  in the workspace's `.gitignore`.

A repository counts as registered when its remote matches a project's `remote`
(after normalising `https://`, `ssh://` and `user@host:` forms and a trailing
`.git`) or its folder is a project's path for this host. A `declined` entry's
`remote` matches a repository's remote, and may be a glob; a value that starts
with `/` or `~` is a path glob matched against the repository's folder.

### Exit codes

| Code | `check` | `scan` | `gen`, `register`, `decline` |
|------|---------|--------|------------------------------|
| 0 | consistent | no drift (`WARN` lines may be present) | plan printed, or written |
| 1 | at least one `GAP` | at least one `DRIFT` | refused (`REFUSED` and the reason); nothing written |
| 2 | could not verify | could not verify | could not verify; nothing written |
| 3 | usage error | usage error | usage error |

`gen`, `register` and `decline` read the registry as `scan` does, so a registry
`scan` cannot read (a repeated key, a field outside the grammar), a host missing
from `code_roots`, or a code root `scan` cannot verify is exit 2 for them too;
so is a repository `register` cannot identify (no `.git`, no origin remote, an
include or per-worktree config). A refused PATH, a registry the map cannot
carry, output that would fail `check`, a file changed outside `gen`, or a plan
hash that no longer matches is exit 1. A `gen --write` that stops part way
(for example, a full disk) is exit 2; every file then holds its old content or
the planned one, and the next `gen` finishes it.

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
inside of a repository, and that `git` is never handed a path. The `gen`
tests run `check` on what `gen` writes for 200 generated registries and every
excluded-path form, and inject a crash at each step of a write; the edge and
`register` tests record every file call the same way, to prove that nothing
beneath an excluded entry, through a symlink or named `.env*` is touched.

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

Pull request titles are conventional commits, because the squash-merge title
becomes the commit that release-please reads to choose the next version and to
write the changelog. Write the title as `type(scope)!: subject`. The type is one
of `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `build`, `perf`,
`style` or `revert`, in lower case. The scope in parentheses and the `!` that
marks a breaking change are optional, and a colon and a space come before the
subject. The `pr-title` check fails a title that does not read this way.

## License

MIT. See [LICENSE](LICENSE).
