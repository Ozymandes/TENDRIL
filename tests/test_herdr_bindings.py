"""Installer tests for safe Herdr detach binding migration."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(REPO, "bin", "herdr-bindings.py")
INSTALL = os.path.join(REPO, "install")

loader = SourceFileLoader("herdr_bindings", HELPER)
spec = importlib.util.spec_from_loader(loader.name, loader)
bindings = importlib.util.module_from_spec(spec)
loader.exec_module(bindings)


def read_bytes(path):
    with open(path, "rb") as file:
        return file.read()


def read_text(path):
    with open(path, encoding="utf-8") as file:
        return file.read()


class DetachMerge(unittest.TestCase):
    def test_fresh_keys_section_gets_supported_bindings_without_touching_others(self):
        source = '[theme]\nname = "terminal"\n\n[keys]\nhelp = "prefix+?"\n'
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        updated = bindings.apply_detach(source, plan)
        self.assertEqual(plan["added"], ["prefix+q", "prefix+d", "alt+d", "ctrl+home"])
        self.assertEqual(plan["removed"], [])
        self.assertIn('detach = ["prefix+q", "prefix+d", "alt+d", "ctrl+home"]', updated)
        self.assertIn('help = "prefix+?"', updated)
        self.assertIn('name = "terminal"', updated)

    def test_apply_inserts_into_keys_header_with_trailing_comment(self):
        source = '[keys] # user key bindings\nnew_tab = "prefix+c"\n'
        plan = bindings.plan_detach(source, ctrl_home_supported=False)
        updated = bindings.apply_detach(source, plan)
        self.assertEqual(updated.count("[keys]"), 1)
        self.assertIn('[keys] # user key bindings\ndetach = [', updated)
        self.assertIn('new_tab = "prefix+c"', updated)

    def test_absent_config_creates_minimal_private_keys_file_on_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "herdr", "config.toml")
            plan = subprocess.run([sys.executable, HELPER, "plan", path, "0"],
                                  check=True, capture_output=True, text=True)
            self.assertIn('ADDED=prefix+q,prefix+d,alt+d', plan.stdout)
            self.assertFalse(os.path.exists(path))
            subprocess.run([sys.executable, HELPER, "apply", path, "0"], check=True)
            with open(path, encoding="utf-8") as config:
                contents = config.read()
            self.assertEqual(contents, '[keys]\ndetach = ["prefix+q", "prefix+d", "alt+d"]\n')
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_implicit_default_conflict_does_not_block_other_detach_fallbacks(self):
        source = '[keys]\nnew_tab = "prefix+q"\n'
        plan = bindings.plan_detach(source, ctrl_home_supported=False)
        updated = bindings.apply_detach(source, plan)
        self.assertEqual(plan["conflicts"]["prefix+q"], ["keys.new_tab"])
        self.assertEqual(plan["added"], ["prefix+d", "alt+d"])
        self.assertIn('new_tab = "prefix+q"', updated)
        self.assertNotIn('"prefix+q"', plan["line"])

    def test_existing_managed_legacy_ctrl_bracket_binding_migrates(self):
        source = ('[keys]\n'
                  'detach = ["prefix+q", "prefix+d", "alt+d", "ctrl+]"]\n'
                  'new_tab = "prefix+c"\n')
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        updated = bindings.apply_detach(source, plan)
        self.assertEqual(plan["added"], ["ctrl+home"])
        self.assertEqual(plan["removed"], ["ctrl+]"])
        self.assertIn('detach = ["prefix+q", "prefix+d", "alt+d", "ctrl+home"]', updated)
        self.assertIn('new_tab = "prefix+c"', updated)
        self.assertNotIn('"ctrl+]"', updated)

    def test_home_conflict_preserves_legacy_and_adds_unconflicted_fallbacks(self):
        source = ('[keys]\n'
                  'detach = ["prefix+q", "ctrl+]"]\n'
                  'new_tab = "ctrl+home"\n')
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        updated = bindings.apply_detach(source, plan)
        self.assertIn("ctrl+home", plan["conflicts"])
        self.assertEqual(plan["added"], ["prefix+d", "alt+d"])
        self.assertEqual(plan["removed"], [])
        self.assertIn('"ctrl+]"', updated)
        self.assertIn('new_tab = "ctrl+home"', updated)

    def test_conflicting_fallback_chords_are_not_claimed(self):
        source = ('[keys]\n'
                  'detach = ["prefix+q"]\n'
                  'new_tab = "prefix+d"\n'
                  'next_tab = "alt+d"\n')
        plan = bindings.plan_detach(source, ctrl_home_supported=False)
        self.assertEqual(plan["added"], [])
        self.assertEqual(plan["removed"], [])
        self.assertIn("prefix+d", plan["conflicts"])
        self.assertIn("alt+d", plan["conflicts"])
        self.assertEqual(bindings.apply_detach(source, plan), source)

    def test_unsupported_client_does_not_add_ctrl_home_or_remove_legacy(self):
        fresh = '[keys]\n'
        plan = bindings.plan_detach(fresh, ctrl_home_supported=False)
        updated = bindings.apply_detach(fresh, plan)
        self.assertEqual(plan["added"], ["prefix+q", "prefix+d", "alt+d"])
        self.assertNotIn("ctrl+home", updated)

        legacy = '[keys]\ndetach = ["prefix+q", "ctrl+]"]\n'
        plan = bindings.plan_detach(legacy, ctrl_home_supported=False)
        updated = bindings.apply_detach(legacy, plan)
        self.assertEqual(plan["removed"], [])
        self.assertIn('"ctrl+]"', updated)

    def test_comment_text_does_not_create_a_false_conflict(self):
        source = '[keys]\nnew_tab = "prefix+c" # ctrl+home is mentioned here\n'
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        self.assertNotIn("ctrl+home", plan["conflicts"])
        self.assertIn("ctrl+home", plan["added"])

    def test_multiline_chord_list_still_protects_its_conflicting_key(self):
        source = ('[keys]\n'
                  'detach = ["prefix+q"]\n'
                  'new_tab = [\n'
                  '  "ctrl+home",\n'
                  ']\n')
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        self.assertIn("ctrl+home", plan["conflicts"])
        self.assertNotIn("ctrl+home", plan["added"])

    def test_multiline_existing_detach_array_is_migrated_as_one_assignment(self):
        source = ('[keys]\n'
                  'detach = [\n'
                  '  "prefix+q",\n'
                  '  "ctrl+]",\n'
                  ']\n'
                  'new_tab = "prefix+c"\n')
        plan = bindings.plan_detach(source, ctrl_home_supported=True)
        updated = bindings.apply_detach(source, plan)
        self.assertEqual(plan["removed"], ["ctrl+]"])
        self.assertIn('detach = ["prefix+q", "prefix+d", "alt+d", "ctrl+home"]', updated)
        self.assertIn('new_tab = "prefix+c"', updated)
        self.assertNotIn('"ctrl+]"', updated)
        self.assertNotIn("]]", updated)


class HerdrCapabilityProbe(unittest.TestCase):
    def test_probe_uses_a_temporary_config_and_does_not_touch_live_config(self):
        live_path = os.path.expanduser("~/.config/herdr/config.toml")
        before = read_bytes(live_path) if os.path.isfile(live_path) else None
        observed = {}

        def config_check(args, env, **kwargs):
            observed["args"] = args
            observed["path"] = env["HERDR_CONFIG_PATH"]
            observed["contents"] = read_text(env["HERDR_CONFIG_PATH"])
            return subprocess.CompletedProcess(args, 0, "config: ok\n", "")

        with mock.patch.object(bindings.subprocess, "run", side_effect=config_check):
            self.assertTrue(bindings.herdr_supports_chord("/usr/bin/herdr", "ctrl+home"))
        self.assertEqual(observed["args"], ["/usr/bin/herdr", "config", "check"])
        self.assertIn('"ctrl+home"', observed["contents"])
        self.assertFalse(os.path.exists(observed["path"]))
        if before is not None:
            self.assertEqual(read_bytes(live_path), before)

    @unittest.skipUnless(shutil.which("herdr"), "installed Herdr CLI unavailable")
    def test_installed_cli_can_be_probed_without_writing_its_live_config(self):
        live_path = os.path.expanduser("~/.config/herdr/config.toml")
        before = read_bytes(live_path) if os.path.isfile(live_path) else None
        result = bindings.herdr_supports_chord(shutil.which("herdr"), "ctrl+home")
        self.assertIsInstance(result, bool)
        if before is not None:
            self.assertEqual(read_bytes(live_path), before)

    @unittest.skipUnless(shutil.which("herdr"), "installed Herdr CLI unavailable")
    def test_merged_config_with_implicit_default_conflict_passes_cli_check(self):
        source = '[keys] # existing key bindings\nnew_tab = "prefix+q"\n'
        plan = bindings.plan_detach(source, ctrl_home_supported=False)
        updated = bindings.apply_detach(source, plan)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.toml")
            with open(path, "w", encoding="utf-8") as config:
                config.write(updated)
            env = dict(os.environ, HERDR_CONFIG_PATH=path)
            result = subprocess.run([shutil.which("herdr"), "config", "check"],
                                    env=env, capture_output=True, text=True,
                                    timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installer_calls_capability_probe_and_managed_merge_helper(self):
        source = read_text(INSTALL)
        self.assertIn('supports "$HERDR_BIN" ctrl+home', source)
        self.assertIn('"$DETACH_HELPER" "$1" "$HERDR_CONF" "$CTRL_HOME_SUPPORTED"', source)
        self.assertIn('no $HERDR_CONF yet; a minimal config will be proposed', source)
        self.assertIn('REMOVED=$(printf', source)

    def test_installer_does_not_provision_previous_workspace_alt_up(self):
        # the switching proposals now live in the helper; Alt+Up stays Pi's
        source = read_text(INSTALL)
        self.assertIn('"$DETACH_HELPER" switch "$1" "$HERDR_CONF" "$HERDR_BIN"', source)
        actions = [p[0] + "." + p[1] for p in bindings.SWITCH_PROPOSALS]
        self.assertNotIn("keys.previous_workspace", actions)
        for proposal in bindings.SWITCH_PROPOSALS:
            self.assertNotIn("alt+up", proposal[2])
        merged, _ = bindings.plan_switch("")
        self.assertNotIn("alt+up", merged)
        self.assertNotIn("previous_workspace", merged)


FIXTURE = os.path.join(REPO, "tests", "fixtures", "herdr_switch_tab_claims_digits.toml")


def verdicts(results):
    return {action: (verdict, reason) for action, _, verdict, reason in results}


class SwitchMerge(unittest.TestCase):
    """Workspace/tab switching proposals, decided one by one. The fixture
    is the real config that exposed the bug: switch_tab already claims
    prefix+1..9 and alt+1..9."""

    def setUp(self):
        self.text = read_text(FIXTURE)

    def test_user_config_skips_only_the_conflicting_proposals(self):
        merged, results = bindings.plan_switch(self.text)
        v = verdicts(results)
        self.assertEqual(v["keys.switch_workspace"],
                         ("SKIP", "conflict with keys.switch_tab"))
        self.assertEqual(v["keys.indexed.workspaces"],
                         ("SKIP", "conflict with keys.switch_tab"))
        self.assertEqual(v["keys.indexed.tabs"], ("ADD", ""))
        # every existing byte survives; the one addition is appended
        self.assertTrue(merged.startswith(self.text))
        self.assertEqual(merged[len(self.text):], '\n[keys.indexed]\ntabs = "ctrl"\n')

    def test_apply_is_idempotent_and_reports_the_truth(self):
        tmp = tempfile.mkdtemp(prefix="tendril herdr ")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.toml")
        shutil.copy(FIXTURE, path)
        os.chmod(path, 0o640)
        out = subprocess.run([sys.executable, HELPER, "switch", "apply", path],
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn("ADD=keys.indexed.tabs|", out)
        self.assertIn("DIGITS=Ctrl+B 1..9|switch tab", out)   # not workspaces
        self.assertIn("DIGITS=Alt+1..9|switch tab", out)
        self.assertIn("DIGITS=Ctrl+1..9|switch tab", out)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        once = read_bytes(path)
        out2 = subprocess.run([sys.executable, HELPER, "switch", "apply", path],
                              capture_output=True, text=True, timeout=30).stdout
        self.assertNotIn("WROTE=", out2)
        self.assertNotIn("ADD=", out2)
        self.assertEqual(read_bytes(path), once)

    def test_fresh_config_gets_every_proposal(self):
        merged, results = bindings.plan_switch("")
        self.assertTrue(all(v == "ADD" for _, _, v, _ in results), results)
        self.assertEqual(dict(bindings.describe_digits(merged)),
                         {"Ctrl+B 1..9": "switch workspace",
                          "Alt+1..9": "switch workspace",
                          "Ctrl+1..9": "switch tab"})

    def test_a_single_claimed_digit_blocks_the_whole_range(self):
        text = '[keys]\nswitch_tab = "alt+3"\nnext_tab = "ctrl+5"\n'
        v = verdicts(bindings.plan_switch(text)[1])
        self.assertEqual(v["keys.indexed.workspaces"][0], "SKIP")
        self.assertIn("keys.switch_tab", v["keys.indexed.workspaces"][1])
        self.assertEqual(v["keys.indexed.tabs"][0], "SKIP")
        self.assertIn("keys.next_tab", v["keys.indexed.tabs"][1])
        self.assertEqual(v["keys.switch_workspace"], ("ADD", ""))

    def test_no_substring_matching_between_chords(self):
        # alt+d (detach) must not block alt+down; ctrl+1x is not ctrl+1
        text = '[keys]\ndetach = ["alt+d"]\nsplit = "ctrl+10"\n'
        v = verdicts(bindings.plan_switch(text)[1])
        self.assertEqual(v["keys.next_workspace"], ("ADD", ""))
        self.assertEqual(v["keys.indexed.tabs"], ("ADD", ""))
        text = '[keys]\nzoom = "alt+down"\n'
        v = verdicts(bindings.plan_switch(text)[1])
        self.assertEqual(v["keys.next_workspace"], ("SKIP", "conflict with keys.zoom"))

    def test_modifier_order_and_indexed_modifiers_are_normalised(self):
        text = '[keys.indexed]\nagents = "alt"\n'
        v = verdicts(bindings.plan_switch(text)[1])
        self.assertEqual(v["keys.indexed.workspaces"][0], "SKIP")
        self.assertEqual(bindings._norm("shift+ctrl+X"), "ctrl+shift+x")

    def test_commented_chords_are_not_claims(self):
        text = '[keys]\n# switch_tab = ["alt+1..9"]\n'
        v = verdicts(bindings.plan_switch(text)[1])
        self.assertEqual(v["keys.indexed.workspaces"], ("ADD", ""))

    def test_user_scalar_wins_and_lists_are_merged_not_replaced(self):
        text = '[keys]\nswitch_workspace = "prefix+shift+1..9"\nnext_tab = ["prefix+n"]\n'
        merged, results = bindings.plan_switch(text)
        v = verdicts(results)
        self.assertEqual(v["keys.switch_workspace"][0], "SKIP")
        self.assertIn("already set by you", v["keys.switch_workspace"][1])
        self.assertIn('switch_workspace = "prefix+shift+1..9"', merged)
        self.assertIn('next_tab = ["prefix+n", "alt+right"]', merged)

    def fake_herdr(self, body):
        tmp = tempfile.mkdtemp(prefix="tendril herdr ")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "herdr")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_cli_rejection_skips_only_that_proposal(self):
        # this Herdr refuses ctrl-indexed tabs; a pre-existing warning
        # (unknown key) must not count against any proposal
        herdr = self.fake_herdr(
            'echo "unknown config key keys.foo; ignoring key"\n'
            'if grep -q \'tabs = "ctrl"\' "$HERDR_CONFIG_PATH"; then\n'
            '  echo "ctrl+1: kept keys.x, disabled keys.indexed.tabs"; exit 1; fi\n'
            'exit 1\n')
        merged, results = bindings.plan_switch('[keys]\nfoo = "x"\n', herdr)
        v = verdicts(results)
        self.assertEqual(v["keys.indexed.tabs"],
                         ("SKIP", "rejected by herdr config check: "
                                  "ctrl+1: kept keys.x, disabled keys.indexed.tabs"))
        self.assertEqual(v["keys.switch_workspace"], ("ADD", ""))
        self.assertNotIn('tabs = "ctrl"', merged)

    def test_apply_refuses_and_leaves_bytes_when_the_whole_file_fails(self):
        # last-line gate: if the merged file still adds a Herdr issue, the
        # live config is not written at all
        herdr = self.fake_herdr(
            'grep -q "BROKEN" "$HERDR_CONFIG_PATH" && { echo "bad"; exit 1; }\nexit 0\n')
        tmp = tempfile.mkdtemp(prefix="tendril herdr ")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.toml")
        shutil.copy(FIXTURE, path)
        before = read_bytes(path)
        broken = (self.text + "BROKEN\n",
                  [("keys.indexed.tabs", "Ctrl indexed tabs", "ADD", "")])
        out = io.StringIO()
        with mock.patch.object(bindings, "plan_switch", return_value=broken), \
                contextlib.redirect_stdout(out):
            rc = bindings.switch_main("apply", path, herdr)
        self.assertEqual(rc, 3)
        self.assertIn("REFUSED=bad", out.getvalue())
        self.assertEqual(read_bytes(path), before)

    @unittest.skipUnless(shutil.which("herdr"), "no herdr CLI here")
    def test_real_herdr_accepts_the_merged_user_config(self):
        merged, results = bindings.plan_switch(self.text, shutil.which("herdr"))
        self.assertEqual(verdicts(results)["keys.indexed.tabs"], ("ADD", ""))
        rc, issues = bindings.herdr_issues(shutil.which("herdr"), merged)
        self.assertEqual((rc, issues), (0, set()))
        fresh, _ = bindings.plan_switch("", shutil.which("herdr"))
        self.assertEqual(bindings.herdr_issues(shutil.which("herdr"), fresh), (0, set()))



class HelpTellsTheTruth(unittest.TestCase):
    def setUp(self):
        loader = SourceFileLoader("ra_help_digits", os.path.join(REPO, "bin", "remote-agents"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ra = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ra)

    def test_user_config_help_says_tabs_not_workspaces(self):
        rows = dict(self.ra.digit_rows(read_text(FIXTURE)))
        self.assertEqual(rows, {"Ctrl+B 1..9": "jump to tab", "Alt+1..9": "jump to tab"})

    def test_after_full_merge_help_says_workspaces(self):
        merged, _ = bindings.plan_switch("")
        rows = dict(self.ra.digit_rows(merged))
        self.assertEqual(rows["Ctrl+B 1..9"], "jump to workspace")
        self.assertEqual(rows["Alt+1..9"], "jump to workspace")
        self.assertEqual(rows["Ctrl+1..9"], "jump to tab")


if __name__ == "__main__":
    unittest.main()
