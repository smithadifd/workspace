# Security policy

## Reporting a vulnerability

Report a vulnerability privately through the "Report a vulnerability" button on this repository's Security tab. Private vulnerability reporting is enabled, and the report reaches the maintainer without a public record. Do not open a public issue for a security problem.

Say which verb is affected, what you ran, and what happened. The output, the exit code and a registry that shows the problem (with your own paths replaced) help. You will get a reply in the private report.

## Scope

`check` and `scan` promise to read only: they write nothing and open no network connection. `scan` also promises never to list, open or inspect a path the registry excludes, anything outside the host's code root, or anything inside a registered repository beyond its `.git` marker. A report that either verb writes a file, reaches an excluded path or the outside of the code root (through a symlink, a worktree's git dir or otherwise), reads a file inside a repository, or reports a state it could not verify as a pass is in scope.

`scan` never runs `git` on a repository. To read a repository's remote it hands `git` only the bytes of that repository's `config` file on standard input, with includes and the system and global config switched off. A config with an `[include]` or `[includeIf]` key ends the scan with exit 2 as could-not-verify, and the file it names is never opened. A report that `scan` follows an include, or reads a file one names, is in scope.

## Supported versions

Fixes go to the latest release. Update to the latest tag before reporting; a problem that is already fixed there is closed with a pointer to the fix.
