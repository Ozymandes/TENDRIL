"""The ONE runtime manifest (LINK-CONTRACT-0.2 §7.4).

RC1 shipped `tendril capacity` while install's hardcoded copy list never
mentioned tendril_capacity.py — every host verb then failed with
"tendril_capacity.py is missing next to remote-agents". These tests pin
the root fix from both sides:

(a) manifest completeness — scan bin/ for every module the entrypoints
    import or reference (`import tendril_*`, SourceFileLoader helpers,
    herdr-bindings.py, herdr-notify) and assert each is in
    bin/runtime-files.txt (and exists);
(b) the REAL installer — `sh install --upgrade` into a throwaway HOME
    (stubs for systemctl/launchctl/herdr on PATH, never the live host)
    must land every manifest file next to remote-agents, executable and
    byte-identical, record HERDR_BIN/HERDR_VERSION in the config, write
    the services' service.env, and daemon-reload + enable --now both
    timers. Uninstall removes exactly the manifest again.

Hermetic: every write goes under a temp HOME; the repo is only read.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(REPO, "bin")
INSTALL = os.path.join(REPO, "install")
UNINSTALL = os.path.join(REPO, "uninstall")
MANIFEST = os.path.join(BIN_DIR, "runtime-files.txt")


def manifest_files():
    out = []
    with open(MANIFEST, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
    return out


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def entrypoint_text():
    """Everything the two standalone entrypoints import or reference."""
    chunks = []
    for name in ("remote-agents", "herdr-notify"):
        with open(os.path.join(BIN_DIR, name), encoding="utf-8") as f:
            chunks.append(f.read())
    return "\n".join(chunks)


def referenced_modules():
    """Module FILES the entrypoints pull in: `import tendril_x`,
    `from tendril_x import`, and the SourceFileLoader helpers (loader
    name → on-disk file: herdr_bindings → herdr-bindings.py,
    tendril_notify_mod → herdr-notify)."""
    text = entrypoint_text()
    found = set()
    for m in re.finditer(r"\b(?:import|from)\s+(tendril_[a-z_]+)\b", text):
        found.add(m.group(1) + ".py")
    for m in re.finditer(r'SourceFileLoader\(\s*"([A-Za-z0-9_]+)"', text):
        found.add({"herdr_bindings": "herdr-bindings.py",
                   "tendril_notify_mod": "herdr-notify",
                   "tendril_herdr": "tendril_herdr.py",
                   }.get(m.group(1), m.group(1) + ".py"))
    for literal in ("herdr-notify", "herdr-bindings.py"):
        if literal in text:
            found.add(literal)
    return found


class ManifestCompletenessTests(unittest.TestCase):
    def test_manifest_entries_exist_in_bin(self):
        for name in manifest_files():
            self.assertTrue(os.path.isfile(os.path.join(BIN_DIR, name)),
                            "manifest lists a file bin/ does not have: "
                            + name)

    def test_every_referenced_module_is_in_the_manifest(self):
        refs = referenced_modules()
        self.assertTrue(refs, "scan found nothing — the scan itself broke")
        listed = set(manifest_files())
        for name in sorted(refs):
            self.assertIn(name, listed,
                          "bin entrypoint uses %r but the runtime manifest "
                          "does not list it (it would never install)" % name)
            self.assertTrue(os.path.isfile(os.path.join(BIN_DIR, name)),
                            name)

    def test_known_rc1_gap_is_covered(self):
        # the regression that motivated the manifest
        self.assertIn("tendril_capacity.py", manifest_files())
        self.assertIn("tendril_health.py", manifest_files())
        self.assertIn("tendril_schedule.py", manifest_files())
        self.assertIn("tendril_herdr.py", manifest_files())


HERDR_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TENDRIL_HERDR_LOG"
case "$1 $2" in
  "--version ") echo "herdr 0.9.3"; exit 0 ;;
esac
case "$1" in
  --version) echo "herdr 0.9.3"; exit 0 ;;
  status)
      [ "$2" = "server" ] || { echo "status: running"; \\
        echo "endpoint_compatible: yes"; exit 0; }
      echo '{"status":"running","running":true,"version":"0.9.3",'
      echo '"compatible":true,"endpoint_compatible":true}' ;;
  *) exit 0 ;;
esac
"""

SYSTEMCTL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TENDRIL_SYSTEMCTL_LOG"
case "$1 $2" in
    "--user is-active") echo active ;;
    "--user is-system-running") echo running ;;
esac
exit 0
"""


class InstallCopyTests(unittest.TestCase):
    """The real `sh install --upgrade` into a throwaway HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril runtime-files ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.bindir = os.path.join(self.tmp, "stub-bin")
        os.makedirs(self.bindir)
        for name, body in (("herdr", HERDR_STUB),
                           ("systemctl", SYSTEMCTL_STUB)):
            with open(os.path.join(self.bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.bindir, name), 0o755)
        self.herdr_log = os.path.join(self.tmp, "herdr.log")
        self.systemctl_log = os.path.join(self.tmp, "systemctl.log")
        self.launchctl_log = os.path.join(self.tmp, "launchctl.log")
        self.env = dict(os.environ)
        self.env.update({
            "HOME": self.home,
            "PATH": self.bindir + os.pathsep + self.env.get("PATH", ""),
            "TENDRIL_HERDR_LOG": self.herdr_log,
            "TENDRIL_SYSTEMCTL_LOG": self.systemctl_log,
            "TENDRIL_LAUNCHCTL_LOG": self.launchctl_log,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                  "HERDR_BIN", "TENDRIL_ALIAS", "AGENT_ALIAS"):
            self.env.pop(k, None)

    def conf(self, name):
        return os.path.join(self.home, ".config", "remote-agents", name)

    def installed(self, name):
        return os.path.join(self.home, ".local", "bin", name)

    def run_install(self, *argv):
        return subprocess.run(["sh", INSTALL, *argv], env=self.env,
                              capture_output=True, text=True, timeout=240,
                              stdin=subprocess.DEVNULL, cwd=self.tmp)

    def log(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_upgrade_lands_every_runtime_file_next_to_remote_agents(self):
        p = self.run_install("--upgrade")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in manifest_files():
            dst = self.installed(name)
            self.assertTrue(os.path.isfile(dst),
                            "install --upgrade did not install %s" % name)
            self.assertTrue(os.access(dst, os.X_OK), "%s not executable" % name)
            self.assertTrue(os.access(dst, os.R_OK), "%s not readable" % name)
            self.assertEqual(sha(dst), sha(os.path.join(BIN_DIR, name)),
                             "%s differs from the checkout copy" % name)
        self.assertEqual(os.readlink(self.installed("tendril")),
                         "remote-agents")

    def test_upgrade_records_herdr_and_writes_service_env(self):
        p = self.run_install("--upgrade")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(self.conf("config")) as f:
            cfg = dict(ln.strip().split("=", 1) for ln in f
                       if "=" in ln and not ln.startswith("#"))
        self.assertTrue(cfg.get("HERDR_BIN"), "HERDR_BIN not recorded")
        self.assertTrue(cfg.get("HERDR_VERSION"), "HERDR_VERSION not recorded")
        self.assertTrue(cfg.get("HERDR_REALPATH"),
                        "HERDR_REALPATH provenance not recorded")
        self.assertTrue(os.path.isfile(cfg["HERDR_BIN"]),
                        "recorded HERDR_BIN is not a real file")
        self.assertTrue(os.path.isabs(cfg["HERDR_BIN"]),
                        "HERDR_BIN must be absolute (§7.4)")
        # the recorded entry is the stable PATH entry, never its target
        self.assertEqual(os.path.realpath(cfg["HERDR_BIN"]),
                         cfg["HERDR_REALPATH"])
        self.assertNotIn("shims", cfg["HERDR_BIN"],
                         "a version-manager shim must never be recorded")
        # service.env: the services' non-interactive environment
        with open(self.conf("service.env")) as f:
            svc = dict(ln.strip().split("=", 1) for ln in f
                       if "=" in ln and not ln.startswith("#"))
        self.assertEqual(svc.get("HERDR_BIN"), cfg["HERDR_BIN"])
        self.assertEqual(svc.get("HERDR_VERSION"), cfg["HERDR_VERSION"])
        svc_path = (svc.get("PATH") or ":").split(":")
        self.assertIn(os.path.dirname(cfg["HERDR_BIN"]), svc_path,
                      "service.env PATH misses the herdr dir")
        self.assertIn("/usr/bin", svc_path,
                      "service.env PATH must keep the system dirs")
        mode = os.stat(self.conf("service.env")).st_mode & 0o777
        self.assertEqual(mode, 0o600, "service.env must be 0600")

    def test_upgrade_ensures_both_timers_and_reports_state(self):
        p = self.run_install("--upgrade")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.log(self.systemctl_log)
        self.assertIn("--user daemon-reload", calls)
        self.assertIn("--user enable --now tendril-schedule.timer", calls)
        self.assertIn("--user enable --now tendril-guardian.timer", calls)
        # state reported for both timers (the stub answers "active")
        self.assertIn("scheduler: timer is active", p.stdout)
        self.assertIn("guardian: timer is active", p.stdout)

    def test_units_reference_service_env(self):
        for unit in ("tendril-schedule.service", "tendril-guardian.service",
                     "herdr-notify.service"):
            with open(os.path.join(REPO, "systemd", unit)) as f:
                body = f.read()
            self.assertIn("EnvironmentFile=-%h/.config/remote-agents/"
                          "service.env", body, unit)
            if unit != "herdr-notify.service":
                self.assertIn("NoNewPrivileges=true", body, unit)
                self.assertIn("PrivateTmp=true", body, unit)

    def test_fresh_install_writes_the_recorded_config(self):
        p = self.run_install("--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(self.conf("config")) as f:
            body = f.read()
        self.assertIn("TENDRIL_HOST=", body)
        herdr_bin = [ln for ln in body.splitlines()
                     if ln.startswith("HERDR_BIN=")]
        self.assertEqual(len(herdr_bin), 1)
        entry = herdr_bin[0].split("=", 1)[1]
        self.assertTrue(entry.startswith("HERDR_BIN=/") or
                        entry.startswith("/"),
                        "fresh install must record an absolute herdr path")
        self.assertIn("HERDR_VERSION=0.9.3", body)
        self.assertIn("HERDR_REALPATH=", body)
        self.assertNotIn("shims", entry,
                         "a version-manager shim must never be recorded")

    def test_uninstall_removes_every_runtime_file(self):
        self.run_install("--upgrade")
        for name in manifest_files():
            self.assertTrue(os.path.isfile(self.installed(name)), name)
        p = subprocess.run(["sh", UNINSTALL], env=self.env,
                           capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in manifest_files():
            self.assertFalse(os.path.isfile(self.installed(name)),
                             "uninstall left %s behind" % name)

    def test_upgrade_dry_run_writes_nothing(self):
        p = self.run_install("--upgrade", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in manifest_files():
            self.assertFalse(os.path.isfile(self.installed(name)),
                             "dry-run wrote %s" % name)
        self.assertFalse(os.path.isfile(self.conf("config")))
        self.assertFalse(os.path.isfile(self.conf("service.env")))


if __name__ == "__main__":
    unittest.main()
