"""prompt: at most one line at session start about repos the registry does not name.

    workspace prompt
    workspace prompt --snooze DAYS

Run by the opt-in SessionStart hook, whose command is the constant
`workspace prompt`, with the workspace folder as its working directory. It
prints at most one line, which enters the model's context:

    N unregistered repos (newest: X). Say 'register X' or 'decline X'

or, when a code root cannot be listed from this session, a fixed trust
self-check line. `--snooze DAYS` (1 to 365) keeps it quiet until DAYS days
from today and prints nothing.

Invariants (the bar this verb is built to):

1. State. `prompt` reads and replaces only `state/prompt-<host>.json`, through
   `scan.snapshot_at` and `gen.write_atomic`. `<host>` is the registry's one
   `code_roots` host, the host `gen` uses. Nothing else is written, and
   `state/` is never made: a workspace without it is quiet.
2. Never fails. Exit 0, empty stderr, at most one stdout line, from two fixed
   templates: the unregistered line (its `newest` clause dropped when the name
   fails the grammar of invariant 5) and the trust line, which has no variable
   part. Any error, timeout, hostile state file or unwritable `state/` is
   silence and exit 0. Anything a callee prints is swallowed.
3. Quiet on failure. The line prints only after its state write lands; a
   failed write prints nothing.
4. Opt-in. The opt-in is the registry's top-level `session_prompt: on`; an
   absent key, or any other value, is off. Off reads no state, runs no scan
   and writes nothing. (`gen` owns the hook entry: one manifest-owned
   SessionStart entry whose command is the constant `workspace prompt`, with
   no path or registry text in it.)
5. Output. SessionStart stdout enters the model's context, so escaping is too
   weak: a repo name prints only if it matches `[A-Za-z0-9._-]{1,64}`;
   otherwise "newest" is dropped and the line names no repo.
6. Bounded. Already checked today, or snoozed: no scan, and git never runs.
   Otherwise one deadline (DEADLINE_SECONDS) bounds everything before the
   state write, then silence.
7. No new repo reads. The scan is `scan`'s own walk and identification;
   ranking uses the lstat times (st_mtime_ns) of the code root entry and its
   `.git` marker that the walk already verified, never another read of a repo.

The state file grammar: a UTF-8 JSON object of at most STATE_MAX_BYTES bytes
with exactly the keys `version` (the integer 1), `checked` and `snooze_until`,
each of the last two null or a `YYYY-MM-DD` calendar date. `checked` is the
local date of the last completed scan, whether or not a line printed;
`snooze_until` is the first date the line may show again. Anything else (a
near miss: another key, a missing key, a string or boolean version, another
date shape, a repeated key, a NaN, bytes that are not UTF-8, a list) is
refused by name in `parse_state`, and `prompt` stays silent and leaves the
file as it is.

Failure path and concurrent writers. The one irreversible step is the state
write; the print follows it. A stop before the write leaves the state as it
was, and the next session scans again. A stop between the write and the print
loses that day's line: the rerun the same day sees `checked` is today and stays
quiet. Concurrent writers of the state file are another `prompt` (two sessions
starting together) and the person: `gen.write_atomic` creates its temp with
O_EXCL and refuses the rename unless the file is still the one read, so the
second writer stays silent. The line can still show twice when two runs both
pass the recheck inside the rename window `gen` already accepts (a known gap).
A temp left by a killed run makes `prompt` silent until `gen` removes it.
`gen` itself never writes this file.
"""
import contextlib
import datetime
import io
import json
import os
import re
import signal
import sys

DEADLINE_SECONDS = 4.0
OPT_IN_KEY = "session_prompt"
STATE_DIR = "state"
STATE_VERSION = 1
STATE_KEYS = frozenset(("version", "checked", "snooze_until"))
STATE_MAX_BYTES = 4096
SNOOZE_MAX_DAYS = 365

NAME = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
DAYS = re.compile(r"[1-9][0-9]{0,2}", re.ASCII)

# The two fixed templates. The first takes a count and, when the name passes
# NAME, the name; the trust line takes nothing.
LINE_NEWEST = "%d unregistered %s (newest: %s). Say 'register %s' or 'decline %s'"
LINE_NO_NAME = ("%d unregistered %s. Run workspace scan to name them, then say "
                "'register NAME' or 'decline NAME'")
TRUST_LINE = ("The workspace cannot list its code root from this session; give this "
              "app read access to it, then run workspace scan")

# Why a state file is refused (parse_state).
STATE_TOO_BIG = "too-big"
STATE_NOT_UTF8 = "not-utf8"
STATE_NOT_JSON = "not-json"
STATE_REPEATED_KEY = "repeated-key"
STATE_NOT_OBJECT = "not-object"
STATE_KEYS_DIFFER = "keys-differ"
STATE_BAD_VERSION = "bad-version"
STATE_BAD_DATE = "bad-date"


class Quiet(Exception):
    """Stop silently; the reason is for tests and readers, never printed."""


class Deadline(Exception):
    """The deadline passed before the state write."""


class BadState(Quiet):
    """The state file is outside its grammar; `reason` names the rule it broke."""

    def __init__(self, reason):
        Quiet.__init__(self, reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Grammar: names, the state file, the arguments
# --------------------------------------------------------------------------

def render(count, name):
    """The unregistered line for `count` repos whose newest is named `name`."""
    noun = "repo" if count == 1 else "repos"
    if isinstance(name, str) and NAME.fullmatch(name):
        return LINE_NEWEST % (count, noun, name, name, name)
    return LINE_NO_NAME % (count, noun)


def empty_state():
    return {"version": STATE_VERSION, "checked": None, "snooze_until": None}


def unique_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise BadState(STATE_REPEATED_KEY)
        seen[key] = value
    return seen


def refuse_constant(_text):
    raise BadState(STATE_NOT_JSON)


def parse_date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise BadState(STATE_BAD_DATE)
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise BadState(STATE_BAD_DATE)


def parse_state(data):
    """The state dict from the state file's bytes, or BadState naming the rule broken."""
    if len(data) > STATE_MAX_BYTES:
        raise BadState(STATE_TOO_BIG)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BadState(STATE_NOT_UTF8)
    try:
        state = json.loads(text, object_pairs_hook=unique_object,
                           parse_constant=refuse_constant)
    except ValueError:
        raise BadState(STATE_NOT_JSON)
    if not isinstance(state, dict):
        raise BadState(STATE_NOT_OBJECT)
    if set(state) != STATE_KEYS:
        raise BadState(STATE_KEYS_DIFFER)
    version = state["version"]
    if type(version) is not int or version != STATE_VERSION:
        raise BadState(STATE_BAD_VERSION)
    parse_date(state["checked"])
    parse_date(state["snooze_until"])
    return state


def state_bytes(state):
    return (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")


def parse_args(args):
    """None for the hook; the snooze days for `--snooze DAYS`; Quiet for anything else."""
    if not args:
        return None
    if len(args) == 2 and args[0] == "--snooze":
        text = args[1]
    elif len(args) == 1 and args[0].startswith("--snooze="):
        text = args[0][len("--snooze="):]
    else:
        raise Quiet("unknown arguments")
    if not DAYS.fullmatch(text) or int(text) > SNOOZE_MAX_DAYS:
        raise Quiet("--snooze takes 1 to %d days" % SNOOZE_MAX_DAYS)
    return int(text)


def opted_in(document):
    """`session_prompt: on` at the top of the registry. YAML reads a bare `on` as true."""
    if not isinstance(document, dict):
        return False
    value = document.get(OPT_IN_KEY)
    return value is True or value == "on"


def quiet_today(state, today):
    if state["checked"] == today.isoformat():
        return True
    snooze = parse_date(state["snooze_until"])
    return snooze is not None and today < snooze


# --------------------------------------------------------------------------
# The deadline
# --------------------------------------------------------------------------

@contextlib.contextmanager
def deadline(seconds):
    """Raise Deadline in the body once `seconds` pass. A git child is killed by
    `subprocess.run` as the exception passes through it."""
    def expire(_signum, _frame):
        raise Deadline()

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# --------------------------------------------------------------------------
# The scan: scan's own walk, ranked by the lstat times it verified
# --------------------------------------------------------------------------

def find_line(scan, code_roots, excluded, declined, projects, host):
    """The line to show, or None when every repo is registered or declined."""
    root, exclusions, nodes, registered, _ = scan.prelude(
        code_roots, excluded, declined, projects, host)
    try:
        scope = scan.root_scope(root, exclusions, nodes, registered)
        # The trust self-check: the code root that gather lists next.
        os.listdir(root)
    except PermissionError:
        return TRUST_LINE
    except scan.Unverifiable as exc:
        if isinstance(exc.__context__, PermissionError):
            return TRUST_LINE
        raise
    gathered = scan.gather(scope, registered, [])

    found = {}
    for entry, hops, kind, marker_info in gathered.candidates:
        parent, remote = scan.identify(scope, hops, kind, marker_info, gathered.known)
        record = found.setdefault(parent, {"remote": None, "seen": 0})
        record["seen"] = max(record["seen"], hops[-1][1].st_mtime_ns, marker_info.st_mtime_ns)
        if remote and record["remote"] is None:
            record["remote"] = scan.normalize_remote(remote)

    # The same judgement as scan.scan's DRIFT lines for unregistered repos.
    remotes = {project["remote"] for project in projects if project["remote"]}
    unregistered = []
    for parent, record in found.items():
        remote = record["remote"]
        if parent in registered or (remote is not None and remote in remotes):
            continue
        if scan.declined_match(declined, remote, parent):
            continue
        unregistered.append((record["seen"], parent))
    if not unregistered:
        return None
    _, newest = max(unregistered)
    return render(len(unregistered), os.path.basename(newest))


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def run(args):
    """The line to print once its state write has landed, or None. Raises to stay quiet."""
    snooze_days = parse_args(args)
    from workspace import gen, scan

    today = datetime.date.today()
    folder = None
    try:
        with deadline(DEADLINE_SECONDS):
            document = scan.parse_registry(scan.registry_snapshot(os.curdir).data)
            if not opted_in(document):
                return None
            grammar = {key: value for key, value in document.items() if key != OPT_IN_KEY}
            code_roots, excluded, declined, projects = scan.validate(grammar)
            host = scan.select_host(code_roots, None)
            if not isinstance(host, str) or not HOST.fullmatch(host):
                raise Quiet("host is not a plain file name")
            name = "prompt-%s.json" % host
            rel = os.path.join(STATE_DIR, name)
            folder = gen.open_folder(os.curdir, STATE_DIR)
            snapshot = scan.snapshot_at(folder, name, rel)
            state = empty_state() if snapshot is None else parse_state(snapshot.data)
            if snooze_days is not None:
                line = None
                state["snooze_until"] = (today + datetime.timedelta(days=snooze_days)).isoformat()
            elif quiet_today(state, today):
                return None
            else:
                line = find_line(scan, code_roots, excluded, declined, projects, host)
                state["checked"] = today.isoformat()
        gen.write_atomic(folder, name, state_bytes(state), gen.expected_of(snapshot), rel)
        return line
    finally:
        if folder is not None:
            os.close(folder)


def silence_stdout(out):
    """After a failed write to stdout, point its descriptor at /dev/null so the
    interpreter's exit flush prints nothing to stderr."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, out.fileno())
        os.close(null)
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        pass


def main(args=(), out=None):
    """The `prompt` verb: always exit 0."""
    out = sys.stdout if out is None else out
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            line = run(list(args))
        if line is not None:
            try:
                out.write(line + "\n")
                out.flush()
            except (OSError, ValueError):
                silence_stdout(out)
    except BaseException:  # fail closed: silence, exit 0
        return 0
    return 0
