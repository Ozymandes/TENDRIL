"""tendril_host on a simulated Darwin and a simulated Linux.

Unit tests with injected fakes: host facts (sysctl/vm_stat/pmset and
/proc), launchd + systemd lifecycle against a recording `run`, backend()
capability selection, the managed remote-PATH block (including real
`sh`/`bash`/`zsh` sourcing), and the CLI verb table. The real home is
never touched: every filesystem-touching test runs with HOME pointed at
a temp dir whose name contains a space (macOS-style) and with
XDG_CONFIG_HOME removed.
"""
import contextlib
import io
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_host as th  # noqa: E402

UID = 501

# --- realistic Darwin fixtures -------------------------------------------------
VMSTAT_ASI = (
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
    "Pages free: 21894.\n"
    "Pages active: 426591.\n"
    "Pages inactive: 201301.\n"
    "Pages speculative: 51234.\n"
    "Pages wired down: 178230.\n"
    "Pages purgeable: 16.\n"
    "Pages occupying compressed: 930112.\n"
)
VMSTAT_INTEL = VMSTAT_ASI.replace(
    "page size of 16384 bytes", "page size of 4096 bytes")
VMSTAT_NO_PAGESIZE = VMSTAT_ASI.replace(
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)",
    "Mach Virtual Memory Statistics")
BOOTTIME = "{ sec = 1700000000, usec = 0 } Tue Nov 14 06:13:20 2023 PST\n"
PMSET_BATT = ("-InternalBattery-0 (id=5302241)\t87%; discharging; "
              "4:11 remaining present: true\n")
PMSET_AC = "Now drawing from 'AC Power'\n"
MEMINFO = ("MemTotal:       16384000 kB\n"
           "MemFree:        4200000 kB\n"
           "MemAvailable:   9800000 kB\n"
           "Buffers:         300000 kB\n"
           "Cached:         4400000 kB\n")
MEMINFO_NO_AVAIL = ("MemTotal:       16384000 kB\n"
                    "MemFree:        4200000 kB\n"
                    "Buffers:         300000 kB\n")


def fake_exists(hidden=("/proc", "/sys")):
    """An os.path.exists that hides procfs/sysfs but stays honest elsewhere
    (shutil.which also consults os.path.exists, so a blanket False lies)."""
    real = os.path.exists

    def exists(path, *a, **kw):
        if isinstance(path, str) and path.startswith(hidden):
            return False
        return real(path, *a, **kw)

    return exists


def recorder(reply=None, default=(0, "", "")):
    """A tendril_host-style run() that records every argv it is handed.
    reply(args) may return an (rc, out, err) triple; None falls through."""
    calls = []
    envs = []

    def run(args, timeout=6, env=None, **kw):
        calls.append(list(args))
        envs.append(env)
        if reply is not None:
            res = reply(args)
            if res is not None:
                return res
        return default

    run.calls = calls
    run.envs = envs
    return run


class HomePatched(unittest.TestCase):
    """HOME -> temp dir with a space in the name; XDG_CONFIG_HOME gone."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril home ")
        self.addCleanup(shutil.rmtree, self.home, True)
        old_home = os.environ.get("HOME")
        old_xdg = os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ["HOME"] = self.home

        def restore():
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_xdg is not None:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

        self.addCleanup(restore)


# ---------------------------------------------------------------- facts
class ShortHostname(unittest.TestCase):
    def test_strips_mdns_local_suffix(self):
        self.assertEqual(th.short_hostname("Janes-MacBook-Pro.local"),
                         "Janes-MacBook-Pro")

    def test_strip_is_case_insensitive(self):
        self.assertEqual(th.short_hostname("MACBOOK.LOCAL"), "MACBOOK")
        self.assertEqual(th.short_hostname("box.LoCaL"), "box")

    def test_plain_names_pass_through(self):
        self.assertEqual(th.short_hostname("box"), "box")
        self.assertEqual(th.short_hostname("host.localx"), "host.localx")
        self.assertEqual(th.short_hostname("a.b.c"), "a.b.c")


class FormatUptime(unittest.TestCase):
    def test_unknown_and_negative(self):
        self.assertEqual(th.format_uptime(None), "?")
        self.assertEqual(th.format_uptime(-1), "?")

    def test_minutes_hours_days(self):
        self.assertEqual(th.format_uptime(0), "0m")
        self.assertEqual(th.format_uptime(59), "0m")
        self.assertEqual(th.format_uptime(60), "1m")
        self.assertEqual(th.format_uptime(3599), "59m")
        self.assertEqual(th.format_uptime(3600), "1h 00m")
        self.assertEqual(th.format_uptime(7320), "2h 02m")
        self.assertEqual(th.format_uptime(90000), "1d 1h")
        self.assertEqual(th.format_uptime(172800), "2d 0h")


class TailscaleCli(unittest.TestCase):
    def test_path_wins(self):
        which = lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
        self.assertEqual(th.tailscale_cli(which=which, exists=lambda p: False),
                         "tailscale")

    def test_app_bundle_fallback(self):
        app = th.TAILSCALE_APP
        self.assertEqual(th.tailscale_cli(which=lambda n: None,
                                          exists=lambda p: p == app), app)

    def test_absent(self):
        self.assertIsNone(th.tailscale_cli(which=lambda n: None,
                                           exists=lambda p: False))


class TailscaleName(unittest.TestCase):
    """The CLI is injected, so these hold whether or not the machine
    running the tests has Tailscale (CI runners do not)."""

    def test_first_label_of_self_dnsname(self):
        def reply(args):
            if args == ["tailscale", "status", "--json"]:
                return 0, json.dumps(
                    {"Self": {"DNSName": "janes-mac.tail1234.ts.net."}}), ""
            return None

        run = recorder(reply)
        self.assertEqual(th.tailscale_name(run=run, cli="tailscale"), "janes-mac")
        self.assertEqual(run.calls, [["tailscale", "status", "--json"]])

    def test_app_bundle_cli_is_used_as_given(self):
        run = recorder(lambda a: (0, json.dumps({"Self": {"DNSName": "m.ts.net."}}), ""))
        self.assertEqual(th.tailscale_name(run=run, cli=th.TAILSCALE_APP), "m")
        self.assertEqual(run.calls[0][0], th.TAILSCALE_APP)

    def test_down_cli_gives_empty(self):
        run = recorder(lambda a: (7, "", "boom"))
        self.assertEqual(th.tailscale_name(run=run, cli="tailscale"), "")
        self.assertEqual(len(run.calls), 1)          # really asked, really failed

    def test_bad_json_gives_empty(self):
        run = recorder(lambda a: (0, "<html>not json</html>", ""))
        self.assertEqual(th.tailscale_name(run=run, cli="tailscale"), "")
        self.assertEqual(len(run.calls), 1)

    def test_missing_self_gives_empty(self):
        run = recorder(lambda a: (0, json.dumps({"Self": None}), ""))
        self.assertEqual(th.tailscale_name(run=run, cli="tailscale"), "")
        self.assertEqual(len(run.calls), 1)

    def test_no_cli_anywhere_gives_empty_without_running_anything(self):
        run = recorder(lambda a: (0, "{}", ""))
        with mock.patch.object(th, "tailscale_cli", return_value=None):
            self.assertEqual(th.tailscale_name(run=run), "")
            self.assertEqual(th.tailscale_ip(run=run), "")
        self.assertEqual(run.calls, [])

    def test_ip_first_line(self):
        run = recorder(lambda a: (0, "100.64.0.7\n", ""))
        self.assertEqual(th.tailscale_ip(run=run, cli="tailscale"), "100.64.0.7")


class MemoryLinux(unittest.TestCase):
    def test_meminfo_with_memavailable(self):
        mem = th.memory_mb(read=lambda p: MEMINFO if p == "/proc/meminfo"
                           else None)
        self.assertEqual(mem, (9570, 16000))

    def test_meminfo_falls_back_to_memfree(self):
        mem = th.memory_mb(read=lambda p: MEMINFO_NO_AVAIL
                           if p == "/proc/meminfo" else None)
        self.assertEqual(mem, (4101, 16000))

    def test_no_memtotal_is_none(self):
        mem = th.memory_mb(read=lambda p: "SwapTotal: 8000000 kB\n"
                           if p == "/proc/meminfo" else None)
        self.assertIsNone(mem)

    def test_existing_but_unreadable_meminfo_is_none(self):
        # /proc/meminfo exists (on Linux this is already true; on macOS the
        # patch forces the same branch) but read() returns None.
        with mock.patch("os.path.exists", return_value=True):
            mem = th.memory_mb(run=recorder(), read=lambda p: None)
        self.assertIsNone(mem)


class MemoryDarwin(unittest.TestCase):
    def darwin(self, vmstat=VMSTAT_ASI, memsize="17179869184\n",
               memsize_rc=0, vmstat_rc=0):
        def reply(args):
            if args[:3] == ["sysctl", "-n", "hw.memsize"]:
                return memsize_rc, memsize, ""
            if args == ["vm_stat"]:
                return vmstat_rc, vmstat, ""
            return None

        return recorder(reply)

    def test_apple_silicon_page_size_16384(self):
        run = self.darwin()
        with mock.patch("os.path.exists", fake_exists()):
            mem = th.memory_mb(run=run, read=lambda p: None)
        # free+inactive+speculative = 274429 pages * 16384 bytes
        self.assertEqual(mem, (4287, 16384))
        self.assertEqual(run.calls, [["sysctl", "-n", "hw.memsize"],
                                     ["vm_stat"]])

    def test_intel_page_size_4096(self):
        run = self.darwin(vmstat=VMSTAT_INTEL)
        with mock.patch("os.path.exists", fake_exists()):
            mem = th.memory_mb(run=run, read=lambda p: None)
        # 274429 pages * 4096 bytes, floored to MiB
        self.assertEqual(mem, (1071, 16384))

    def test_missing_page_size_line_defaults_to_4096(self):
        run = self.darwin(vmstat=VMSTAT_NO_PAGESIZE)
        with mock.patch("os.path.exists", fake_exists()):
            mem = th.memory_mb(run=run, read=lambda p: None)
        self.assertEqual(mem, (1071, 16384))

    def test_bad_memsize_is_none(self):
        run = self.darwin(memsize="not a number\n")
        with mock.patch("os.path.exists", fake_exists()):
            mem = th.memory_mb(run=run, read=lambda p: None)
        self.assertIsNone(mem)

    def test_vm_stat_failure_is_none(self):
        run = self.darwin(vmstat_rc=1)
        with mock.patch("os.path.exists", fake_exists()):
            mem = th.memory_mb(run=run, read=lambda p: None)
        self.assertIsNone(mem)


class Battery(unittest.TestCase):
    def test_linux_sysfs_wins_without_running_anything(self):
        run = recorder()
        bat = th.battery(run=run, read=lambda p: "87\n"
                         if p.endswith("/BAT0/capacity") else None)
        self.assertEqual(bat, "87%")
        self.assertEqual(run.calls, [])

    def test_pmset_discharging(self):
        rc = lambda a: (0, PMSET_BATT, "") if a == ["pmset", "-g", "batt"] \
            else None
        with mock.patch("shutil.which", return_value="/usr/bin/pmset"):
            bat = th.battery(run=recorder(rc), read=lambda p: None)
        self.assertEqual(bat, "87%")

    def test_desktop_mac_without_battery_is_empty(self):
        rc = lambda a: (0, PMSET_AC, "")
        with mock.patch("shutil.which", return_value="/usr/bin/pmset"):
            bat = th.battery(run=recorder(rc), read=lambda p: None)
        self.assertEqual(bat, "")

    def test_no_pmset_is_empty(self):
        with mock.patch("shutil.which", return_value=None):
            run = recorder()
            bat = th.battery(run=run, read=lambda p: None)
        self.assertEqual(bat, "")
        self.assertEqual(run.calls, [])


class UptimeSeconds(unittest.TestCase):
    def test_linux_proc_uptime(self):
        up = th.uptime_seconds(read=lambda p: "12345.67 23456.78\n",
                               procfs=True)
        self.assertEqual(up, 12345)

    def test_unreadable_or_garbage_proc_uptime(self):
        self.assertIsNone(th.uptime_seconds(read=lambda p: None, procfs=True))
        self.assertIsNone(th.uptime_seconds(read=lambda p: "garbage\n",
                                            procfs=True))

    def test_darwin_boottime(self):
        rc = lambda a: (0, BOOTTIME, "") \
            if a == ["sysctl", "-n", "kern.boottime"] else None
        up = th.uptime_seconds(run=recorder(rc), procfs=False,
                               now=lambda: 1700003600.0)
        self.assertEqual(up, 3600)

    def test_darwin_boottime_failures(self):
        rc = lambda a: (1, "", "oops")
        self.assertIsNone(th.uptime_seconds(run=recorder(rc), procfs=False,
                                            now=lambda: 1.0))
        rc = lambda a: (0, "sec = potato\n", "")
        self.assertIsNone(th.uptime_seconds(run=recorder(rc), procfs=False,
                                            now=lambda: 1.0))


class Facts(HomePatched):
    def test_facts_on_simulated_darwin(self):
        def reply(args):
            if args[:3] == ["sysctl", "-n", "hw.memsize"]:
                return 0, "17179869184\n", ""
            if args == ["vm_stat"]:
                return 0, VMSTAT_ASI, ""
            if args[:3] == ["sysctl", "-n", "kern.boottime"]:
                return 0, BOOTTIME, ""
            if args == ["pmset", "-g", "batt"]:
                return 0, PMSET_BATT, ""
            return None

        run = recorder(reply)
        with mock.patch("os.path.exists", fake_exists()), \
                mock.patch("shutil.which", return_value="/usr/bin/pmset"):
            doc = th.facts(run=run, read=lambda p: None)
        self.assertEqual(doc["host"], th.short_hostname())
        self.assertEqual(doc["tailscale"], "down")     # no CLI, no app bundle
        self.assertEqual(doc["mem_available_mb"], 4287)
        self.assertEqual(doc["mem_total_mb"], 16384)
        self.assertEqual(doc["battery"], "87%")
        self.assertRegex(doc["uptime"], r"^\d+d \d+h$")
        self.assertEqual(len(doc["load"]), 3)


# ---------------------------------------------------------------- launchd
class LaunchdRender(HomePatched):
    PY = "/Users/Jöhn & Co <x>/.local/bin/python3"
    SCRIPT = "/Users/Jöhn & Co <x>/.local/bin/herdr-notify"

    def setUp(self):
        super().setUp()
        self.body = th.Launchd(run=recorder(), uid=UID).render(self.PY,
                                                               self.SCRIPT)
        self.doc = plistlib.loads(self.body)

    def test_program_arguments_survive_plist_round_trip(self):
        # spaces, '&', '<', quotes and non-ASCII must be escaped and exact
        self.assertEqual(self.doc["ProgramArguments"], [self.PY, self.SCRIPT])

    def test_label(self):
        self.assertEqual(self.doc["Label"], th.LABEL)
        self.assertEqual(th.LABEL, "com.tendril.herdr-notify")

    def test_log_is_private(self):
        # the watcher log names workspaces; keep it out of other users' reach
        self.assertEqual(self.doc["Umask"], 0o077)

    def test_keepalive_and_runatload(self):
        self.assertTrue(self.doc["RunAtLoad"])
        self.assertEqual(self.doc["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(self.doc["ThrottleInterval"], 5)
        self.assertEqual(self.doc["ProcessType"], "Background")

    def test_no_systemd_style_keys(self):
        self.assertNotIn("WatchdogSec", self.doc)
        self.assertNotIn(b"WatchdogSec", self.body)
        self.assertNotIn(b"Restart", self.body)

    def test_logs_live_under_library_logs(self):
        log = os.path.join(self.home, "Library", "Logs", "tendril",
                           "herdr-notify.log")
        self.assertEqual(self.doc["StandardOutPath"], log)
        self.assertEqual(self.doc["StandardErrorPath"], log)

    def test_path_orders_local_bin_and_both_brew_prefixes_first(self):
        pathv = self.doc["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(pathv[0], os.path.join(self.home, ".local", "bin"))
        self.assertIn("/opt/homebrew/bin", pathv)      # arm64 Homebrew
        self.assertIn("/usr/local/bin", pathv)         # Intel Homebrew
        self.assertLess(pathv.index("/opt/homebrew/bin"), pathv.index("/usr/bin"))
        self.assertLess(pathv.index("/usr/local/bin"), pathv.index("/usr/bin"))
        for tail in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(tail, pathv)


def _write_plist(b, python="/usr/bin/python3"):
    os.makedirs(os.path.dirname(b.plist), exist_ok=True)
    with open(b.plist, "wb") as f:
        f.write(b.render(python, os.path.join(b.home_stub, "herdr-notify")))


class LaunchdLifecycle(HomePatched):
    def make(self, reply=None, default=(0, "", ""), dry=False):
        run = recorder(reply, default)
        b = th.Launchd(run=run, dry_run=dry, out=io.StringIO(), uid=UID)
        b.home_stub = os.path.join(self.home, ".local", "bin")
        return b, run

    def seed(self, b):
        os.makedirs(os.path.dirname(b.plist), exist_ok=True)
        with open(b.plist, "wb") as f:
            f.write(b.render("/usr/bin/python3", "/x/herdr-notify"))

    def test_install_writes_plist_then_bootout_enable_bootstrap(self):
        b, run = self.make()
        self.assertEqual(b.install("/pkg", "/usr/bin/python3"), 0)
        self.assertTrue(os.path.isfile(b.plist))
        self.assertEqual(stat.S_IMODE(os.stat(b.plist).st_mode), 0o644)
        doc = plistlib.loads(open(b.plist, "rb").read())
        self.assertEqual(doc["ProgramArguments"],
                         ["/usr/bin/python3",
                          os.path.join(self.home, ".local", "bin",
                                       "herdr-notify")])
        self.assertTrue(os.path.isdir(os.path.join(self.home, "Library",
                                                   "Logs", "tendril")))
        self.assertEqual(run.calls[0], ["launchctl", "print", "gui/%d" % UID])
        self.assertEqual(run.calls[1],
                         ["launchctl", "bootout",
                          "gui/%d/%s" % (UID, th.LABEL)])
        self.assertEqual(run.calls[2],
                         ["launchctl", "enable",
                          "gui/%d/%s" % (UID, th.LABEL)])
        self.assertEqual(run.calls[3],
                         ["launchctl", "bootstrap", "gui/%d" % UID, b.plist])

    def test_install_reports_failure(self):
        def reply(args):
            return (1, "", "denied") if args[1] == "bootstrap" else None

        b, _ = self.make(reply)
        self.assertEqual(b.install("/pkg", "/usr/bin/python3"), 1)

    def test_domain_falls_back_to_user_domain(self):
        def reply(args):
            # `launchctl print gui/<uid>` fails: bare SSH login, no GUI session
            return (1, "", "no gui") if args[2] == "gui/%d" % UID else None

        b, run = self.make(reply)
        b.install("/pkg", "/usr/bin/python3")
        boot = run.calls[3]
        self.assertEqual(boot[2], "user/%d" % UID)
        self.assertTrue(run.calls[1][2].startswith("user/%d/" % UID))

    def test_start_uses_kickstart_when_loaded(self):
        b, run = self.make()
        self.seed(b)
        self.assertEqual(b.start(), 0)
        # loaded() -> target() -> domain() probe + target probe; kickstart
        # re-resolves the domain once more
        self.assertEqual(run.calls,
                         [["launchctl", "print", "gui/%d" % UID],
                          ["launchctl", "print",
                           "gui/%d/%s" % (UID, th.LABEL)],
                          ["launchctl", "print", "gui/%d" % UID],
                          ["launchctl", "kickstart",
                           "gui/%d/%s" % (UID, th.LABEL)]])

    def test_start_bootstraps_when_not_loaded(self):
        def reply(args):
            if args[2].endswith("/" + th.LABEL):
                return 1, "", "not loaded"
            return None

        b, run = self.make(reply)
        self.seed(b)
        self.assertEqual(b.start(), 0)
        self.assertEqual(run.calls[-1],
                         ["launchctl", "bootstrap", "gui/%d" % UID, b.plist])

    def test_start_without_install_fails_without_running_anything(self):
        b, run = self.make()
        self.assertEqual(b.start(), 1)
        self.assertEqual(run.calls, [])

    def test_stop_bootouts_and_never_kills(self):
        b, run = self.make()
        self.seed(b)
        self.assertEqual(b.stop(), 0)
        self.assertEqual(run.calls,
                         [["launchctl", "print", "gui/%d" % UID],
                          ["launchctl", "bootout",
                           "gui/%d/%s" % (UID, th.LABEL)]])
        self.assertTrue(all("kill" not in " ".join(c) for c in run.calls))

    def test_restart_kickstarts_when_loaded(self):
        b, run = self.make()
        self.seed(b)
        self.assertEqual(b.restart(), 0)
        self.assertEqual(run.calls[-1],
                         ["launchctl", "kickstart", "-k",
                          "gui/%d/%s" % (UID, th.LABEL)])

    def test_restart_falls_back_to_start_when_not_loaded(self):
        def reply(args):
            if args[2].endswith("/" + th.LABEL):
                return 1, "", "not loaded"
            return None

        b, run = self.make(reply)
        self.seed(b)
        self.assertEqual(b.restart(), 0)
        self.assertEqual(run.calls[-1][1], "bootstrap")

    def test_status_states(self):
        b, _ = self.make()
        self.assertEqual(b.status(), "not installed")
        self.seed(b)
        b2, run = self.make()
        b2.plist = b.plist
        run_reply = lambda a: (0, "\tstate = running\n\tpid = 91\n", "")
        b3, run3 = self.make(run_reply)
        b3.plist = b.plist
        self.assertEqual(b3.status(), "running")
        b4, run4 = self.make(lambda a: (0, "\tstate = waiting\n", ""))
        b4.plist = b.plist
        self.assertEqual(b4.status(), "stopped")
        b5, run5 = self.make(lambda a: (1, "", ""))
        b5.plist = b.plist
        self.assertEqual(b5.status(), "stopped")

    def test_uninstall_removes_only_our_plist(self):
        other = os.path.join(self.home, "Library", "LaunchAgents",
                             "com.example.other.plist")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w") as f:
            f.write("<plist/>")
        b, run = self.make()
        self.seed(b)
        self.assertEqual(b.uninstall(), 0)
        self.assertFalse(os.path.exists(b.plist))
        self.assertTrue(os.path.isfile(other))       # untouched
        self.assertTrue(any(c[1:2] == ["bootout"] for c in run.calls))

    def test_install_is_idempotent(self):
        b, _ = self.make()
        b.install("/pkg", "/usr/bin/python3")
        first = open(b.plist, "rb").read()
        b2, run2 = self.make()
        b2.install("/pkg", "/usr/bin/python3")
        self.assertEqual(open(b2.plist, "rb").read(), first)
        self.assertEqual(os.listdir(os.path.dirname(b.plist)),
                         [th.LABEL + ".plist"])
        self.assertEqual(run2.calls[1][1], "bootout")   # reloads, not stacks

    def test_dry_run_writes_nothing(self):
        out = io.StringIO()
        b, _ = self.make(dry=True)
        b.out = out
        self.assertEqual(b.install("/pkg", "/usr/bin/python3"), 0)
        self.assertFalse(os.path.exists(os.path.dirname(b.plist)))
        text = out.getvalue()
        self.assertIn("[dry-run]", text)
        self.assertIn(b.plist, text)
        self.assertIn("launchctl bootstrap", text)


# ---------------------------------------------------------------- systemd
class SystemdLifecycle(HomePatched):
    def make_pkg(self):
        pkg = tempfile.mkdtemp(prefix="tendril pkg ")
        self.addCleanup(shutil.rmtree, pkg, True)
        os.makedirs(os.path.join(pkg, "systemd"))
        src = os.path.join(pkg, "systemd", th.UNIT)
        with open(src, "w") as f:
            f.write("[Unit]\nDescription=test watcher\n\n[Service]\n"
                    "ExecStart=/usr/bin/env herdr-notify\n")
        return pkg

    def make(self, reply=None, default=(0, "", ""), dry=False):
        run = recorder(reply, default)
        b = th.Systemd(run=run, dry_run=dry, out=io.StringIO())
        return b, run

    def test_install_copies_unit_enables_and_reloads(self):
        b, run = self.make()
        pkg = self.make_pkg()
        self.assertEqual(b.install(pkg, "/usr/bin/python3"), 0)
        self.assertTrue(os.path.isfile(b.unit))
        self.assertEqual(stat.S_IMODE(os.stat(b.unit).st_mode), 0o600)
        with open(os.path.join(pkg, "systemd", th.UNIT), "rb") as f:
            self.assertEqual(open(b.unit, "rb").read(), f.read())
        self.assertEqual(run.calls[0],
                         ["systemctl", "--user", "daemon-reload"])
        self.assertEqual(run.calls[1],
                         ["systemctl", "--user", "enable", "--now", th.UNIT])

    def test_uninstall_disables_removes_and_reloads(self):
        b, run = self.make()
        b.install(self.make_pkg(), "/usr/bin/python3")
        run.calls[:] = []
        self.assertEqual(b.uninstall(), 0)
        self.assertFalse(os.path.exists(b.unit))
        self.assertEqual(run.calls[0],
                         ["systemctl", "--user", "disable", "--now", th.UNIT])
        self.assertEqual(run.calls[-1],
                         ["systemctl", "--user", "daemon-reload"])

    def test_uninstall_without_unit_is_a_noop(self):
        b, run = self.make()
        self.assertEqual(b.uninstall(), 0)
        self.assertEqual(run.calls, [])

    def test_status_via_is_active(self):
        b, _ = self.make()
        self.assertEqual(b.status(), "not installed")
        b.install(self.make_pkg(), "/usr/bin/python3")
        b2, _ = self.make(lambda a: (0, "active\n", ""))
        b2.unit = b.unit
        self.assertEqual(b2.status(), "running")
        b3, _ = self.make(lambda a: (3, "inactive\n", ""))
        b3.unit = b.unit
        self.assertEqual(b3.status(), "stopped")

    def test_dry_run_writes_nothing(self):
        b, _ = self.make(dry=True)
        self.assertEqual(b.install(self.make_pkg(), "/usr/bin/python3"), 0)
        self.assertFalse(os.path.exists(b.unit))

    def test_logs_mentions_journalctl(self):
        self.assertIn("journalctl", th.Systemd(run=recorder()).logs())


# ---------------------------------------------------------------- backend
class BackendSelection(unittest.TestCase):
    def test_launchctl_wins(self):
        run = recorder()
        b = th.backend(run=run, which=lambda n: n == "launchctl")
        self.assertIsInstance(b, th.Launchd)
        self.assertEqual(b.name, "launchd")

    def test_running_systemd_user_manager(self):
        for state in ("running\n", "degraded\n"):
            run = recorder(default=(0, state, ""))
            b = th.backend(run=run, which=lambda n: n == "systemctl",
                           env={"XDG_RUNTIME_DIR": "/run/user/1000"})
            self.assertIsInstance(b, th.Systemd, state)
            self.assertEqual(run.calls,
                             [["systemctl", "--user", "is-system-running"]])

    def test_offline_or_starting_systemd_is_none(self):
        for state in ("offline\n", "starting\n", "maintenance\n", ""):
            b = th.backend(run=recorder(default=(0, state, "")),
                           which=lambda n: n == "systemctl",
                           env={"XDG_RUNTIME_DIR": "/run/user/1000"})
            self.assertIsNone(b, repr(state))

    def test_missing_xdg_runtime_dir_never_shells_out(self):
        run = recorder()
        b = th.backend(run=run, which=lambda n: n == "systemctl", env={})
        self.assertIsNone(b)
        self.assertEqual(run.calls, [])

    def test_darwin_like_host_never_calls_systemctl(self):
        run = recorder()
        which = lambda n: n == "launchctl"     # systemctl absent entirely
        b = th.backend(run=run, which=which, env={})
        self.assertIsInstance(b, th.Launchd)
        self.assertEqual(run.calls, [])

    def test_nothing_available_is_none(self):
        self.assertIsNone(th.backend(run=recorder(), which=lambda n: None,
                                     env={}))


# ---------------------------------------------------------------- remote PATH
class ManagedPathBlock(HomePatched):
    USER = "# my own stuff\nexport FOO=bar\n"

    def test_remove_cleans_every_rc_even_after_a_login_shell_change(self):
        # applied as a zsh user, removed while $SHELL says bash
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(th.main(["remote-path", "apply"]), 0)
        self.assertIn(th.BEGIN, open(os.path.join(self.home, ".zshenv")).read())
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(th.main(["remote-path", "remove"]), 0)
        for rc in (".zshenv", ".profile"):
            self.assertNotIn(th.BEGIN, open(os.path.join(self.home, rc)).read(), rc)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bashrc")))

    def test_with_block_is_idempotent(self):
        once = th.with_block(self.USER)
        self.assertEqual(th.with_block(once), once)
        self.assertEqual(once.count(th.BEGIN), 1)

    def test_with_block_on_empty_text_is_just_the_block(self):
        self.assertEqual(th.with_block(""), th.BLOCK)

    def test_strip_removes_only_the_block_byte_for_byte(self):
        self.assertEqual(th.strip_block(th.with_block(self.USER)), self.USER)

    def test_edit_rc_writes_block_backup_and_preserves_mode(self):
        rc = os.path.join(self.home, ".profile")
        with open(rc, "w") as f:
            f.write(self.USER)
        os.chmod(rc, 0o640)
        out = io.StringIO()
        self.assertTrue(th.edit_rc(rc, out=out, stamp=777))
        text = open(rc).read()
        self.assertIn(th.BEGIN, text)
        self.assertIn("export FOO=bar", text)
        self.assertEqual(stat.S_IMODE(os.stat(rc).st_mode), 0o640)
        bak = rc + ".bak.777"
        self.assertTrue(os.path.isfile(bak))
        self.assertEqual(open(bak).read(), self.USER)

    def test_edit_rc_edits_symlink_target_and_keeps_the_link(self):
        target = os.path.join(self.home, "dotfiles", "profile.real")
        os.makedirs(os.path.dirname(target))
        with open(target, "w") as f:
            f.write(self.USER)
        rc = os.path.join(self.home, ".profile")
        os.symlink(target, rc)
        self.assertTrue(th.edit_rc(rc, out=io.StringIO()))
        self.assertTrue(os.path.islink(rc))
        self.assertEqual(os.readlink(rc), target)
        self.assertIn(th.BEGIN, open(target).read())

    def test_edit_rc_apply_twice_changes_nothing_the_second_time(self):
        rc = os.path.join(self.home, ".profile")
        with open(rc, "w") as f:
            f.write(self.USER)
        edit = lambda: th.edit_rc(rc, out=io.StringIO())
        self.assertTrue(edit())
        self.assertFalse(edit())
        self.assertEqual(open(rc).read().count(th.BEGIN), 1)

    def test_edit_rc_remove_restores_user_content(self):
        rc = os.path.join(self.home, ".profile")
        with open(rc, "w") as f:
            f.write(th.with_block(self.USER))
        self.assertTrue(th.edit_rc(rc, remove=True, out=io.StringIO()))
        self.assertNotIn(th.BEGIN, open(rc).read())
        self.assertIn("export FOO=bar", open(rc).read())

    def test_edit_rc_remove_on_missing_file_is_a_noop(self):
        rc = os.path.join(self.home, ".profile")
        self.assertFalse(th.edit_rc(rc, remove=True, out=io.StringIO()))
        self.assertFalse(os.path.exists(rc))

    def test_edit_rc_creates_missing_file_with_safe_mode(self):
        rc = os.path.join(self.home, ".profile")
        self.assertTrue(th.edit_rc(rc, out=io.StringIO()))
        self.assertIn(th.BEGIN, open(rc).read())
        self.assertEqual(stat.S_IMODE(os.stat(rc).st_mode), 0o644)

    def test_edit_rc_dry_run_writes_nothing(self):
        rc = os.path.join(self.home, ".profile")
        with open(rc, "w") as f:
            f.write(self.USER)
        out = io.StringIO()
        self.assertTrue(th.edit_rc(rc, dry_run=True, out=out))
        self.assertEqual(open(rc).read(), self.USER)
        self.assertFalse(os.path.exists(rc + ".bak.%d" % 0)
                         or [p for p in os.listdir(self.home)
                             if p.startswith(".profile.bak")])
        self.assertIn("[dry-run]", out.getvalue())

    def test_remote_rc_files_follow_the_login_shell(self):
        cases = {"zsh": [".zshenv", ".profile"],
                 "bash": [".bashrc", ".profile"],
                 "sh": [".profile"],
                 "fish": [".profile"]}
        for shell, expected in cases.items():
            with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/" + shell}):
                self.assertEqual([os.path.basename(p)
                                  for p in th.remote_rc_files()],
                                 expected, shell)
        # explicit argument wins over the environment
        self.assertEqual([os.path.basename(p)
                          for p in th.remote_rc_files(shell="zsh")],
                         [".zshenv", ".profile"])

    def _rc(self):
        rc = os.path.join(self.home, "rc-file with spaces")
        with open(rc, "w") as f:
            f.write(th.with_block("# user line\n"))
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        return rc

    def _source(self, shell, twice=False):
        rc = self._rc()
        env = {"PATH": "/usr/bin:/bin", "HOME": self.home}
        cmd = ". %s; . %s; printf '%%s\\n' \"$PATH\"" % (
            shlex.quote(rc), shlex.quote(rc)) if twice else \
            ". %s; printf '%%s\\n' \"$PATH\"" % shlex.quote(rc)
        p = subprocess.run([shell, "-c", cmd], env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def test_block_puts_local_bin_first_when_sourced_by_sh(self):
        local = os.path.join(self.home, ".local", "bin")
        pathv = self._source("sh")
        self.assertTrue(pathv.startswith(local + ":"), pathv)

    def test_block_puts_local_bin_first_when_sourced_by_bash(self):
        local = os.path.join(self.home, ".local", "bin")
        self.assertTrue(self._source("bash").startswith(local + ":"))

    def test_sourcing_twice_adds_no_duplicates(self):
        local = os.path.join(self.home, ".local", "bin")
        pathv = self._source("sh", twice=True).split(":")
        self.assertEqual(pathv.count(local), 1, pathv)

    @unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
    def test_block_puts_local_bin_first_when_sourced_by_zsh(self):
        local = os.path.join(self.home, ".local", "bin")
        self.assertTrue(self._source("zsh").startswith(local + ":"))

    def test_remote_path_missing_reports_each_transport(self):
        run = recorder(default=(0, "remote-agents\n", ""))
        missing = th.remote_path_missing(shell="/bin/bash", run=run)
        self.assertEqual(missing, {"ssh": ["remote-agents"],
                                   "mosh": ["remote-agents"]})
        self.assertEqual([c[:2] for c in run.calls],
                         [["/bin/bash", "-c"], ["/bin/sh", "-lc"]])
        for env in run.envs:
            self.assertEqual(env["PATH"], th.SSHD_PATH)
            self.assertEqual(env["HOME"], os.path.expanduser("~"))

    def test_remote_path_missing_all_names_on_timeout(self):
        run = recorder(default=(124, "", ""))
        missing = th.remote_path_missing(
            names=("remote-agents", "mosh-server"), shell="/bin/bash",
            run=run)
        self.assertEqual(missing["ssh"], ["remote-agents", "mosh-server"])
        self.assertEqual(missing["mosh"], ["remote-agents", "mosh-server"])

    def test_remote_path_missing_nothing_missing(self):
        run = recorder(default=(0, "", ""))
        self.assertEqual(th.remote_path_missing(shell="/bin/bash", run=run),
                         {})


# ---------------------------------------------------------------- CLI
class CliMain(unittest.TestCase):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(th.sys, "stdout", out), \
                mock.patch.object(th.sys, "stderr", err):
            rc = th.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_unknown_verbs_exit_two_with_usage(self):
        for argv in (["bogus"], ["service"], ["service", "status", "extra"],
                     ["remote-path"], ["remote-path", "bogus"]):
            rc, out, err = self.main(argv)
            self.assertEqual(rc, 2, argv)
            self.assertTrue(err.strip(), argv)

    def test_service_status_without_backend_exits_three(self):
        with mock.patch.object(th, "backend", return_value=None):
            rc, out, err = self.main(["service", "status"])
        self.assertEqual(rc, 3)
        self.assertIn("no service manager", out)

    def test_service_status_uses_the_backend(self):
        class FakeBackend(object):
            name = "launchd"

            def status(self):
                return "running"

        with mock.patch.object(th, "backend", return_value=FakeBackend()):
            rc, out, err = self.main(["service", "status"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "running (launchd)")


if __name__ == "__main__":
    unittest.main()
