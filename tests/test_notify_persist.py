"""Installer notification persistence: the ntfy topic survives reinstalls.

Regression for the topic-rotation bug: `./install` used to mint a fresh
random topic every time the ntfy question was answered yes, silently
invalidating every subscribed phone. The contract now is:

- an existing configuration (non-empty NTFY_TOPIC) is detected, shown,
  and preserved by default (ENTER = keep);
- notify.env is left byte-for-byte unchanged unless the user explicitly
  rotates (a separate, default-no question that warns about resubscribing);
- rotation carries over a custom NTFY_URL / NTFY_TOKEN, restarts a running
  watcher (the watcher reads notify.env once at startup), and keeps the
  file shape (3 lines, no duplicate keys);
- a malformed/partial file (no usable topic) falls back to the first-run
  flow instead of inventing a keep decision.

Runs the real `install` script under the FakeDarwin userland from
test_darwin_install (works on Linux CI and macOS runners alike).
"""
import os
import re
import sys
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:  # importable however the suite is invoked
    sys.path.insert(0, TESTS)
from test_darwin_install import FakeDarwin, INSTALL  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))  # noqa: E402

# stdin is a pipe, so the identity/roots block takes its discovered defaults
# and asks nothing; answers start at the PATH-block question, then the ntfy
# flow, watcher, keybindings
KEEP_ANSWERS = ["n", "", "n", "", ""]                     # ENTER = keep
KEEP_YES_ANSWERS = ["n", "Y", "n", "", ""]
DECLINE_BOTH = ["n", "n", "n", "n", "n", ""]             # no keep, no rotate
ROTATE_ANSWERS = ["n", "n", "y", "n", "n", ""]           # explicit rotation
FRESH_YES = ["n", "y", "n", "n", ""]                     # first-run flow
FRESH_NO = ["n", "n", "n", "n", ""]

OLD_TOPIC = "tendril-lPD1ADyhP1iXjUGs"  # the topic from the real bug report
NEW_TOPIC_RE = re.compile(r"^tendril-[A-Za-z0-9_-]{16}$")

notify_path = lambda self: os.path.join(  # noqa: E731
    self.home, ".config", "remote-agents", "notify.env")


def seed_notify(self, body, mode=0o600):
    path = notify_path(self)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, mode)
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


def load_installed_notify(home):
    """The herdr-notify the installer just put in ~/.local/bin, imported
    with HOME pointing at the fake home so ENV_FILE resolves to the fake
    notify.env. PATH is stripped so no real herdr is probed."""
    path = os.path.join(home, ".local", "bin", "herdr-notify")
    import importlib.util
    from importlib.machinery import SourceFileLoader
    with mock.patch.dict(os.environ, {"HOME": home, "PATH": "/usr/bin:/bin"}):
        loader = SourceFileLoader("installed_herdr_notify_under_test", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    return module


class ReinstallKeepsTopic(FakeDarwin):
    """git pull && ./install — the topic and the file survive untouched."""

    def test_keep_by_default_is_byte_for_byte(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        before = read(path)
        p = self.run_script(INSTALL, [], KEEP_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Existing ntfy configuration found:", out)
        self.assertIn(f"topic: {OLD_TOPIC}", out)
        self.assertIn("Keep existing notification configuration? [Y/n]", out)
        self.assertIn("kept - notify.env unchanged", out)
        self.assertEqual(read(path), before)          # byte-for-byte
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))),
                         ["config", "install.json", "notify.env"])
                         # install.json = upgrade provenance; no *.bak churn

    def test_update_sequence_two_installs_still_identical(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        before = read(path)
        for _ in range(2):  # first install already done; two more updates
            p = self.run_script(INSTALL, [], KEEP_ANSWERS)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(read(path), before)

    def test_explicit_Y_also_keeps(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        before = read(path)
        p = self.run_script(INSTALL, [], KEEP_YES_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(read(path), before)
        self.assertNotIn("Rotate notification topic?", out)

    def test_declining_both_leaves_the_file_in_place(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        before = read(path)
        p = self.run_script(INSTALL, [], DECLINE_BOTH)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Rotate notification topic? Existing subscribers "
                      "will need to resubscribe. [y/N]", out)
        self.assertIn("skipped - existing notification configuration "
                      "left in place", out)
        self.assertEqual(read(path), before)

    def test_watcher_loads_the_preserved_config(self):
        seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                          f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=tok_preserved\n")
        p = self.run_script(INSTALL, [], KEEP_ANSWERS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cfg = load_installed_notify(self.home).load_env()
        self.assertEqual(cfg, {"NTFY_URL": "https://ntfy.sh",
                               "NTFY_TOPIC": OLD_TOPIC,
                               "NTFY_TOKEN": "tok_preserved"})


class ExplicitRotation(FakeDarwin):
    def test_rotation_mints_topic_and_preserves_url_token(self):
        path = seed_notify(self, 'NTFY_URL="https://ntfy.selfhosted.example"\n'
                                 f"NTFY_TOPIC={OLD_TOPIC}\n"
                                 "NTFY_TOKEN=tok_secret\n")
        p = self.run_script(INSTALL, [], ROTATE_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Rotate notification topic? Existing subscribers "
                      "will need to resubscribe. [y/N]", out)
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)               # fixed shape, no dups
        self.assertEqual(sum(1 for ln in lines
                             if ln.startswith("NTFY_TOPIC=")), 1)
        body = "\n".join(lines)
        new_topic = dict(ln.split("=", 1) for ln in lines)["NTFY_TOPIC"]
        self.assertRegex(new_topic, NEW_TOPIC_RE)
        self.assertNotEqual(new_topic, OLD_TOPIC)
        self.assertNotIn(OLD_TOPIC, body)
        self.assertIn("NTFY_URL=https://ntfy.selfhosted.example", body)
        self.assertIn("NTFY_TOKEN=tok_secret", body)  # carried over, not reset
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertIn(f"Your private topic name:  {new_topic}", out)
        backups = [n for n in os.listdir(os.path.dirname(path))
                   if n.startswith("notify.env.bak.")]
        self.assertEqual(len(backups), 1)
        self.assertIn(OLD_TOPIC, read(os.path.join(
            os.path.dirname(path), backups[0])).decode())

    def test_rotation_restarts_a_running_watcher(self):
        seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                          f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        agents = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(agents)
        plist = os.path.join(agents, "com.tendril.herdr-notify.plist")
        with open(plist, "w") as f:
            f.write("<plist/>\n")
        # launchctl print answers "running", so service status says running
        self.stub("launchctl",
                  'printf \'%s\\n\' "$*" >> "$TENDRIL_LAUNCHCTL_LOG"\n'
                  '[ "$1" = "print" ] && printf \'\\tstate = running\\n\'\n'
                  "exit 0\n")
        p = self.run_script(INSTALL, [], ROTATE_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("watcher restarted (now pushing to the new topic)", out)
        self.assertTrue(any("kickstart -k" in c and "herdr-notify" in c
                            for c in self.launchctl_calls()),
                        self.launchctl_calls())

    def test_keep_never_touches_a_running_watcher(self):
        seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                          f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        agents = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(agents)
        with open(os.path.join(agents,
                               "com.tendril.herdr-notify.plist"), "w") as f:
            f.write("<plist/>\n")
        self.stub("launchctl",
                  'printf \'%s\\n\' "$*" >> "$TENDRIL_LAUNCHCTL_LOG"\n'
                  '[ "$1" = "print" ] && printf \'\\tstate = running\\n\'\n'
                  "exit 0\n")
        p = self.run_script(INSTALL, [], KEEP_ANSWERS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # status probes are fine; a keep decision must not restart anything
        self.assertEqual([c for c in self.launchctl_calls()
                          if "kickstart" in c], [])

    def test_watcher_loads_the_rotated_config(self):
        seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                          f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        p = self.run_script(INSTALL, [], ROTATE_ANSWERS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cfg = load_installed_notify(self.home).load_env()
        self.assertRegex(cfg["NTFY_TOPIC"], NEW_TOPIC_RE)
        self.assertNotEqual(cfg["NTFY_TOPIC"], OLD_TOPIC)
        self.assertEqual(cfg["NTFY_URL"], "https://ntfy.sh")
        self.assertEqual(cfg["NTFY_TOKEN"], "")


class FirstInstall(FakeDarwin):
    def test_fresh_install_still_writes_topic_and_instructions(self):
        p = self.run_script(INSTALL, [], FRESH_YES)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("Existing ntfy configuration found", out)
        self.assertIn("Configure ntfy push now? [y/N]", out)
        path = notify_path(self)
        with open(path) as f:
            lines = dict(ln.split("=", 1) for ln in f.read().splitlines())
        self.assertRegex(lines["NTFY_TOPIC"], NEW_TOPIC_RE)
        self.assertEqual(lines["NTFY_URL"], "https://ntfy.sh")
        self.assertEqual(lines["NTFY_TOKEN"], "")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertIn(f"Your private topic name:  {lines['NTFY_TOPIC']}", out)
        conf_dir = os.path.dirname(path)
        self.assertEqual([n for n in os.listdir(conf_dir)
                          if n.startswith("notify.env")], ["notify.env"])

    def test_declined_fresh_install_writes_nothing(self):
        p = self.run_script(INSTALL, [], FRESH_NO)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertFalse(os.path.exists(notify_path(self)))
        self.assertIn("skipped - create", out)


class MalformedExistingConfig(FakeDarwin):
    """No usable NTFY_TOPIC -> the first-run flow, never a keep decision."""

    def test_empty_topic_gets_fresh_setup_and_keeps_url_token(self):
        path = seed_notify(self, 'NTFY_URL="https://ntfy.selfhosted.example"\n'
                                 "NTFY_TOPIC=\nNTFY_TOKEN=tok_keep\n"
                                 "# leftover comment\n")
        p = self.run_script(INSTALL, [], FRESH_YES)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("Existing ntfy configuration found", out)
        self.assertIn("Configure ntfy push now? [y/N]", out)
        with open(path) as f:
            lines = dict(ln.split("=", 1) for ln in f.read().splitlines())
        self.assertRegex(lines["NTFY_TOPIC"], NEW_TOPIC_RE)
        self.assertEqual(lines["NTFY_URL"], "https://ntfy.selfhosted.example")
        self.assertEqual(lines["NTFY_TOKEN"], "tok_keep")

    def test_missing_topic_and_junk_declined_untouched(self):
        path = seed_notify(self, "garbage\nNTFY_TOKEN=whatever\n", 0o644)
        before = read(path)
        p = self.run_script(INSTALL, [], FRESH_NO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(read(path), before)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)  # not touched

    def test_quoted_topic_is_recognized_and_preserved(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC=\"{OLD_TOPIC}\"\nNTFY_TOKEN=\n")
        before = read(path)
        p = self.run_script(INSTALL, [], KEEP_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(f"topic: {OLD_TOPIC}", out)     # unquoted for display
        self.assertEqual(read(path), before)          # and file untouched


class InjectionAttempts(FakeDarwin):
    """A crafted notify.env must never gain execution; the hostile topic is
    either displayed verbatim (keep) or discarded (rotation)."""

    HOSTILE = "tendril-$(touch pwned)`touch pwned2`'; rm -rf $HOME; '"

    def _assert_no_execution(self):
        for root in (self.tmp, os.getcwd()):
            self.assertFalse(os.path.exists(os.path.join(root, "pwned")))
            self.assertFalse(os.path.exists(os.path.join(root, "pwned2")))

    def test_hostile_topic_kept_is_only_displayed(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={self.HOSTILE}\nNTFY_TOKEN=\n")
        before = read(path)
        p = self.run_script(INSTALL, [], KEEP_ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(f"topic: {self.HOSTILE}", out)
        self.assertEqual(read(path), before)
        self._assert_no_execution()

    def test_hostile_topic_discarded_on_rotation(self):
        seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                          f"NTFY_TOPIC={self.HOSTILE}\nNTFY_TOKEN=\n")
        p = self.run_script(INSTALL, [], ROTATE_ANSWERS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(notify_path(self)) as f:
            body = f.read()
        self.assertRegex(
            dict(ln.split("=", 1) for ln in body.splitlines())["NTFY_TOPIC"],
            NEW_TOPIC_RE)
        self.assertNotIn("rm -rf", body)
        self._assert_no_execution()


class DryRunKeepsWorking(FakeDarwin):
    # dry-run skips the PATH question: keep(""), watcher(""), detach(""),
    # switching("") — the identity block asks nothing on a pipe
    ANSWERS = ["", "", "", ""]

    def test_dry_run_with_existing_config_writes_nothing(self):
        path = seed_notify(self, f"NTFY_URL=https://ntfy.sh\n"
                                 f"NTFY_TOPIC={OLD_TOPIC}\nNTFY_TOKEN=\n")
        before = read(path)
        walk_before = self.walk_home()
        p = self.run_script(INSTALL, ["--dry-run"], self.ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(f"topic: {OLD_TOPIC}", out)
        self.assertIn("kept - notify.env unchanged", out)
        self.assertEqual(self.walk_home(), walk_before)  # dry-run adds nothing
        self.assertEqual(read(path), before)
