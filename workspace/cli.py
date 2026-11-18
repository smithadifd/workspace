"""The `workspace` command line: the only interface to this package.

    workspace check WORKSPACE
    workspace scan WORKSPACE [--host HOST]

WORKSPACE is the workspace folder that holds `registry.yaml`, the map and
`.claude/settings.json`. Both verbs only read.

Exit codes: 0 conforms, 1 gap or drift, 2 could not verify, 3 usage.
"""
import sys

from workspace import __version__

EX_USAGE = 3

USAGE = """\
Usage: workspace check WORKSPACE
       workspace scan WORKSPACE [--host HOST]
       workspace --version

  check  prove the registry, the map and .claude/settings.json agree
  scan   report repos under this host's code root that the registry
         does not account for

WORKSPACE is the folder holding registry.yaml, the map (AGENTS.md, or
CLAUDE.md) and .claude/settings.json. Both verbs only read.

Exit codes: 0 conforms, 1 gap or drift, 2 could not verify, 3 usage.
"""


def usage_error(message=None):
    if message:
        sys.stderr.write("workspace: %s\n" % message)
    sys.stderr.write(USAGE)
    return EX_USAGE


def parse_scan_args(args):
    """Return (workspace, host) or raise ValueError naming the problem."""
    workspace = None
    host = None
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
    return workspace, host


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
            workspace, host = parse_scan_args(rest)
        except ValueError as exc:
            return usage_error(str(exc))
        from workspace import scan

        return scan.main(workspace, host)
    return usage_error("unknown verb %s" % verb)
