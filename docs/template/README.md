# Starting a workspace

A workspace is one folder you launch a coding agent from when work spans several
repositories. Put it outside every code root, and no code root inside it: an
agent loads every `CLAUDE.md` from the folder it starts in up to the root, so a
workspace inside a code root picks up that folder's instructions too.

1. Create the folder and copy [`registry.yaml`](registry.yaml) into it. Fill in
   the system name, a host name and code root for each machine, and the paths
   sessions must never read under `excluded`. Remove the example cluster and
   project, or replace them with real ones.
2. Add the repositories already on disk:

   ```sh
   workspace scan ~/workspaces/main
   workspace register ~/workspaces/main ~/src/shop-web --cluster shop --one-liner "Storefront."
   ```

   `scan` lists the repositories the registry does not name. `register` prints
   the entry it would add and a plan hash; run it again with
   `--write --expect <hash>` to add it. Use `decline` for a repository that
   should stay off the map.
3. Render the map and the settings:

   ```sh
   workspace gen ~/workspaces/main
   workspace gen ~/workspaces/main --write --expect <hash>
   ```

   `gen` writes `AGENTS.md`, `CLAUDE.md` (a one-line pointer to `AGENTS.md`),
   `.claude/settings.json`, `<host>.code-workspace` and its manifest. Write
   anything you want kept in the map inside its `top` or `bottom` keep block.
4. Add `*.code-workspace` and `state/` to the workspace's `.gitignore`, and
   commit the rest. `workspace check` proves the registry, the map and the
   settings agree.

Moving a hand-kept map in: put `<!-- workspace:keep begin top -->` and
`<!-- workspace:keep end top -->` around what you want kept (and the same with
`bottom` for text that belongs at the end), then run `workspace gen --adopt`.
Without keep markers, `gen` refuses to replace a hand-kept map.
