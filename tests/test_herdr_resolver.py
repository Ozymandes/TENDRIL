"""tendril_herdr — the ONE Herdr binary resolver (LINK-CONTRACT-0.2 §7.4).

The live RC2 failure this module kills: a mise Herdr 0.9.3 at
~/.local/share/mise/installs/herdr while a stale /usr/bin/herdr 0.8.2
shadowed it on the service PATH, and `HERDR_BIN=` written EMPTY by the
installer meant every component did "first on PATH wins". These tests
pin, with fake herdr shells in temp dirs (each prints a version and
fakes status / api snapshot):

- recorded config path wins and is never re-probed for compatibility;
- a vanished recorded path re-detects (provenance: detected);
- newest protocol-compatible candidate wins — never PATH order —
  including the /usr/bin-old + mise-newer live scenario and the
  newer-but-incompatible corner;
- discovery covers ~/.local/bin only, mise (latest + version dirs,
  newest first), asdf shims/installs, and missing-herdr → missing;
- record() rewrites HERDR_BIN/HERDR_VERSION atomically (0600) with
  every other line byte-identical, appends missing keys, and dry-run
  writes nothing;
- the CLI verbs print machine-parseable lines and honest exit codes.

Hermetic: HOME / PATH / MISE_DATA_DIR / ASDF_DIR are patched to the temp
tree for every test, so the real host's herdr installs never leak in
(except /usr/bin/herdr, which lives outside HOME and is explicitly
excluded by pointing the fixed-dir scan at the fake HOME too — the
module reads only $HOME-derived dirs plus the literal system dirs, so
the /usr/bin scenario is tested with a dedicated PATH-only fixture).
"""
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_herdr  # noqa: E402

HERDR = os.path.join(REPO, "bin", "tendril_herdr.py")


def shq(text):
    return "'" + text.replace("'", "'\\''") + "'"


def make_herdr(path, version, compatible=True, running=True, log=None,
               server_version=None):
    """A fake herdr: --version prints `version`; `status server --json`
    reports the running SERVER (its own version via server_version, and
    whether it accepts this client); `api snapshot` answers."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    compat_json = "true" if compatible else "false"
    status_json = ('{"status":"running","running":%s,"version":"%s",'
                   '"compatible":%s,"endpoint_compatible":%s}'
                   % ("true" if running else "false",
                      server_version or version, compat_json, compat_json))
    body = ["#!/bin/sh"]
    if log:
        body.append('printf \'%%s\\n\' "$*" >> %s' % shq(log))
    body += [
        'case "$1" in',
        '  --version) echo "herdr %s"; exit 0 ;;' % version,
        '  status)',
        '      [ "$2" = "server" ] || { echo "client: herdr %s";' % version,
        '        echo "status: running"; \\',
        '        echo "endpoint_compatible: %s"; exit 0; }' % (
            "yes" if compatible else "no"),
        '      echo %s ;;' % shq(status_json),
        '  api) [ "$2" = "snapshot" ] && { echo "{}"; exit 0; }; exit 64 ;;',
        '  server) exit 0 ;;',
        'esac',
        'exit 0',
    ]
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    os.chmod(path, 0o755)
    return path


class ResolverEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril herdr-resolver ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.stubbin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.stubbin)
        patcher = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "MISE_DATA_DIR": os.path.join(self.home, ".local", "share",
                                          "mise"),
            "ASDF_DIR": os.path.join(self.home, ".asdf"),
            "PATH": self.stubbin,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("HERDR_BIN",):
            os.environ.pop(k, None)

    def set_path(self, *dirs):
        os.environ["PATH"] = os.pathsep.join(dirs)

    def write_config(self, **kv):
        d = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "config")
        with open(path, "w") as f:
            for k, v in kv.items():
                f.write("%s=%s\n" % (k, v))
        return path

    def resolve(self, config=None):
        return tendril_herdr.resolve(config=config or {})

    def detect(self):
        return tendril_herdr.detect()


class ResolutionTests(ResolverEnv):
    def test_recorded_path_wins_without_scanning_others(self):
        log = os.path.join(self.tmp, "recorded.log")
        bin_path = make_herdr(os.path.join(self.tmp, "recorded", "herdr"),
                              "0.9.3", compatible=True, log=log)
        res = self.resolve({"HERDR_BIN": bin_path})
        self.assertEqual(res["provenance"], "recorded")
        self.assertEqual(res["bin"], bin_path)
        self.assertEqual(res["version"], "0.9.3")
        self.assertFalse(res["stale"])
        # §7.4: the recorded path is used as-is — no candidate scan; the
        # only gate is one bounded status probe against the running server
        with open(log) as f:
            argvs = [ln.split()[0] if ln.split() else ""
                     for ln in f.read().splitlines()]
        self.assertEqual(argvs, ["--version", "status"])

    def test_recorded_path_moved_redetects_as_stale(self):
        make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        self.set_path(os.path.join(self.tmp, "pool"))
        res = self.resolve({"HERDR_BIN": os.path.join(self.tmp, "gone",
                                                      "herdr")})
        self.assertEqual(res["provenance"], "detected")
        self.assertEqual(res["bin"],
                         os.path.join(self.tmp, "pool", "herdr"))
        self.assertTrue(res["stale"])       # recorded value had to go

    def test_env_override_used_without_config(self):
        bin_path = make_herdr(os.path.join(self.tmp, "env", "herdr"), "0.9.3")
        with mock.patch.dict(os.environ, {"HERDR_BIN": bin_path}):
            res = self.resolve()
        self.assertEqual(res["bin"], bin_path)
        self.assertEqual(res["provenance"], "detected")

    def test_distro_old_mise_newer_picks_mise(self):
        # the live RC2 scenario: /usr/bin-style herdr 0.8.2 incompatible
        # with the running 0.9.3 server; a mise install 0.9.3 → mise wins
        make_herdr(os.path.join(self.stubbin, "herdr"), "0.8.2",
                   compatible=False)                      # "first on PATH"
        make_herdr(os.path.join(os.environ["MISE_DATA_DIR"], "installs",
                                "herdr", "0.9.3", "herdr"), "0.9.3",
                   compatible=True)
        picked, version, _verdicts = self.detect()
        self.assertEqual(picked, os.path.join(os.environ["MISE_DATA_DIR"],
                                              "installs", "herdr", "0.9.3",
                                              "herdr"))
        self.assertEqual(version, "0.9.3")

    def test_fixed_dirs_come_after_a_compatible_local(self):
        # /usr/bin/herdr is scanned too, but only when nothing better
        # exists: an incompatible distro binary never shadows a good one
        make_herdr(os.path.join(self.home, "usr", "bin", "herdr"),
                   "0.8.2", compatible=False)
        with mock.patch.object(tendril_herdr, "FIXED_DIRS",
                               (os.path.join(self.home, "usr", "bin"),)):
            picked, version, verdicts = self.detect()
        self.assertIsNone(picked)
        self.assertTrue(all(not ok for _c, ok, _d in verdicts))

    def test_newer_but_incompatible_is_rejected(self):
        # newest-first probing: 0.10.0 cannot drive the 0.9.3 server, so
        # the compatible 0.9.3 wins even though it is older
        make_herdr(os.path.join(self.tmp, "a", "herdr"), "0.10.0",
                   compatible=False)
        make_herdr(os.path.join(self.tmp, "b", "herdr"), "0.9.3",
                   compatible=True)
        self.set_path(os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b"))
        picked, version, verdicts = self.detect()
        self.assertEqual(picked, os.path.join(self.tmp, "b", "herdr"))
        self.assertEqual(version, "0.9.3")
        self.assertTrue(any("0.10.0" in d and not ok
                            for _c, ok, d in verdicts))

    def test_no_server_anything_compatible_newest_wins(self):
        make_herdr(os.path.join(self.tmp, "old", "herdr"), "0.8.2",
                   running=False)
        make_herdr(os.path.join(self.tmp, "new", "herdr"), "0.9.3",
                   running=False)
        self.set_path(os.path.join(self.tmp, "old"),
                      os.path.join(self.tmp, "new"))
        picked, version, _v = self.detect()
        self.assertEqual(version, "0.9.3")
        self.assertEqual(picked, os.path.join(self.tmp, "new", "herdr"))

    def test_local_bin_only(self):
        make_herdr(os.path.join(self.home, ".local", "bin", "herdr"), "0.9.3")
        res = self.resolve()
        self.assertEqual(res["bin"],
                         os.path.join(self.home, ".local", "bin", "herdr"))
        self.assertEqual(res["provenance"], "detected")

    def test_mise_latest_and_version_dirs_discovered(self):
        mise = os.environ["MISE_DATA_DIR"]
        make_herdr(os.path.join(mise, "installs", "herdr", "latest", "herdr"),
                   "0.9.1")
        make_herdr(os.path.join(mise, "installs", "herdr", "0.9.3", "herdr"),
                   "0.9.3")
        make_herdr(os.path.join(mise, "installs", "herdr", "0.9", "herdr"),
                   "0.9")
        res = self.resolve()
        self.assertEqual(res["version"], "0.9.3")
        self.assertEqual(os.path.dirname(os.path.dirname(res["bin"])),
                         os.path.join(mise, "installs", "herdr"))

    def test_asdf_shim_and_installs_discovered(self):
        asdf = os.environ["ASDF_DIR"]
        make_herdr(os.path.join(asdf, "shims", "herdr"), "0.9.0")
        make_herdr(os.path.join(asdf, "installs", "herdr", "0.9.4", "bin",
                                "herdr"), "0.9.4")
        res = self.resolve()
        self.assertEqual(res["version"], "0.9.4")

    def test_missing_everywhere_is_missing(self):
        with mock.patch.dict(os.environ,
                             {"TENDRIL_HERDR_FIXED_DIRS": ""}):
            res = self.resolve()
        self.assertIsNone(res["bin"])
        self.assertEqual(res["provenance"], "missing")

    def test_unrunnable_candidate_is_skipped(self):
        path = make_herdr(os.path.join(self.tmp, "dead", "herdr"), "0.9.9")
        os.chmod(path, 0o644)                   # not executable
        res = self.resolve({"HERDR_BIN": path})
        self.assertEqual(res["provenance"], "missing")

    # ---------------------------------------------- recorded server gate
    def test_mise_upgrade_is_followed_through_the_latest_alias(self):
        # record the STABLE entry (.../latest/herdr); after
        # `mise upgrade herdr` retargets the symlink 0.9.3 → 0.9.4 the
        # recorded value follows automatically — the RC2 pinning bug
        mise = os.environ["MISE_DATA_DIR"]
        v093 = make_herdr(os.path.join(mise, "installs", "herdr", "0.9.3",
                                       "herdr"), "0.9.3", running=False)
        latest = os.path.join(mise, "installs", "herdr", "latest")
        os.symlink(os.path.dirname(v093), latest)
        entry = os.path.join(latest, "herdr")
        res = self.resolve({"HERDR_BIN": entry})
        self.assertEqual(res["provenance"], "recorded")
        self.assertEqual(res["version"], "0.9.3")
        # the upgrade: latest now points at 0.9.4
        v094 = make_herdr(os.path.join(mise, "installs", "herdr", "0.9.4",
                                       "herdr"), "0.9.4", running=False)
        os.remove(latest)
        os.symlink(os.path.dirname(v094), latest)
        res = self.resolve({"HERDR_BIN": entry})
        self.assertEqual(res["provenance"], "recorded")     # still recorded
        self.assertEqual(res["bin"], entry)                 # same entry
        self.assertEqual(res["version"], "0.9.4")           # new target

    def test_recorded_realpath_pin_plus_newer_server_redetects(self):
        # an older install recorded the version-pinned realpath; a newer
        # server is running and refuses that client → detect() must catch
        # up to a candidate that matches the server (provenance detected,
        # stale)
        old = make_herdr(os.path.join(self.tmp, "pinned-0.9.3", "herdr"),
                         "0.9.3", compatible=False, server_version="0.9.4")
        new = make_herdr(os.path.join(self.tmp, "fresh-0.9.4", "herdr"),
                         "0.9.4", compatible=True, server_version="0.9.4")
        self.set_path(os.path.join(self.tmp, "fresh-0.9.4"))
        res = self.resolve({"HERDR_BIN": old})
        self.assertEqual(res["provenance"], "detected")
        self.assertEqual(res["bin"], new)
        self.assertEqual(res["version"], "0.9.4")
        self.assertTrue(res["stale"])

    def test_recorded_kept_when_server_accepts_it_despite_version(self):
        # the server reports the recorded client protocol-compatible even
        # though versions differ: the recorded binary still works — keep it
        old = make_herdr(os.path.join(self.tmp, "pinned-0.9.3", "herdr"),
                         "0.9.3", compatible=True, server_version="0.9.4")
        res = self.resolve({"HERDR_BIN": old})
        self.assertEqual(res["provenance"], "recorded")
        self.assertEqual(res["bin"], old)
        self.assertFalse(res["stale"])

    def test_recorded_lagging_but_no_better_candidate_stays(self):
        # the server is newer but detect finds nothing matching: keep the
        # recorded binary (health shows the mismatch from its own probe)
        old = make_herdr(os.path.join(self.tmp, "pinned-0.9.3", "herdr"),
                         "0.9.3", compatible=False, server_version="0.9.4")
        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty)
        self.set_path(empty)
        res = self.resolve({"HERDR_BIN": old})
        self.assertEqual(res["provenance"], "recorded")
        self.assertEqual(res["bin"], old)
        self.assertFalse(res["stale"])

    def test_shim_only_resolves_to_installs_realpath_never_the_shim(self):
        mise = os.environ["MISE_DATA_DIR"]
        real = make_herdr(os.path.join(mise, "installs", "herdr", "latest",
                                       "herdr"), "0.9.3", running=False)
        shim = os.path.join(mise, "shims")
        os.makedirs(shim)
        shim_path = os.path.join(shim, "herdr")
        with open(shim_path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(shim_path, 0o755)
        self.set_path(mise + "/shims")
        picked, version, _v = self.detect()
        self.assertEqual(picked, os.path.realpath(real))   # never the shim
        self.assertEqual(version, "0.9.3")

    def test_record_refuses_unsafe_world_writable_target(self):
        unsafe = make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        os.chmod(unsafe, 0o775)                 # group-writable
        path = self.write_config(HERDR_BIN="")
        self.assertFalse(tendril_herdr.record(path, unsafe, "0.9.3"))
        os.chmod(unsafe, 0o755)
        os.chmod(unsafe, 0o757)                 # world-writable
        self.assertFalse(tendril_herdr.record(path, unsafe, "0.9.3"))
        with open(path) as f:
            self.assertEqual(f.read(), "HERDR_BIN=\n")
        # root-owned non-writable targets stay fine (uid check tolerated)
        os.chmod(unsafe, 0o755)
        self.assertTrue(tendril_herdr.record(path, unsafe, "0.9.3"))


class RecordTests(ResolverEnv):
    def test_record_rewrites_only_herdr_keys(self):
        bin_path = make_herdr(os.path.join(self.tmp, "pool", "herdr"),
                              "0.9.3")
        path = self.write_config(TENDRIL_HOST="omarchy",
                                 SSH_TARGET="omarchy.ts.net",
                                 PROJECT_ROOTS="/home/x/Projects",
                                 HERDR_BIN="",
                                 REMOTE_AGENTS_BIN="/home/x/.local/bin/"
                                                   "remote-agents",
                                 NOTIFY_BIN="/home/x/.local/bin/"
                                            "herdr-notify")
        ok = tendril_herdr.record(path, bin_path, "0.9.3")
        self.assertTrue(ok)
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, [
            "TENDRIL_HOST=omarchy",
            "SSH_TARGET=omarchy.ts.net",
            "PROJECT_ROOTS=/home/x/Projects",
            "HERDR_BIN=%s" % bin_path,            # the entry, verbatim
            "REMOTE_AGENTS_BIN=/home/x/.local/bin/remote-agents",
            "NOTIFY_BIN=/home/x/.local/bin/herdr-notify",
            "HERDR_VERSION=0.9.3",
            "HERDR_REALPATH=%s" % os.path.realpath(bin_path),
        ])
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        # no temp leftovers
        self.assertEqual([n for n in os.listdir(os.path.dirname(path))
                          if n.startswith(".herdr-record-")], [])

    def test_record_keeps_a_symlinked_entry_and_records_its_target(self):
        # §7.4: record the stable entry (the symlink), never its target —
        # a `mise upgrade` retargets the symlink and the record follows
        real = make_herdr(os.path.join(self.tmp, "real-0.9.3", "herdr"),
                          "0.9.3")
        entry = os.path.join(self.tmp, "latest")
        os.symlink(os.path.dirname(real), entry)
        path = self.write_config()
        self.assertTrue(tendril_herdr.record(path, os.path.join(
            entry, "herdr"), "0.9.3"))
        with open(path) as f:
            cfg = dict(ln.strip().split("=", 1) for ln in f if "=" in ln)
        self.assertEqual(cfg["HERDR_BIN"], os.path.join(entry, "herdr"))
        self.assertEqual(cfg["HERDR_REALPATH"], os.path.realpath(real))

    def test_record_replaces_an_old_realpath_pin(self):
        # config from the rejected first patch (realpath-pinned) is
        # rewritten to the entry on the next record
        bin_path = make_herdr(os.path.join(self.tmp, "pool", "herdr"),
                              "0.9.3")
        path = self.write_config(HERDR_BIN=os.path.realpath(bin_path),
                                 HERDR_VERSION="0.9.2")
        self.assertTrue(tendril_herdr.record(path, bin_path, "0.9.3"))
        with open(path) as f:
            cfg = dict(ln.strip().split("=", 1) for ln in f if "=" in ln)
        self.assertEqual(cfg["HERDR_BIN"], bin_path)
        self.assertEqual(cfg["HERDR_VERSION"], "0.9.3")
        self.assertEqual(cfg["HERDR_REALPATH"], os.path.realpath(bin_path))

    def test_record_appends_missing_keys(self):
        make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        path = self.write_config(TENDRIL_HOST="omarchy")
        self.assertTrue(tendril_herdr.record(
            path, os.path.join(self.tmp, "pool", "herdr"), "0.9.3"))
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["TENDRIL_HOST=omarchy",
                                 "HERDR_BIN=%s" % os.path.join(
                                     self.tmp, "pool", "herdr"),
                                 "HERDR_VERSION=0.9.3",
                                 "HERDR_REALPATH=%s" % os.path.join(
                                     self.tmp, "pool", "herdr")])

    def test_record_realpaths_symlinks(self):
        real = make_herdr(os.path.join(self.tmp, "real", "herdr"), "0.9.3")
        link = os.path.join(self.tmp, "link-herdr")
        os.symlink(real, link)
        path = self.write_config()
        self.assertTrue(tendril_herdr.record(path, link, "0.9.3"))
        with open(path) as f:
            cfg = dict(ln.strip().split("=", 1) for ln in f if "=" in ln)
        self.assertEqual(cfg["HERDR_BIN"], link)          # entry preserved
        self.assertEqual(cfg["HERDR_REALPATH"], os.path.realpath(real))

    def test_record_dry_run_writes_nothing(self):
        make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        path = self.write_config(HERDR_BIN="")
        with open(path) as f:
            before = f.read()
        self.assertTrue(tendril_herdr.record(
            path, os.path.join(self.tmp, "pool", "herdr"), "0.9.3",
            dry_run=True))
        with open(path) as f:
            self.assertEqual(f.read(), before)

    def test_record_refuses_unusable_binary(self):
        path = self.write_config(HERDR_BIN="")
        self.assertFalse(tendril_herdr.record(
            path, os.path.join(self.tmp, "nope", "herdr"), "9.9.9"))
        self.assertFalse(tendril_herdr.record(path, "", "9.9.9"))


class CliTests(ResolverEnv):
    def cli(self, *argv):
        import subprocess
        return subprocess.run([sys.executable, HERDR, *argv],
                              capture_output=True, text=True, timeout=120)

    def test_detect_cli_lines(self):
        make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        self.set_path(os.path.join(self.tmp, "pool"))
        p = self.cli("detect")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("chosen: ", p.stdout)
        self.assertIn("version: 0.9.3", p.stdout)
        self.assertIn("provenance: detected", p.stdout)
        self.assertIn("realpath: ", p.stdout)

    def test_detect_cli_missing_is_honest(self):
        with mock.patch.dict(os.environ,
                             {"TENDRIL_HERDR_FIXED_DIRS": ""}):
            p = self.cli("detect")
        self.assertEqual(p.returncode, 0)
        self.assertIn("chosen: NONE", p.stdout)
        self.assertIn("provenance: missing", p.stdout)

    def test_record_cli_writes_and_reports(self):
        make_herdr(os.path.join(self.tmp, "pool", "herdr"), "0.9.3")
        self.set_path(os.path.join(self.tmp, "pool"))
        cfg = self.write_config(HERDR_BIN="")
        p = self.cli("record", "--config", cfg)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("herdr-bin: ", p.stdout)
        self.assertIn("herdr-version: 0.9.3", p.stdout)
        self.assertIn("herdr-realpath: ", p.stdout)
        self.assertIn("herdr-provenance: detected", p.stdout)
        with open(cfg) as f:
            body = f.read()
        self.assertIn("HERDR_VERSION=0.9.3", body)
        self.assertIn("HERDR_REALPATH=", body)
        self.assertNotIn("HERDR_BIN=" + os.path.join(
            self.tmp, "pool", "shims"), body)     # never a shim

    def test_record_cli_without_herdr_fails_clean(self):
        cfg = self.write_config(HERDR_BIN="")
        with mock.patch.dict(os.environ,
                             {"TENDRIL_HERDR_FIXED_DIRS": ""}):
            p = self.cli("record", "--config", cfg)
        self.assertEqual(p.returncode, 1)
        self.assertIn("chosen: NONE", p.stderr)
        with open(cfg) as f:
            body = f.read()
        self.assertEqual(body, "HERDR_BIN=\n")

    def test_usage_error(self):
        p = self.cli("bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stderr.lower())


if __name__ == "__main__":
    unittest.main()
