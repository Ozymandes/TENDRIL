#!/usr/bin/env python3
"""tendril_upgrade — `tendril --upgrade`: safe self-update for a TENDRIL host.

Every `./install` records provenance (~/.config/remote-agents/install.json:
source checkout, git commit and branch, version, sha256 of the installed
phone launchers). The upgrade re-reads it, refuses to touch anything unless
the recorded checkout is a clean git tree on the same branch, in sync with
its upstream (git fetch --quiet + git merge --ff-only @{u}; never
reset/force), refreshes the installed files with the installer's
non-interactive `./install --upgrade` mode, and asserts that config and
notify.env stayed byte-identical. It closes with non-destructive health
checks (python, herdr api snapshot, ssh/mosh, watcher service, config
parse, remote PATH) and a compact summary block. Stdlib only, Python 3.9+
(the Xcode Command Line Tools python3 on macOS).

    tendril_upgrade.py [--verbose]                the upgrade (tendril --upgrade)
    tendril_upgrade.py record-provenance --pkg DIR
    tendril_upgrade.py version                    the one-line version

Flags only — there is deliberately no bare `upgrade` word: a single word is
a session-token position (workspace ids, pane ids, exact labels) and a
workspace may legitimately be labeled "upgrade"; `--upgrade` is
unambiguous. Source discovery never guesses ~/Projects/tendril: provenance
first, then the realpath of this script, but only when that lives inside a
git checkout containing ./install.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_VERSION = "0.2.0-dev"
INSTALLER = "install"
PHONE_FILES = ("tendril", "termux-url-opener", "agent")
USAGE = ("usage: tendril_upgrade.py [--verbose]\n"
         "       tendril_upgrade.py record-provenance --pkg DIR\n"
         "       tendril_upgrade.py version")


class Refused(Exception):
    """The upgrade cannot run: one clear line, exit non-zero, nothing done."""


def say(msg):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- paths
def config_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "remote-agents")


def config_path():
    return os.path.join(config_dir(), "config")


def notify_path():
    return os.path.join(config_dir(), "notify.env")


def provenance_path():
    return os.path.join(config_dir(), "install.json")


def sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def parse_env_file(path):
    """KEY=VALUE lines, first match wins, matching quotes stripped (the
    same loose shape remote-agents and the installer read)."""
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


# ---------------------------------------------------------------- processes
def run(argv, cwd=None, timeout=60, env=None):
    """(rc, stdout, stderr) with timeouts folded into rc 124."""
    try:
        p = subprocess.run(argv, cwd=cwd, timeout=timeout, env=env,
                           capture_output=True, text=True)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except OSError as exc:
        return 127, "", str(exc)


def git(args, repo, timeout=60):
    return run(["git", "-C", repo] + args, timeout=timeout)


# ---------------------------------------------------------------- provenance
def read_provenance(path=None):
    """The install.json dict, or None (missing/malformed is not fatal here)."""
    path = path or provenance_path()
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def valid_checkout(repo):
    """A TENDRIL source checkout: ./install present, git answers."""
    return bool(repo) and os.path.isfile(os.path.join(repo, INSTALLER)) \
        and git(["rev-parse", "--git-dir"], repo, 10)[0] == 0


def checkout_from_script():
    """The checkout this script runs from, via realpath (the installed
    command is a symlink into the checkout); None when not inside bin/."""
    real = os.path.realpath(os.path.abspath(__file__))
    bin_dir = os.path.dirname(real)
    if os.path.basename(bin_dir) != "bin":
        return None
    repo = os.path.dirname(bin_dir)
    return repo


def source_candidates(prov):
    out = []
    if prov and prov.get("source_dir"):
        out.append(prov["source_dir"])
    out.append(checkout_from_script())
    seen, uniq = set(), []
    for cand in out:
        if cand and cand not in seen:
            seen.add(cand)
            uniq.append(cand)
    return uniq


def discover_source(prov):
    """Provenance first, the running script's checkout second; never a
    guessed path like ~/Projects/tendril."""
    for cand in source_candidates(prov):
        if valid_checkout(cand):
            return cand
    if prov and prov.get("source_dir"):
        raise Refused(
            "recorded source checkout is gone: %s - re-run ./install once "
            "from your checkout, then retry" % prov["source_dir"])
    raise Refused(
        "no TENDRIL source checkout found (no install provenance and this "
        "script is not running from inside a checkout) - run ./install once "
        "from your checkout, then retry")


# ---------------------------------------------------------------- git facts
def git_dir(repo):
    rc, out, _ = git(["rev-parse", "--absolute-git-dir"], repo, 10)
    return out.strip() if rc == 0 else None


def sequence_in_progress(repo):
    d = git_dir(repo)
    if not d:
        return False
    return any(os.path.exists(os.path.join(d, name))
               for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                            "rebase-merge", "rebase-apply"))


def porcelain_lines(repo):
    rc, out, _ = git(["status", "--porcelain"], repo, 30)
    if rc != 0:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def current_branch(repo):
    rc, out, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], repo, 10)
    if rc != 0:
        return None
    branch = out.strip()
    return None if branch in ("", "HEAD") else branch


def upstream_name(repo):
    rc, out, _ = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                      "@{u}"], repo, 10)
    return out.strip() if rc == 0 else None


def ahead_behind(repo):
    rc, out, _ = git(["rev-list", "--left-right", "--count", "@{u}...HEAD"],
                     repo, 60)
    if rc != 0:
        raise Refused("cannot compare the checkout with its upstream "
                      "(git rev-list failed)")
    try:
        behind, ahead = (int(x) for x in out.split()[:2])
    except (ValueError, IndexError):
        raise Refused("cannot parse git rev-list output: %r" % out.strip())
    return behind, ahead


def head_sha(repo, timeout=10):
    rc, out, _ = git(["rev-parse", "HEAD"], repo, timeout)
    return out.strip() if rc == 0 else None


def check_source(repo, recorded_branch):
    """Every refusal in one pass; returns the state needed to proceed.
    Fetches (git fetch --quiet) so the ahead/behind math is fresh."""
    if git_dir(repo) is None:
        raise Refused("%s is not a git repository" % repo)
    if sequence_in_progress(repo):
        raise Refused("a merge, rebase or cherry-pick is in progress in %s - "
                      "finish it first" % repo)
    dirty = porcelain_lines(repo)
    if dirty is None:
        raise Refused("git status failed in %s" % repo)
    if dirty:
        raise Refused("checkout has %d uncommitted change(s) - commit or "
                      "stash first (git status --porcelain is not empty)"
                      % len(dirty))
    branch = current_branch(repo)
    if branch is None:
        raise Refused("HEAD is detached in %s - check out a branch first"
                      % repo)
    if recorded_branch and branch != recorded_branch:
        raise Refused("checkout is on branch '%s' but the install recorded "
                      "'%s' - switch back (git checkout %s) or re-run "
                      "./install from the checkout"
                      % (branch, recorded_branch, recorded_branch))
    if upstream_name(repo) is None:
        raise Refused("branch '%s' has no upstream configured - nothing to "
                      "fetch from (git branch --set-upstream-to=...)"
                      % branch)
    rc, _, err = git(["fetch", "--quiet"], repo, 300)
    if rc != 0:
        raise Refused("git fetch failed: %s - check the remote or network"
                      % ((err.strip().splitlines() or ["?"])[0]))
    behind, ahead = ahead_behind(repo)
    if ahead and behind:
        raise Refused("checkout diverged from upstream: %d local and %d "
                      "remote commit(s) - reconcile manually (the upgrade "
                      "never rebases, resets or forces)" % (ahead, behind))
    if ahead:
        raise Refused("%d local commit(s) not in the upstream branch - push "
                      "them first (the upgrade only fast-forwards)" % ahead)
    return {"branch": branch, "behind": behind, "ahead": ahead,
            "head": head_sha(repo)}


# ---------------------------------------------------------------- version
def version_file(repo):
    try:
        with open(os.path.join(repo, "VERSION"), encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except OSError:
        return None


def exact_tag(repo):
    rc, out, _ = git(["describe", "--tags", "--exact-match"], repo, 15)
    if rc != 0:
        return None
    return out.strip()[1:] if out.strip().startswith("v") else out.strip()


def describe_version(repo):
    """git describe when the VERSION file is absent (old checkouts)."""
    rc, out, _ = git(["describe", "--tags", "--long"], repo, 15)
    if rc != 0:
        return None
    m = re.match(r"^(.+?)-(\d+)-g([0-9a-f]+)$", out.strip())
    if not m:
        return None
    tag = m.group(1)[1:] if m.group(1).startswith("v") else m.group(1)
    return tag if m.group(2) == "0" else "%s-%s-g%s" % (tag, m.group(2),
                                                         m.group(3))


def resolve_version(repo):
    """Single source of truth: VERSION (0.2.0-dev until tagged); a tag on
    HEAD overrides it; git describe fills in for pre-VERSION checkouts."""
    tag = exact_tag(repo)
    if tag:
        return tag
    return version_file(repo) or describe_version(repo) or DEFAULT_VERSION


def version_line(repo=None):
    prov = read_provenance()
    if repo is None:
        for cand in source_candidates(prov):
            if valid_checkout(cand):
                repo = cand
                break
    if repo and os.path.isdir(repo):
        ver = resolve_version(repo)
        commit = head_sha(repo)
    elif prov:
        ver = prov.get("version") or DEFAULT_VERSION
        commit = prov.get("commit")
    else:
        ver, commit = DEFAULT_VERSION, None
    suffix = " (%s)" % commit[:12] if commit else ""
    return "tendril %s%s" % (ver, suffix)


# ---------------------------------------------------------------- record
def staged_link_info():
    """The staged TENDRIL Link APK summary (from the link snapshot dir,
    XDG_DATA_HOME honoured), or None. Recorded under "link_apk" so the
    next upgrade can notice a freshly staged APK."""
    data = os.environ.get("XDG_DATA_HOME") \
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(data, "tendril", "link", "tendril-link.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("sha256"), str) \
            or not doc.get("sha256"):
        return None
    return {"sha256": doc["sha256"],
            "versionCode": doc.get("versionCode"),
            "versionName": doc.get("versionName")}


def record_provenance(pkg_dir):
    """Called by ./install at the end of BOTH install modes. Read-only git
    facts about the checkout; writes ~/.config/remote-agents/install.json."""
    repo = os.path.dirname(os.path.abspath(pkg_dir or "."))
    if not os.path.isfile(os.path.join(repo, INSTALLER)):
        warn("record-provenance: %s does not look like a TENDRIL checkout"
             % repo)
        return 1
    doc = {
        "source_dir": repo,
        "commit": head_sha(repo),
        "branch": current_branch(repo),
        "version": resolve_version(repo),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phone": {name: sha256(os.path.join(repo, "phone", name))
                  for name in PHONE_FILES},
        "link_apk": staged_link_info(),
    }
    os.makedirs(config_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".install-json-", dir=config_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, provenance_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    say("provenance recorded: %s (version %s%s)"
        % (provenance_path(), doc["version"],
           ", commit %s" % doc["commit"][:12] if doc["commit"] else ""))
    return 0


# ---------------------------------------------------------------- health
def host_helper(repo=None):
    for base in (os.path.dirname(checkout_from_script() or ""),
                 os.path.join(repo or "", "bin")):
        cand = os.path.join(base or "", "tendril_host.py")
        if os.path.isfile(cand):
            return cand
    return None


def find_herdr():
    cands = []
    for override in (os.environ.get("HERDR_BIN"),
                     parse_env_file(config_path()).get("HERDR_BIN")):
        if override:
            cands.append(os.path.expanduser(override))
    found = shutil.which("herdr")
    if found:
        cands.append(found)
    for d in ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"):
        cands.append(os.path.join(os.path.expanduser(d), "herdr"))
    for cand in cands:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def service_state(repo=None):
    helper = host_helper(repo)
    if not helper:
        return "unknown"
    rc, out, _ = run([sys.executable, helper, "service", "status"], timeout=30)
    first = (out.strip().split(" (")[0] if out.strip() else "").strip()
    return first or "unknown"


def health_checks(repo=None):
    """Non-destructive checks; herdr/unreachable and warnings never abort."""
    checks, warnings = {}, []
    checks["python"] = sys.version_info >= (3, 9)
    herdr = find_herdr()
    if herdr is None:
        checks["herdr"] = "unreachable"
        warnings.append("herdr client not found - the console cannot reach "
                        "workspaces")
    else:
        rc, _, _ = run([herdr, "api", "snapshot"], timeout=15)
        checks["herdr"] = "compatible" if rc == 0 else "unreachable"
        if rc != 0:
            warnings.append("herdr is installed but the server did not "
                            "answer `herdr api snapshot` (rc %d)" % rc)
    if shutil.which("ssh") is None:
        warnings.append("ssh not found - phones have no way in")
    if shutil.which("mosh") is None:
        warnings.append("mosh not found (optional; SSH only)")
    checks["watcher"] = service_state(repo)
    helper = host_helper(repo)
    if helper:
        rc, _, _ = run([sys.executable, helper, "remote-path", "check"],
                       timeout=45)
        if rc not in (0,):
            warnings.append("remote PATH incomplete - SSH/Mosh sessions may "
                            "miss remote-agents (re-run ./install)")
    if not parse_env_file(config_path()) and os.path.exists(config_path()):
        warnings.append("%s does not parse as KEY=VALUE lines" % config_path())
    return checks, warnings


# ---------------------------------------------------------------- summary
def palette():
    """(lime, gray, red, reset): TENDRIL palette on a real tty only, plain
    otherwise — mirrors remote-agents (256-colour entries survive mosh)."""
    if not (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM", "") != "dumb"):
        return ("", "", "", "")
    if (os.environ.get("COLORTERM") or "").lower() in ("truecolor", "24bit"):
        return ("\033[38;2;203;253;117m", "\033[38;2;112;128;136m",
                "\033[38;2;232;92;111m", "\033[0m")
    return ("\033[38;5;191m", "\033[38;5;244m", "\033[38;5;203m", "\033[0m")


def refresh_marker(installer_output):
    """The last `refresh: <verb>` line `service refresh` printed."""
    found = re.findall(r"^\s*refresh: (\S+)", installer_output, re.M)
    return found[-1] if found else None


def watcher_row(marker, state):
    if marker == "restarted":
        return "restarted"
    if marker == "started":
        return "running"
    return "running" if state == "running" else "NOT RUNNING"


def phone_changes(recorded_phone, repo):
    if not isinstance(recorded_phone, dict):
        return []
    changed = []
    for name in PHONE_FILES:
        old = recorded_phone.get(name)
        new = sha256(os.path.join(repo, "phone", name))
        if old and new and old != new:
            changed.append(name)
    return changed


SAFE_ALIAS_RE = re.compile(r"^[A-Za-z0-9._@][A-Za-z0-9._@-]{0,252}$")


def notice_alias():
    """The host the phone-side hint dials: config SSH_TARGET -> SSH_ALIAS
    (the same resolution the setup script writes into a fresh phone's
    alias file). Returned only when it is a single shell-safe token, so
    the printed command stays safe to paste into Termux; empty otherwise."""
    cfg = parse_env_file(config_path())
    for key in ("SSH_TARGET", "SSH_ALIAS"):
        value = (cfg.get(key) or "").strip()
        if SAFE_ALIAS_RE.match(value):
            return value
    return ""


def android_notice():
    """Exactly two compact lines, aligned with the print_summary rows:
    the phone pulls everything itself over its existing SSH key auth
    (replaces the old scp block), and the second line is the ONE-command
    transition for phones still running the old launcher (whose
    `tendril --upgrade` predates the flag and would only error)."""
    alias = notice_alias() or "<alias>"
    return ("  phone       run  tendril --upgrade  in Termux\n"
            "              first time on an older phone:  ssh %s "
            "remote-agents --phone-script upgrade > $PREFIX/tmp/tu.sh "
            "&& sh $PREFIX/tmp/tu.sh" % alias)


def print_summary(ver, old_short, new_short, host, watcher, herdr, android):
    lime, gray, red, reset = palette()

    def value(text):
        if text in ("NOT RUNNING", "unreachable"):
            return red + text + reset
        return text

    say(lime + "TENDRIL // UPGRADE COMPLETE" + reset)
    for label, text in (
            ("version", "%s (%s → %s)" % (ver, old_short, new_short)),
            ("host", host), ("watcher", watcher), ("ntfy", "preserved"),
            ("herdr", herdr), ("android", android)):
        if gray:
            say("  %s%s%s%s" % (gray, "%-12s" % label, reset, value(text)))
        else:
            say("  %-12s%s" % (label, text))


# ---------------------------------------------------------------- upgrade
def byte_snapshot():
    return {config_path(): sha256(config_path()),
            notify_path(): sha256(notify_path())}


def run_installer(repo, verbose):
    argv = ["sh", os.path.join(repo, INSTALLER), "--upgrade"]
    if verbose:
        say("$ " + " ".join(argv))
    rc, out, err = run(argv, timeout=900)
    if verbose:
        if out:
            print(out, end="" if out.endswith("\n") else "\n", flush=True)
        if err:
            print(err, end="" if err.endswith("\n") else "\n", flush=True)
    return rc, out + err


def upgrade_main(verbose=False):
    lime, _, _, reset = palette()
    say(lime + "TENDRIL // UPGRADE" + reset)
    before = byte_snapshot()
    prov = read_provenance()
    repo = discover_source(prov)
    state = check_source(repo, (prov or {}).get("branch"))
    old_commit = (prov or {}).get("commit")
    head = state["head"]

    marker, host = None, "updated"
    if state["behind"] == 0 and old_commit and old_commit == head:
        # up to date AND installed == HEAD: health check only, change nothing
        host = "already current"
        say("checkout matches upstream and the installed commit - nothing "
            "to refresh")
    else:
        if state["behind"]:
            rc, out, err = git(["merge", "--ff-only", "@{u}"], repo, 600)
            if verbose and (out or err):
                say((out + err).strip())
            if rc != 0:
                warn("tendril --upgrade: fast-forward merge failed: %s"
                     % ((err.strip() or out.strip()) or rc))
                return 1
        rc, output = run_installer(repo, verbose)
        marker = refresh_marker(output)
        if rc != 0:
            warn("tendril --upgrade: ./install --upgrade failed (exit %d) - "
                 "rerun with --verbose for its output" % rc)
            return 1
        head = head_sha(repo) or head

    after = byte_snapshot()
    changed_paths = [p for p, h in before.items() if h != after[p]]
    if changed_paths:
        for p in changed_paths:
            warn("ABORT: %s changed during the upgrade - it must stay "
                 "byte-identical" % p)
        warn("backups live next to the files (*.bak.<epoch>); restore and "
             "re-run ./install by hand")
        return 1

    checks, warnings = health_checks(repo)
    ver = resolve_version(repo)
    old_short = (old_commit or "?")[:12]
    android = "client current"
    if host == "updated":
        changed = phone_changes((prov or {}).get("phone"), repo)
        staged = staged_link_info()
        prev_link = (prov or {}).get("link_apk")
        link_new = bool(staged) and (
            not isinstance(prev_link, dict)
            or prev_link.get("sha256") != staged["sha256"])
        if changed or link_new:
            android = "client refresh recommended"
            say(android_notice())
    print_summary(ver, old_short, head[:12], host,
                  watcher_row(marker, checks.get("watcher")),
                  checks["herdr"], android)
    for w in warnings:
        say("! " + w)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["record-provenance"]:
        rest = argv[1:]
        if "--pkg" in rest:
            pkg = rest[rest.index("--pkg") + 1] if rest.index("--pkg") + 1 < len(rest) else None
            if pkg:
                return record_provenance(pkg)
        warn(USAGE)
        return 2
    if argv[:1] == ["version"]:
        print(version_line())
        return 0
    verbose = "--verbose" in argv
    rest = [a for a in argv if a != "--verbose"]
    if rest:
        warn(USAGE)
        return 2
    try:
        return upgrade_main(verbose=verbose)
    except Refused as exc:
        warn("tendril --upgrade: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
