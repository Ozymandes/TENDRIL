"""Installer tests for safe Herdr detach binding migration."""
import importlib.util
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
        source = read_text(INSTALL)
        start = source.index("herdr_switch() {")
        end = source.index("\n}\n", start)
        switch = source[start:end]
        self.assertNotRegex(
            switch,
            r'(?m)^text,\s*did\s*=\s*set_scalar\(text,\s*"keys",\s*"previous_workspace"')
        self.assertNotIn('"alt+up"', switch)
        self.assertNotIn("previous_workspace=alt+up", switch)


if __name__ == "__main__":
    unittest.main()
