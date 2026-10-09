"""`tendril --upgrade`: provenance, safety refusals, non-interactive refresh.

Hermetic by construction: every upgrade runs against a throwaway git
checkout with a bare upstream on local disk (no network), a fake HOME with
a space in its name, and recording stubs for systemctl / launchctl / herdr
on PATH. The real home, the real checkout and the user's real installation
are never touched (the installer/provenance paths only ever read the real
repo, and git calls on it are read-only).

Covers: clean fast-forward upgrade, already-current (health check only,
nothing changed), byte-identical config/notify.env with custom ntfy values,
every safety refusal (dirty tree, merge in progress, detached HEAD, local
commits, diverged, branch switch, no upstream, fetch failure, missing
provenance), service refresh semantics per backend (no reload/restart when
the unit is current, reload+restart once when it changed, start when it was
not running), launchd and systemd paths, PATH-block/plist/binding
idempotence across repeated upgrades, first-install behaviour staying
intact, --version/--help output, and the deliberate absence of a bare
`upgrade` word.
"""
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:  # importable however the suite is invoked
    sys.path.insert(0, TESTS)
from test_darwin_install import INSTALL, FakeDarwin  # noqa: E402
from test_host_platform import UID, recorder  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_host as th  # noqa: E402
import tendril_upgrade as tu  # noqa: E402

MODULE = os.path.join(REPO, "bin", "tendril_upgrade.py")
REMOTE_AGENTS = os.path.join(REPO, "bin", "remote-agents")
UNIT_SRC = os.path.join(REPO, "systemd", th.UNIT)

BIN_FILES = ("remote-agents", "herdr-notify", "tendril_link.py",
             "tendril_host.py", "herdr-bindings.py", "tendril_upgrade.py",
             "tendril_phone.py")
PHONE_FILES = ("tendril", "termux-url-opener", "agent")

CUSTOM_NOTIFY = (b"NTFY_URL=https://ntfy.example\n"
                 b"NTFY_TOPIC=tendril-custom-topic-keep\n"
                 b"NTFY_TOKEN=tok_preserve_me\n")
CUSTOM_CONFIG = (b"TAILSCALE_HOST=tendril-host\n"
                 b"SSH_ALIAS=tendril-test\n"
                 b"PROJECT_ROOTS=/home/x/Projects\n"
                 b"HERDR_BIN=\n")

SYSTEMCTL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TENDRIL_SYSTEMCTL_LOG"
case "$1 $2" in
    "--user is-active") exit "${TENDRIL_SVC_ACTIVE:-0}" ;;
    "--user is-system-running") echo running ;;
esac
exit 0
"""

LAUNCHCTL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TENDRIL_LAUNCHCTL_LOG"
if [ "$1" = "print" ]; then
    if [ -n "${TENDRIL_LAUNCHCTL_PRINT_RC:-}" ]; then
        exit "$TENDRIL_LAUNCHCTL_PRINT_RC"
    fi
    printf 'pid = 123\\nstate = running\\n'
    exit 0
fi
exit 0
"""

HERDR_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TENDRIL_HERDR_LOG"
case "$1" in
    --version) echo "herdr 0.9.3" ;;
    status)
        printf 'status: running\\n'
        printf 'private_protocol_compatible: yes\\n'
        printf 'version: 0.9.3\\nprivate_protocol: 1\\n' ;;
    *) exit 0 ;;
esac
"""


def sh(args, cwd=None, env=None, timeout=240):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def git_env():
    e = dict(os.environ)
    e.update({
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example",
        "LC_ALL": "C", "LANG": "C",
    })
    return e


def git_ok(args, cwd=None):
    p = sh(["git"] + args, cwd=cwd, env=git_env())
    if p.returncode != 0:
        raise AssertionError("git %s failed: %s%s" % (" ".join(args),
                                                      p.stdout, p.stderr))
    return p.stdout.strip()


def seed_tree(work):
    """A minimal but REAL tendril tree: the actual scripts, installer and
    assets from this checkout."""
    for sub, names in (("bin", BIN_FILES),
                       ("phone", PHONE_FILES),
                       ("systemd", (th.UNIT,))):
        os.makedirs(os.path.join(work, sub), exist_ok=True)
        for name in names:
            shutil.copy2(os.path.join(REPO, sub, name),
                         os.path.join(work, sub, name))
    shutil.copy2(os.path.join(REPO, "install"), os.path.join(work, "install"))
    shutil.copy2(os.path.join(REPO, "VERSION"), os.path.join(work, "VERSION"))


def make_checkout(tmp):
    """bare upstream + a work clone at one pushed commit; returns (work, bare)."""
    bare = os.path.join(tmp, "upstream.git")
    work = os.path.join(tmp, "work")
    git_ok(["init", "--quiet", "--bare", "-b", "main", bare], cwd=tmp)
    git_ok(["clone", "--quiet", bare, work], cwd=tmp)
    seed_tree(work)
    git_ok(["add", "-A"], cwd=work)
    git_ok(["commit", "--quiet", "-m", "seed"], cwd=work)
    git_ok(["push", "--quiet", "-u", "origin", "main"], cwd=work)
    return work, bare


def advance_upstream(tmp, bare, files):
    """Push a new commit from a second clone so the work clone is behind."""
    dev2 = os.path.join(tmp, "dev2")
    if not os.path.isdir(dev2):
        git_ok(["clone", "--quiet", bare, dev2], cwd=tmp)
    for rel, text in files.items():
        with open(os.path.join(dev2, rel), "a") as f:
            f.write(text)
    git_ok(["add", "-A"], cwd=dev2)
    git_ok(["commit", "--quiet", "-m", "advance"], cwd=dev2)
    git_ok(["push", "--quiet", "origin", "main"], cwd=dev2)
    return git_ok(["rev-parse", "HEAD"], cwd=dev2)


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def read_file(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


class UpgradeEnv(unittest.TestCase):
    """Fake HOME + throwaway git checkout + recording stubs on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril upgrade ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home dir")
        os.makedirs(self.home)
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.bindir)
        self.work, self.bare = make_checkout(self.tmp)
        self.systemctl_log = os.path.join(self.tmp, "systemctl.log")
        self.launchctl_log = os.path.join(self.tmp, "launchctl.log")
        self.herdr_log = os.path.join(self.tmp, "herdr.log")
        self.write_seed_config()

    # -- stubs ------------------------------------------------------------
    def stub(self, name, body):
        path = os.path.join(self.bindir, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def stub_linux_service(self):
        """A live systemd user manager (recording)."""
        self.stub("systemctl", SYSTEMCTL_STUB)

    def stub_launchd_service(self):
        """A loaded, running LaunchAgent (recording)."""
        self.stub("launchctl", LAUNCHCTL_STUB)

    def stub_herdr(self):
        self.stub("herdr", HERDR_STUB)

    # -- fake HOME state --------------------------------------------------
    def write_seed_config(self):
        conf = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "config"), "wb") as f:
            f.write(CUSTOM_CONFIG)
        with open(os.path.join(conf, "notify.env"), "wb") as f:
            f.write(CUSTOM_NOTIFY)
        os.chmod(os.path.join(conf, "notify.env"), 0o600)

    def seed_config_toml(self):
        """A herdr config with a user-owned binding the upgrade must keep."""
        conf = os.path.join(self.home, ".config", "herdr")
        os.makedirs(conf, exist_ok=True)
        path = os.path.join(conf, "config.toml")
        with open(path, "w") as f:
            f.write('# my own binding, must survive every upgrade\n'
                    '[keys]\ndetach = ["alt+x"]\n')
        return path

    def record_provenance(self):
        p = sh([sys.executable, MODULE, "record-provenance", "--pkg",
                os.path.join(self.work, "bin")], env=self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    # -- environment ------------------------------------------------------
    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join(
            [self.bindir, "/usr/bin", "/bin", e.get("PATH", "")])
        e["HOME"] = self.home
        e["SHELL"] = "/bin/sh"
        e["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "xdg-run")
        os.makedirs(e["XDG_RUNTIME_DIR"], exist_ok=True)
        e.update({k: "/dev/null" for k in ("GIT_CONFIG_GLOBAL",
                                           "GIT_CONFIG_SYSTEM")})
        e["GIT_TERMINAL_PROMPT"] = "0"
        e["LC_ALL"] = "C"
        e["TENDRIL_SYSTEMCTL_LOG"] = self.systemctl_log
        e["TENDRIL_LAUNCHCTL_LOG"] = self.launchctl_log
        e["TENDRIL_HERDR_LOG"] = self.herdr_log
        for k in ("XDG_CONFIG_HOME", "TENDRIL_ALIAS", "AGENT_ALIAS",
                  "HERDR_BIN", "NO_COLOR", "TENDRIL_SVC_ACTIVE",
                  "TENDRIL_LAUNCHCTL_PRINT_RC"):
            e.pop(k, None)
        e.update(over)
        return e

    def run_module(self, args=(), **env_over):
        return sh([sys.executable, MODULE] + list(args), cwd=self.tmp,
                  env=self.env(**env_over))

    def run_installer_directly(self):
        return sh(["sh", os.path.join(self.work, "install"), "--upgrade"],
                  env=self.env())

    # -- assertions -------------------------------------------------------
    def installed(self, name):
        return os.path.join(self.home, ".local", "bin", name)

    def install_json(self):
        return json.loads(read_file(os.path.join(
            self.home, ".config", "remote-agents", "install.json")))

    def log_lines(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def launchctl(self):
        return self.log_lines(self.launchctl_log)

    def syscalls(self):
        """systemctl subcommand lines (the log drops the `systemctl` argv0
        and we strip the constant `--user`)."""
        out = []
        for ln in self.log_lines(self.systemctl_log):
            out.append(ln[len("--user "):] if ln.startswith("--user ") else ln)
        return out

    def verb_counts(self, lines):
        counts = {}
        for ln in lines:
            first = ln.split()[:1]
            if first:
                counts[first[0]] = counts.get(first[0], 0) + 1
        return counts

    def assert_block(self, out, host, watcher="running",
                     android="client current"):
        self.assertIn("TENDRIL // UPGRADE COMPLETE", out)
        self.assertIn("  host        %s" % host, out)
        self.assertIn("  watcher     %s" % watcher, out)
        self.assertIn("  ntfy        preserved", out)
        self.assertIn("  herdr       compatible", out)
        self.assertIn("  android     %s" % android, out)


class VersionResolution(unittest.TestCase):
    """VERSION is the single source of truth; a tag on HEAD overrides."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril version ")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_version_file_until_tagged(self):
        work, _ = make_checkout(self.tmp)          # VERSION file, no tags
        self.assertEqual(tu.resolve_version(work), "0.2.0-dev")
        self.assertTrue(tu.version_line(work).startswith("tendril 0.2.0-dev"))

    def test_exact_tag_overrides(self):
        work, _ = make_checkout(self.tmp)
        git_ok(["tag", "v9.9.9"], cwd=work)
        self.assertEqual(tu.resolve_version(work), "9.9.9")

    def test_describe_fills_in_without_version_file(self):
        work, _ = make_checkout(self.tmp)
        os.remove(os.path.join(work, "VERSION"))
        git_ok(["add", "-A"], cwd=work)
        git_ok(["commit", "--quiet", "-m", "drop VERSION"], cwd=work)
        git_ok(["tag", "v1.2.3"], cwd=work)
        git_ok(["commit", "--quiet", "--allow-empty", "-m", "after"], cwd=work)
        short = git_ok(["rev-parse", "--short", "HEAD"], cwd=work)
        self.assertEqual(tu.resolve_version(work), "1.2.3-1-g%s" % short)


class ServiceRefreshSystemd(unittest.TestCase):
    """tendril_host `service refresh` against a recording systemctl."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril svc home ")
        self.addCleanup(shutil.rmtree, self.home, True)
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(os.environ.__setitem__, "HOME", old)

    def make(self, reply=None, active=True):
        if reply is None and not active:
            def reply(args):
                # only is-active reports the stopped watcher; actions succeed
                return (1, "", "") if len(args) > 2 and args[2] == "is-active" \
                    else None
        run = recorder(reply, (0, "", ""))
        out = io.StringIO()
        return th.Systemd(run=run, out=out), run, out

    def seed_unit(self, b):
        os.makedirs(os.path.dirname(b.unit), exist_ok=True)
        shutil.copy2(UNIT_SRC, b.unit)

    def verbs(self, run):
        return [c[2] for c in run.calls if len(c) > 2]

    def test_unit_current_and_running_leaves_everything_alone(self):
        b, run, out = self.make()
        self.seed_unit(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        self.assertNotIn("daemon-reload", verbs)
        self.assertNotIn("restart", verbs)
        self.assertNotIn("start", verbs)
        self.assertIn("refresh: unchanged", out.getvalue())

    def test_unit_changed_reloads_once_and_restarts_once(self):
        b, run, out = self.make()
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        self.assertEqual(verbs.count("daemon-reload"), 1)
        self.assertEqual(verbs.count("restart"), 1)
        self.assertIn("refresh: restarted", out.getvalue())
        self.assertEqual(read_file(b.unit, "rb"), read_file(UNIT_SRC, "rb"))
        self.assertEqual(stat.S_IMODE(os.stat(b.unit).st_mode), 0o600)

    def test_not_running_gets_started_not_restarted(self):
        b, run, out = self.make(active=False)
        self.seed_unit(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        self.assertEqual(verbs.count("start"), 1)
        self.assertNotIn("restart", verbs)
        self.assertIn("refresh: started", out.getvalue())

    def test_bin_change_restarts_even_with_current_unit(self):
        b, run, out = self.make()
        self.seed_unit(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3", bin_changed=True), 0)
        verbs = self.verbs(run)
        self.assertNotIn("daemon-reload", verbs)
        self.assertEqual(verbs.count("restart"), 1)
        self.assertIn("refresh: restarted", out.getvalue())

    def test_dry_run_writes_nothing(self):
        b, _, out = self.make()
        b.dry_run = True
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        self.assertFalse(os.path.exists(b.unit))
        self.assertIn("refresh: (dry-run)", out.getvalue())

    def test_missing_source_unit_fails_cleanly(self):
        b, _, out = self.make()
        self.assertEqual(b.refresh(os.path.join(REPO, "nowhere"),
                                   "/usr/bin/python3"), 1)
        self.assertIn("cannot read", out.getvalue())


class ServiceRefreshLaunchd(unittest.TestCase):
    """tendril_host `service refresh` against a recording launchctl."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril svc home ")
        self.addCleanup(shutil.rmtree, self.home, True)
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(os.environ.__setitem__, "HOME", old)

    def make(self, reply=None):
        run = recorder(reply, (0, "", ""))
        out = io.StringIO()
        return th.Launchd(run=run, out=out, uid=UID), run, out

    @staticmethod
    def running(args):
        if len(args) > 2 and args[1] == "print":
            return (0, "pid = 5\nstate = running\n", "")
        return None

    def seed_plist(self, b):
        os.makedirs(os.path.dirname(b.plist), exist_ok=True)
        with open(b.plist, "wb") as f:
            f.write(b.render("/usr/bin/python3",
                             os.path.join(self.home, ".local", "bin",
                                          "herdr-notify")))

    def verbs(self, run):
        return [c[1] for c in run.calls if len(c) > 1]

    def test_plist_current_and_running_leaves_everything_alone(self):
        b, run, out = self.make(reply=self.running)
        self.seed_plist(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        for never in ("bootout", "bootstrap", "kickstart"):
            self.assertNotIn(never, verbs)
        self.assertIn("refresh: unchanged", out.getvalue())

    def test_plist_changed_bootstraps_exactly_once(self):
        b, run, out = self.make(reply=self.running)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        self.assertEqual(verbs.count("bootout"), 1)
        self.assertEqual(verbs.count("bootstrap"), 1)
        self.assertIn("refresh: restarted", out.getvalue())
        doc = plistlib.loads(read_file(b.plist, "rb"))
        self.assertEqual(doc["Label"], th.LABEL)

    def test_not_loaded_gets_started(self):
        def not_loaded(args):
            if len(args) > 2 and args[1] == "print":
                return (1, "", "not loaded")
            return None

        b, run, out = self.make(reply=not_loaded)
        self.seed_plist(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3"), 0)
        verbs = self.verbs(run)
        self.assertEqual(verbs.count("bootstrap"), 1)
        self.assertNotIn("bootout", verbs)
        self.assertIn("refresh: started", out.getvalue())

    def test_bin_change_restarts_even_with_current_plist(self):
        b, run, out = self.make(reply=self.running)
        self.seed_plist(b)
        self.assertEqual(b.refresh(REPO, "/usr/bin/python3", bin_changed=True), 0)
        verbs = self.verbs(run)
        self.assertEqual(verbs.count("bootstrap"), 1)
        self.assertIn("refresh: restarted", out.getvalue())


class FullUpgradeLaunchd(UpgradeEnv):
    """The whole `tendril --upgrade` pipeline, launchd backend (the stub
    shadows any real launchctl, so this is deterministic everywhere)."""

    def setUp(self):
        super().setUp()
        self.stub_linux_service()
        self.stub_launchd_service()
        self.stub_herdr()
        self.record_provenance()

    def test_clean_upgrade_fast_forwards_and_reprovenances(self):
        new_head = advance_upstream(self.tmp, self.bare, {
            "bin/remote-agents": "# upgraded-dispatch-marker\n",
            "bin/herdr-notify": "# upgraded-watcher-marker\n",
            "phone/tendril": "# upgraded-phone-marker\n",
        })
        p = self.run_module()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        # checkout fast-forwarded, nothing left dirty
        self.assertEqual(git_ok(["rev-parse", "HEAD"], cwd=self.work), new_head)
        self.assertEqual(tu.porcelain_lines(self.work), [])
        # binaries refreshed from the new tree
        self.assertIn("upgraded-dispatch-marker",
                      read_file(self.installed("remote-agents")))
        self.assertIn("upgraded-watcher-marker",
                      read_file(self.installed("herdr-notify")))
        # provenance updated to the new commit + fresh phone hashes
        doc = self.install_json()
        self.assertEqual(doc["commit"], new_head)
        self.assertEqual(doc["phone"]["tendril"],
                         sha(os.path.join(self.work, "phone", "tendril")))
        self.assertEqual(doc["version"], "0.2.0-dev")
        # watcher: herdr-notify changed -> exactly one restart cycle
        counts = self.verb_counts(self.launchctl())
        self.assertEqual(counts.get("bootout"), 1)
        self.assertEqual(counts.get("bootstrap"), 1)
        self.assertIn("  watcher     restarted", out)
        # android client refresh recommended: the phone pulls everything
        # itself now — one command, never scp; the second notice line is
        # the transition hint for phones still running the old launcher
        # (alias resolved from CUSTOM_CONFIG: SSH_ALIAS=tendril-test)
        self.assertIn("  phone       run  tendril --upgrade  in Termux", out)
        self.assertIn(
            "              first time on an older phone:  "
            "ssh tendril-test remote-agents --phone-script upgrade "
            "> $PREFIX/tmp/tu.sh && sh $PREFIX/tmp/tu.sh", out)
        self.assertNotIn("scp", out)
        # config + notify.env byte-identical, PATH block exactly once
        conf = os.path.join(self.home, ".config", "remote-agents")
        self.assertEqual(read_file(os.path.join(conf, "notify.env"), "rb"),
                         CUSTOM_NOTIFY)
        self.assertEqual(read_file(os.path.join(conf, "config"), "rb"),
                         CUSTOM_CONFIG)
        profile = read_file(os.path.join(self.home, ".profile"))
        self.assertEqual(profile.count("# >>> tendril remote PATH"), 1)
        self.assert_block(out, host="updated", watcher="restarted",
                          android="client refresh recommended")
        self.assertIn("  version     0.2.0-dev", out)

    def test_already_current_runs_health_check_and_changes_nothing(self):
        first = self.run_installer_directly()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        conf = os.path.join(self.home, ".config", "remote-agents")
        bindir = os.path.dirname(self.installed("x"))
        before_json = read_file(os.path.join(conf, "install.json"), "rb")
        before_bins = {n: sha(self.installed(n)) for n in BIN_FILES}
        before_lc = self.launchctl()
        before_dir = sorted(os.listdir(bindir))
        p = self.run_module()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("nothing to refresh", p.stdout)
        self.assert_block(p.stdout, host="already current")
        # strictly nothing changed: no launchctl ACTIONS, no bin churn,
        # no provenance rewrite, no new backup files (status probes are
        # read-only and allowed)
        actions = [ln for ln in self.launchctl()[len(before_lc):]
                   if ln.split()[:1] and ln.split()[0]
                   in ("bootout", "bootstrap", "kickstart", "enable")]
        self.assertEqual(actions, [])
        for n, h in before_bins.items():
            self.assertEqual(sha(self.installed(n)), h, n)
        self.assertEqual(read_file(os.path.join(conf, "install.json"), "rb"),
                         before_json)
        self.assertEqual(sorted(os.listdir(bindir)), before_dir)

    def test_verbose_prints_the_installer_output(self):
        advance_upstream(self.tmp, self.bare,
                         {"bin/remote-agents": "# verbose-marker\n"})
        p = self.run_module(["--verbose"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("$ sh", p.stdout)
        self.assertIn("TENDRIL // UPGRADE (non-interactive refresh)", p.stdout)

    def test_manual_pull_still_refreshes_the_host(self):
        # behind == 0 but the installed commit is older: refresh, no merge
        new_head = advance_upstream(self.tmp, self.bare,
                                    {"bin/remote-agents": "# manual\n"})
        git_ok(["fetch", "--quiet"], cwd=self.work)
        git_ok(["merge", "--ff-only", "@{u}"], cwd=self.work)
        p = self.run_module()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.install_json()["commit"], new_head)
        self.assert_block(p.stdout, host="updated", watcher="restarted")

    def test_newly_staged_link_apk_triggers_the_phone_line(self):
        # an APK staged after the last install: the summary recommends the
        # phone refresh even when no phone asset changed, and provenance
        # records the staged APK under "link_apk"
        data = os.path.join(self.tmp, "data")
        link = os.path.join(data, "tendril", "link")
        os.makedirs(link)
        apk = b"staged-link-apk"
        with open(os.path.join(link, "tendril-link.apk"), "wb") as f:
            f.write(apk)
        with open(os.path.join(link, "tendril-link.json"), "w") as f:
            json.dump({"schema": 1, "package": "app.tendril.link",
                       "versionCode": 7, "versionName": "0.2.0",
                       "build": "abc1234",
                       "sha256": sha(os.path.join(link,
                                                  "tendril-link.apk")),
                       "size": len(apk), "signer_sha256": "d" * 64}, f)
        advance_upstream(self.tmp, self.bare,
                         {"bin/remote-agents": "# not-a-phone-file\n"})
        p = self.run_module(XDG_DATA_HOME=data)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("run  tendril --upgrade  in Termux", out)
        self.assertNotIn("scp", out)
        self.assert_block(out, host="updated", watcher="restarted",
                          android="client refresh recommended")
        doc = self.install_json()
        self.assertEqual(doc["link_apk"]["sha256"],
                         sha(os.path.join(link, "tendril-link.apk")))
        self.assertEqual(doc["link_apk"]["versionCode"], 7)

    def test_unchanged_link_apk_stays_quiet(self):
        # the staged APK was already recorded: no phone line when the phone
        # assets did not change either
        first = self.run_installer_directly()      # unit + PATH block in place
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        data = os.path.join(self.tmp, "data")
        link = os.path.join(data, "tendril", "link")
        os.makedirs(link)
        apk = b"staged-link-apk"
        sha_hex = hashlib.sha256(apk).hexdigest()
        with open(os.path.join(link, "tendril-link.apk"), "wb") as f:
            f.write(apk)
        with open(os.path.join(link, "tendril-link.json"), "w") as f:
            json.dump({"schema": 1, "package": "app.tendril.link",
                       "versionCode": 7, "versionName": "0.2.0",
                       "build": "abc1234", "sha256": sha_hex,
                       "size": len(apk), "signer_sha256": "d" * 64}, f)
        p = sh([sys.executable, MODULE, "record-provenance", "--pkg",
                os.path.join(self.work, "bin")],
               env={**self.env(), "XDG_DATA_HOME": data})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_module(XDG_DATA_HOME=data)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.install_json()["link_apk"]["sha256"], sha_hex)
        self.assert_block(p.stdout, host="already current")
        self.assertNotIn("run  tendril --upgrade  in Termux", p.stdout)


class FullUpgradeSystemd(UpgradeEnv):
    """Same pipeline with a systemd user manager instead of launchd.
    Skipped where a real launchctl exists (it would win backend selection
    and we deliberately do not shadow it with a stub here)."""

    def setUp(self):
        if shutil.which("launchctl"):
            self.skipTest("real launchctl present; launchd would be selected")
        super().setUp()
        self.stub_linux_service()
        self.stub_herdr()
        self.record_provenance()

    def test_clean_upgrade_reloads_and_restarts_once(self):
        advance_upstream(self.tmp, self.bare,
                         {"bin/herdr-notify": "# new watcher\n"})
        p = self.run_module()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        verbs = self.verb_counts(self.syscalls())
        self.assertEqual(verbs.get("daemon-reload"), 1)
        self.assertEqual(verbs.get("restart"), 1)
        self.assertIn("  watcher     restarted", p.stdout)
        unit = os.path.join(self.home, ".config", "systemd", "user", th.UNIT)
        self.assertEqual(read_file(unit, "rb"), read_file(UNIT_SRC, "rb"))

    def test_second_run_with_current_unit_does_nothing(self):
        first = self.run_installer_directly()   # establish installed state
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(os.path.isfile(
            os.path.join(self.home, ".config", "systemd", "user", th.UNIT)))
        before = self.action_lines(self.syscalls())
        p2 = self.run_module()
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertEqual(self.action_lines(self.syscalls())[len(before):], [])

    @staticmethod
    def action_lines(lines):
        """Mutating systemctl verbs only (status probes are read-only)."""
        return [ln for ln in lines
                if ln.split()[:1] and ln.split()[0]
                in ("daemon-reload", "restart", "start", "stop", "enable")]


class AndroidNotice(unittest.TestCase):
    """The two-line android block, pinned: print_summary row style, the
    configured host dials the same target setup writes to the phone, and
    an unusable alias never reaches a paste-able command."""

    FIRST = "  phone       run  tendril --upgrade  in Termux"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril notice ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "remote-agents"))

    def notice(self, config):
        with open(os.path.join(self.home, ".config", "remote-agents",
                               "config"), "w") as f:
            f.write(config)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return tu.android_notice()

    def test_ssh_target_used_and_both_lines_exact(self):
        lines = self.notice(
            "SSH_TARGET=omarchy.tail1234.ts.net\n").splitlines()
        self.assertEqual(lines, [
            self.FIRST,
            "              first time on an older phone:  "
            "ssh omarchy.tail1234.ts.net remote-agents --phone-script "
            "upgrade > $PREFIX/tmp/tu.sh && sh $PREFIX/tmp/tu.sh"])

    def test_ssh_alias_is_the_fallback_and_target_wins(self):
        second = self.notice("SSH_ALIAS=tendril-test\n").splitlines()[1]
        self.assertIn("ssh tendril-test remote-agents", second)
        both = self.notice("SSH_TARGET=real.host\n"
                           "SSH_ALIAS=tendril-test\n")
        self.assertIn("ssh real.host remote-agents", both)

    def test_unsafe_or_missing_alias_stays_a_template(self):
        for config in ("", "SSH_TARGET=bad host\n",
                       "SSH_TARGET='x;rm -rf'\n", "SSH_TARGET=-evil\n"):
            with self.subTest(config=config):
                self.assertIn("ssh <alias> remote-agents",
                              self.notice(config))


class UpgradeRefusals(UpgradeEnv):
    """Every refusal: one line, non-zero exit, and strictly nothing done."""

    def setUp(self):
        super().setUp()
        self.stub_linux_service()
        self.stub_launchd_service()
        self.stub_herdr()
        self.record_provenance()
        self.head = git_ok(["rev-parse", "HEAD"], cwd=self.work)

    def assert_refused(self, p, needle, head=None):
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(needle, p.stderr)
        self.assertNotIn("UPGRADE COMPLETE", p.stdout)
        self.assertEqual(git_ok(["rev-parse", "HEAD"], cwd=self.work),
                         head or self.head)
        self.assertFalse(os.path.exists(self.installed("remote-agents")),
                         "refusal must not install anything")

    def test_dirty_tree(self):
        with open(os.path.join(self.work, "install"), "a") as f:
            f.write("# local edit\n")
        with open(os.path.join(self.work, "untracked.txt"), "w") as f:
            f.write("stray\n")
        self.assert_refused(self.run_module(), "uncommitted change")

    def test_merge_in_progress(self):
        with open(os.path.join(self.work, ".git", "MERGE_HEAD"), "w") as f:
            f.write(self.head + "\n")
        self.assert_refused(self.run_module(), "merge, rebase or cherry-pick")

    def test_detached_head(self):
        git_ok(["checkout", "--detach", "--quiet"], cwd=self.work)
        self.assert_refused(self.run_module(), "detached")

    def test_local_commits_ahead(self):
        with open(os.path.join(self.work, "local.txt"), "w") as f:
            f.write("local only\n")
        git_ok(["add", "-A"], cwd=self.work)
        git_ok(["commit", "--quiet", "-m", "local"], cwd=self.work)
        local_head = git_ok(["rev-parse", "HEAD"], cwd=self.work)
        self.assert_refused(self.run_module(),
                            "local commit(s) not in the upstream",
                            head=local_head)

    def test_diverged(self):
        with open(os.path.join(self.work, "local.txt"), "w") as f:
            f.write("local only\n")
        git_ok(["add", "-A"], cwd=self.work)
        git_ok(["commit", "--quiet", "-m", "local"], cwd=self.work)
        advance_upstream(self.tmp, self.bare, {"bin/remote-agents": "# x\n"})
        local_head = git_ok(["rev-parse", "HEAD"], cwd=self.work)
        self.assert_refused(self.run_module(), "diverged", head=local_head)

    def test_branch_mismatch(self):
        git_ok(["checkout", "--quiet", "-b", "side-branch"], cwd=self.work)
        self.assert_refused(self.run_module(),
                            "'side-branch' but the install recorded 'main'")

    def test_no_upstream(self):
        git_ok(["branch", "--unset-upstream"], cwd=self.work)
        self.assert_refused(self.run_module(), "no upstream")

    def test_fetch_failure(self):
        shutil.rmtree(self.bare)
        self.assert_refused(self.run_module(), "git fetch failed")

    def run_installed_copy(self):
        """The module in an installed context: not inside any checkout."""
        installed_bin = os.path.join(self.home, ".local", "bin")
        os.makedirs(installed_bin, exist_ok=True)
        helper = os.path.join(installed_bin, "tendril_upgrade.py")
        shutil.copy2(MODULE, helper)
        return sh([sys.executable, helper], cwd=self.tmp, env=self.env())

    def test_missing_provenance_and_no_checkout_gives_guidance(self):
        shutil.rmtree(os.path.join(self.home, ".config", "remote-agents"))
        p = self.run_installed_copy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no TENDRIL source checkout found", p.stderr)
        self.assertIn("./install", p.stderr)

    def test_gone_provenance_checkout(self):
        shutil.rmtree(self.work)
        p = self.run_installed_copy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("recorded source checkout is gone", p.stderr)


class InstallerUpgradeMode(FakeDarwin):
    """`./install --upgrade` on its own, under the fake Darwin userland
    (launchd backend, recording launchctl)."""

    def setUp(self):
        super().setUp()
        # FakeDarwin's launchctl stub always succeeds; the recording variant
        # can simulate a not-loaded agent for the start-path test.
        self.stub("launchctl", LAUNCHCTL_STUB)
        conf = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf)
        with open(os.path.join(conf, "config"), "wb") as f:
            f.write(CUSTOM_CONFIG)
        with open(os.path.join(conf, "notify.env"), "wb") as f:
            f.write(CUSTOM_NOTIFY)

    def upgrade(self, **env_over):
        return self.run_script(INSTALL, ["--upgrade"], [], **env_over)

    def conf_path(self, name):
        return os.path.join(self.home, ".config", "remote-agents", name)

    def installed(self, name):
        return os.path.join(self.home, ".local", "bin", name)

    def seed_config_toml(self):
        conf = os.path.join(self.home, ".config", "herdr")
        os.makedirs(conf, exist_ok=True)
        path = os.path.join(conf, "config.toml")
        with open(path, "w") as f:
            f.write('# my own binding, must survive every upgrade\n'
                    '[keys]\ndetach = ["alt+x"]\n')
        return path

    def test_upgrade_installs_and_repeats_idempotently(self):
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("provenance recorded", p.stdout)
        self.assertTrue(os.path.isfile(self.installed("tendril_upgrade.py")))
        self.assertTrue(os.path.islink(self.installed("tendril")))
        doc = json.loads(self.read(self.conf_path("install.json")))
        self.assertEqual(doc["source_dir"], REPO)
        self.assertEqual(doc["version"],
                         read_file(os.path.join(REPO, "VERSION")).strip())
        self.assertEqual(doc["phone"]["tendril"],
                         sha(os.path.join(REPO, "phone", "tendril")))
        before = self.launchctl_calls()
        for _ in range(2):
            self.assertEqual(self.upgrade().returncode, 0)
        for rc in (".profile", ".zshenv"):
            body = self.read(rc)
            self.assertEqual(body.count("# >>> tendril remote PATH"), 1, rc)
        self.assertEqual(read_file(self.conf_path("notify.env"), "rb"),
                         CUSTOM_NOTIFY)
        self.assertEqual(read_file(self.conf_path("config"), "rb"),
                         CUSTOM_CONFIG)
        # nothing changed on repeats: no restart actions, no plist churn
        restarts = [c for c in self.launchctl_calls()[len(before):]
                    if c.startswith(("bootout", "bootstrap"))]
        self.assertEqual(restarts, [])
        plist = os.path.join(self.home, "Library", "LaunchAgents",
                             "com.tendril.herdr-notify.plist")
        self.assertTrue(os.path.isfile(plist))
        self.assertEqual([a for a in os.listdir(os.path.dirname(plist))
                          if "tendril" in a],
                         ["com.tendril.herdr-notify.plist"])

    def test_upgrade_merges_bindings_once_and_keeps_user_lines(self):
        self.seed_config_toml()
        self.assertEqual(self.upgrade().returncode, 0)
        toml = os.path.join(self.home, ".config", "herdr", "config.toml")
        after_first = self.read(toml)
        self.assertIn("alt+x", after_first)           # user chord survived
        self.assertIn("ctrl+home", after_first)       # managed set applied
        self.assertEqual(self.upgrade().returncode, 0)
        self.assertEqual(self.read(toml), after_first)  # second: byte-stable
        self.assertEqual(after_first.count("alt+x"), 1)
        self.assertEqual(after_first.count("ctrl+home"), 1)

    def test_upgrade_starts_a_watcher_that_is_not_loaded(self):
        self.assertEqual(self.upgrade().returncode, 0)
        before = self.launchctl_calls()
        p = self.upgrade(TENDRIL_LAUNCHCTL_PRINT_RC="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("refresh: started", p.stdout)
        delta = self.launchctl_calls()[len(before):]
        self.assertEqual(len([c for c in delta if c.startswith("bootstrap")]),
                         1)
        self.assertEqual([c for c in delta if c.startswith("bootout")], [])

    def test_upgrade_restarts_when_watcher_binary_changed(self):
        self.assertEqual(self.upgrade().returncode, 0)
        installed = self.installed("herdr-notify")
        with open(installed, "w") as f:
            f.write("#!/bin/sh\n# stale\n")
        before = self.launchctl_calls()
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("refresh: restarted", p.stdout)
        actions = [c for c in self.launchctl_calls()[len(before):]
                   if c.startswith(("bootout", "bootstrap"))]
        self.assertEqual(len(actions), 2)             # bootout + bootstrap
        self.assertEqual(self.read(installed),
                         read_file(os.path.join(REPO, "bin", "herdr-notify")))

    def test_upgrade_rewrites_a_modified_plist_exactly_once(self):
        self.assertEqual(self.upgrade().returncode, 0)
        plist = os.path.join(self.home, "Library", "LaunchAgents",
                             "com.tendril.herdr-notify.plist")
        with open(plist, "rb") as f:
            doc = plistlib.loads(f.read())
        doc["ThrottleInterval"] = 9
        with open(plist, "wb") as f:
            plistlib.dump(doc, f)
        before = self.launchctl_calls()
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("refresh: restarted", p.stdout)
        self.assertEqual(
            len([c for c in self.launchctl_calls()[len(before):]
                 if c.startswith("bootstrap")]), 1)
        with open(plist, "rb") as f:
            self.assertEqual(plistlib.loads(f.read())["ThrottleInterval"], 5)

    def test_upgrade_dry_run_writes_nothing(self):
        before = self.walk_home()      # seeded config/notify.env count as state
        p = self.run_script(INSTALL, ["--upgrade", "--dry-run"], [])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.walk_home(), before)

    def test_first_install_still_works_and_records_provenance(self):
        p = self.run_script(INSTALL, [], ["", "", "", "n", "", "n", "", ""])
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        doc = json.loads(self.read(self.conf_path("install.json")))
        self.assertEqual(doc["source_dir"], REPO)
        self.assertEqual(doc["phone"]["termux-url-opener"],
                         sha(os.path.join(REPO, "phone",
                                          "termux-url-opener")))
        self.assertIn("provenance recorded", out)


class Interface(unittest.TestCase):
    """--version / --help / the thin dispatch / the missing word form."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril iface ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def env(self):
        e = dict(os.environ)
        e["HOME"] = self.home
        e["PATH"] = "/usr/bin:/bin"
        e.pop("XDG_CONFIG_HOME", None)
        return e

    def run_cli(self, script, *args):
        return sh([sys.executable, script] + list(args), env=self.env())

    def test_version_from_checkout(self):
        p = self.run_cli(REMOTE_AGENTS, "--version")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith("tendril 0.2.0-dev"), p.stdout)

    def test_module_version_subcommand(self):
        p = self.run_cli(MODULE, "version")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith("tendril 0.2.0-dev"))

    def test_help_lists_upgrade_and_version(self):
        p = self.run_cli(REMOTE_AGENTS, "--help")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for needle in ("--upgrade", "--version", "service status|start|stop",
                       "session-token", "fast-forward"):
            self.assertIn(needle, p.stdout)

    def test_bare_upgrade_word_is_not_a_verb(self):
        # a workspace labeled "upgrade" would be shadowed by the word form,
        # so only the flag exists: the bare word stays a token position.
        p = self.run_cli(REMOTE_AGENTS, "upgrade")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--upgrade", p.stderr)

    def test_flag_dispatch_end_to_end(self):
        """remote-agents --upgrade runs the same pipeline (already-current
        path, so nothing is installed or restarted)."""
        stub = DispatchEnv(self.tmp, self.home)
        try:
            p = sh([sys.executable, REMOTE_AGENTS, "--upgrade"],
                   cwd=self.tmp, env=stub.env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("nothing to refresh", p.stdout)
            self.assertIn("UPGRADE COMPLETE", p.stdout)
            self.assertIn("  host        already current", p.stdout)
        finally:
            shutil.rmtree(stub.tmp, True)


class DispatchEnv:
    """Minimal checkout + fake HOME + stubs for the dispatch test."""

    def __init__(self, parent_tmp, home):
        self.tmp = tempfile.mkdtemp(prefix="tendril dispatch ",
                                    dir=parent_tmp)
        self.home = home
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.bindir)
        for name, body in (("herdr", HERDR_STUB),
                           ("systemctl", SYSTEMCTL_STUB),
                           ("launchctl", LAUNCHCTL_STUB)):
            with open(os.path.join(self.bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.bindir, name), 0o755)
        self.work, self.bare = make_checkout(self.tmp)
        p = sh([sys.executable, MODULE, "record-provenance", "--pkg",
                os.path.join(self.work, "bin")], env=self.env())
        if p.returncode != 0:
            raise AssertionError(p.stdout + p.stderr)

    def env(self):
        e = dict(os.environ)
        e["HOME"] = self.home
        e["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        e["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "xdg-run")
        os.makedirs(e["XDG_RUNTIME_DIR"], exist_ok=True)
        for k in ("XDG_CONFIG_HOME", "HERDR_BIN"):
            e.pop(k, None)
        e["TENDRIL_HERDR_LOG"] = os.path.join(self.tmp, "herdr.log")
        e["TENDRIL_LAUNCHCTL_LOG"] = os.path.join(self.tmp, "launchctl.log")
        e["TENDRIL_SYSTEMCTL_LOG"] = os.path.join(self.tmp, "systemctl.log")
        return e


if __name__ == "__main__":
    unittest.main()
