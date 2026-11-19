"""The `workspace` command line: the only interface to this package.

    workspace check WORKSPACE
    workspace scan WORKSPACE [--host HOST] [--edges]
    workspace gen WORKSPACE [--host HOST] [--adopt] [--write --expect HASH]
    workspace register WORKSPACE PATH --cluster ID --one-liner TEXT [--name NAME]
                       [--lifecycle LIFECYCLE] [--host HOST] [--write --expect HASH]
    workspace decline WORKSPACE PATTERN --reason TEXT [--write --expect HASH]
    workspace prompt [--snooze DAYS]

WORKSPACE is the workspace folder that holds `registry.yaml`, the map and
`.claude/settings.json`. `check` and `scan` only read. `gen`, `register` and
`decline` print a plan and its hash and write nothing; with `--write --expect
HASH` they write exactly the plan whose hash is HASH.

`prompt` takes no WORKSPACE: the opt-in SessionStart hook runs it in the
workspace folder. It always exits 0 and prints at most one line.

Exit codes: 0 conforms or done, 1 gap, drift or refusal, 2 could not verify, 3 usage.
"""
import re
import sys

from workspace import __version__

EX_USAGE = 3

USAGE = """\
Usage: workspace check WORKSPACE
       workspace scan WORKSPACE [--host HOST] [--edges]
       workspace gen WORKSPACE [--host HOST] [--adopt] [--write --expect HASH]
       workspace register WORKSPACE PATH --cluster ID --one-liner TEXT [--name NAME]
                          [--lifecycle LIFECYCLE] [--host HOST] [--write --expect HASH]
       workspace decline WORKSPACE PATTERN --reason TEXT [--write --expect HASH]
       workspace prompt [--snooze DAYS]
       workspace --version

  check     prove the registry, the map and .claude/settings.json agree
  scan      report repos under this host's code root that the registry
            does not account for; --edges also proposes edges between
            registered repos from their source files
  gen       render the map, the CLAUDE.md pointer, .claude/settings.json and
            this host's .code-workspace file from the registry
  register  add the repository at PATH to the registry
  decline   record a remote or path pattern the registry should not propose
  prompt    run in the workspace folder by the opt-in SessionStart hook: at
            most one line naming unregistered repos, once a day; --snooze
            DAYS keeps it quiet; always exits 0

WORKSPACE is the folder holding registry.yaml, the map (AGENTS.md, or
CLAUDE.md) and .claude/settings.json. check and scan only read. gen,
register and decline print a plan and a plan hash and write nothing; run
again with --write --expect HASH to write exactly that plan.

Exit codes: 0 conforms or done, 1 gap, drift or refusal, 2 could not verify,
3 usage.
"""

PLAN_HASH = re.compile(r"^[0-9a-f]{64}$")
LIFECYCLES = ("active", "maintenance", "deprecated", "sunset", "archived")


def usage_error(message=None):
    if message:
        sys.stderr.write("workspace: %s\n" % message)
    sys.stderr.write(USAGE)
    return EX_USAGE


def parse_options(verb, args, positionals, values, flags):
    """Split `args` into positionals and options, or raise ValueError naming the problem.

    `values` are the options that take a value (`--opt V` or `--opt=V`);
    `flags` take none. Any other argument starting with `-` is refused.
    """
    found = []
    options = {}
    index = 0
    while index < len(args):
        arg = args[index]
        name, equals, inline = arg.partition("=")
        if name in values:
            if name in options:
                raise ValueError("%s given twice" % name)
            if equals:
                value = inline
                index += 1
            else:
                if index + 1 >= len(args):
                    raise ValueError("%s needs a value" % name)
                value = args[index + 1]
                index += 2
            if not value:
                raise ValueError("%s needs a value" % name)
            options[name] = value
            continue
        if arg in flags:
            if arg in options:
                raise ValueError("%s given twice" % arg)
            options[arg] = True
            index += 1
            continue
        if arg.startswith("-"):
            raise ValueError("unknown option %s" % arg)
        if len(found) >= len(positionals):
            raise ValueError("%s takes %s" % (verb, " ".join(positionals)))
        found.append(arg)
        index += 1
    if len(found) < len(positionals):
        raise ValueError("%s needs a %s" % (verb, positionals[len(found)]))
    return found, options


def confirm_options(options):
    """The --write/--expect pair: both or neither, and a 64-hex plan hash."""
    write = options.get("--write", False)
    expect = options.get("--expect")
    if write and expect is None:
        raise ValueError("--write needs --expect")
    if expect is not None and not write:
        raise ValueError("--expect needs --write")
    if expect is not None and not PLAN_HASH.match(expect):
        raise ValueError("--expect needs the 64-hex plan hash a dry run printed")
    return write, expect


def parse_scan_args(args):
    """Return (workspace, host, edges) or raise ValueError naming the problem."""
    workspace = None
    host = None
    edges = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--host":
            if index + 1 >= len(args):
                raise ValueError("--host needs a value")
            if host is not None:
                raise ValueError("--host given twice")
            host = args[index + 1]
            index += 2
            continue
        if arg.startswith("--host="):
            if host is not None:
                raise ValueError("--host given twice")
            host = arg[len("--host="):]
            index += 1
            continue
        if arg == "--edges":
            if edges:
                raise ValueError("--edges given twice")
            edges = True
            index += 1
            continue
        if arg.startswith("-"):
            raise ValueError("unknown option %s" % arg)
        if workspace is not None:
            raise ValueError("scan takes one WORKSPACE")
        workspace = arg
        index += 1
    if workspace is None:
        raise ValueError("scan needs a WORKSPACE")
    if host is not None and not host:
        raise ValueError("--host needs a value")
    return workspace, host, edges


def parse_gen_args(args):
    (workspace,), options = parse_options(
        "gen", args, ["WORKSPACE"], {"--host", "--expect"}, {"--write", "--adopt"})
    write, expect = confirm_options(options)
    return workspace, options.get("--host"), options.get("--adopt", False), write, expect


def register_path(text):
    """PATH as written: absolute or `~`-prefixed, with no `.` or `..` component."""
    if not text.startswith(("/", "~")):
        raise ValueError("PATH must be absolute or start with ~: %s" % text)
    if any(part in (".", "..") for part in text.split("/")):
        raise ValueError("PATH must not hold a . or .. component: %s" % text)
    return text


def parse_register_args(args):
    (workspace, path), options = parse_options(
        "register", args, ["WORKSPACE", "PATH"],
        {"--cluster", "--one-liner", "--name", "--lifecycle", "--host", "--expect"},
        {"--write"})
    for required in ("--cluster", "--one-liner"):
        if required not in options:
            raise ValueError("register needs %s" % required)
    lifecycle = options.get("--lifecycle", "active")
    if lifecycle not in LIFECYCLES:
        raise ValueError("--lifecycle must be one of %s" % ", ".join(LIFECYCLES))
    write, expect = confirm_options(options)
    return {
        "workspace": workspace,
        "path": register_path(path),
        "cluster": options["--cluster"],
        "one_liner": options["--one-liner"],
        "name": options.get("--name"),
        "lifecycle": lifecycle,
        "host": options.get("--host"),
        "write": write,
        "expect": expect,
    }


def parse_decline_args(args):
    (workspace, pattern), options = parse_options(
        "decline", args, ["WORKSPACE", "PATTERN"], {"--reason", "--expect"}, {"--write"})
    if "--reason" not in options:
        raise ValueError("decline needs --reason")
    write, expect = confirm_options(options)
    return {
        "workspace": workspace,
        "pattern": pattern,
        "reason": options["--reason"],
        "write": write,
        "expect": expect,
    }


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return usage_error()
    verb, rest = args[0], args[1:]
    if verb in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if verb == "--version":
        sys.stdout.write("workspace %s\n" % __version__)
        return 0
    if verb == "check":
        if len(rest) != 1 or rest[0].startswith("-"):
            return usage_error("check takes one WORKSPACE")
        from workspace import check

        return check.main(rest[0])
    if verb == "scan":
        try:
            workspace, host, edges = parse_scan_args(rest)
        except ValueError as exc:
            return usage_error(str(exc))
        from workspace import scan

        return scan.main(workspace, host, edges=edges)
    if verb == "gen":
        try:
            workspace, host, adopt, write, expect = parse_gen_args(rest)
        except ValueError as exc:
            return usage_error(str(exc))
        from workspace import gen

        return gen.main(workspace, host=host, adopt=adopt, write=write, expect=expect)
    if verb == "register":
        try:
            request = parse_register_args(rest)
        except ValueError as exc:
            return usage_error(str(exc))
        from workspace import register

        return register.register_main(request)
    if verb == "decline":
        try:
            request = parse_decline_args(rest)
        except ValueError as exc:
            return usage_error(str(exc))
        from workspace import register

        return register.decline_main(request)
    if verb == "prompt":
        # No usage error: the hook's verb exits 0 and prints at most one line.
        from workspace import prompt

        return prompt.main(rest)
    return usage_error("unknown verb %s" % verb)
