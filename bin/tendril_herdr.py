#!/usr/bin/env python3
"""tendril_herdr — the ONE Herdr binary resolver (LINK-CONTRACT-0.2 §7.4).

Every host component (remote-agents, herdr-notify, tendril_upgrade,
health, guardian, scheduler, restart, launch, notify) resolves Herdr
through this module. There is deliberately no second candidate list
anywhere else: "first on PATH wins" is how a stale /usr/bin/herdr 0.8.2
ended up talking to a mise Herdr 0.9.3 server.

Resolution order (§7.4):
  1. recorded  — config HERDR_BIN when the path exists and is executable
  2. env       — $HERDR_BIN (same check)                      [provenance: detected]
  3. detect    — protocol-verified NEWEST-compatible candidate among
                 PATH, ~/.local/bin, mise (shims + installs incl. latest),
                 asdf (shims + installs), /opt/homebrew/bin,
                 /usr/local/bin, /usr/bin — never PATH order.

"Protocol-verified" (the same evidence `herdr status server --json`
gives a human): while a server is running, a candidate qualifies only
when that server reports it compatible (endpoint_compatible /
private_protocol_compatible / compatible). With no server running every
candidate is compatible and the newest version wins. An old client
facing a newer running server is therefore never picked — exactly the
live failure this module exists to kill.

`record` writes HERDR_BIN=<stable entry> + HERDR_VERSION +
HERDR_REALPATH=<realpath> into the TENDRIL config (every other line
byte-identical, atomic 0600 write) so services never depend on PATH.
The ENTRY path is recorded — never a version-pinned install dir when a
stable alias exists (`mise .../latest/herdr`), and NEVER a version-
manager shim (mise/asdf shims depend on cwd/.tool-versions): a shim
resolves to what the installs dir holds, and the real target must be a
user/root-owned regular file that is not group/world-writable, else the
record refuses (honest failure).

`resolve` re-checks the recorded binary against the RUNNING server (one
bounded status probe): recorded wins with no server, or when the server
reports it compatible / the same version. A newer running server
(version differs, compatibility not confirmed) triggers detect(); a
protocol-compatible candidate matching the server replaces it with
provenance "detected" and stale=True (health: binary detected,
tendril degraded, hint: re-run tendril --upgrade).

Stdlib only, Python 3.9+. CLI:
    tendril_herdr.py detect  [--config PATH]
    tendril_herdr.py record  --config PATH [--dry-run]
"""
import os
import re
import stat
import subprocess
import sys
import tempfile

VERSION_TIMEOUT_S = 3.0
STATUS_TIMEOUT_S = 4.0

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_SAFE_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,23}$")
_KEY_RE = re.compile(r"^(HERDR_BIN|HERDR_VERSION|HERDR_REALPATH)=")

# Fixed directories, in probe order (install.sh, Homebrew, distro).
# TENDRIL_HERDR_FIXED_DIRS overrides them (test seam; colon-separated).
FIXED_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
              "/usr/bin")


def parse_env_file(path):
    """KEY=VALUE lines, first match wins, matching quotes stripped — the
    same loose shape remote-agents / herdr-notify / the installer read."""
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


def run_bounded(argv, timeout):
    """(rc, stdout, stderr); never raises (timeout → 124, missing → 127)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except (FileNotFoundError, OSError) as exc:
        return 127, "", str(exc)


# ----------------------------------------------------------------- versions
def parse_version(text):
    """First dotted-number token ("herdr 0.9.3" → "0.9.3"), else None."""
    m = _VERSION_RE.search(text or "")
    return m.group(0) if m else None


def _vkey(version):
    """Sort key: ("0.9.10" > "0.9.9"); unparsable sorts lowest."""
    if not version:
        return (0,)
    out = []
    for part in version.split("."):
        out.append(int(part) if part.isdigit() else 0)
    return (1, tuple(out))


def version_of(path, run=None, timeout=VERSION_TIMEOUT_S):
    """The candidate's own client version from `--version`, or None."""
    run = run or run_bounded
    if not path:
        return None
    rc, out, err = run([path, "--version"], timeout)
    if rc in (124, 127):
        return None
    return parse_version((out or "") + (err or ""))


# --------------------------------------------------------------- candidates
def _version_dirs(base):
    """Version-numbered subdirectories of base, newest first (mise/asdf)."""
    try:
        names = os.listdir(base)
    except OSError:
        return []
    pairs = [(n, os.path.join(base, n)) for n in names]
    pairs.sort(key=lambda t: _vkey(t[0]), reverse=True)
    return [p for _n, p in pairs]


def candidate_paths(config=None, env=None):
    """Every plausible herdr binary, deduped by realpath, in probe order:
    recorded config value, env override, EVERY PATH entry (never just the
    first match — "first on PATH wins" is the bug), fixed dirs, mise,
    asdf."""
    env = os.environ if env is None else env
    out = []
    for override in ((config or {}).get("HERDR_BIN"),
                     env.get("HERDR_BIN")):
        if override:
            out.append(os.path.expanduser(override))
    for d in (env.get("PATH") or "").split(os.pathsep):
        if d:
            out.append(os.path.join(d, "herdr"))
    fixed = env.get("TENDRIL_HERDR_FIXED_DIRS")
    fixed_dirs = ([d for d in fixed.split(":") if d] if fixed is not None
                  else FIXED_DIRS)
    for d in fixed_dirs:
        out.append(os.path.join(os.path.expanduser(d), "herdr"))
    # mise: shims + installs (latest first, then newest version dirs)
    mise = os.path.expanduser(
        env.get("MISE_DATA_DIR") or "~/.local/share/mise")
    out.append(os.path.join(mise, "shims", "herdr"))
    out.append(os.path.join(mise, "installs", "herdr", "latest", "herdr"))
    for d in _version_dirs(os.path.join(mise, "installs", "herdr")):
        out.append(os.path.join(d, "herdr"))
    # asdf: shims + installs (newest version dirs)
    asdf = os.path.expanduser(env.get("ASDF_DIR") or "~/.asdf")
    out.append(os.path.join(asdf, "shims", "herdr"))
    for d in _version_dirs(os.path.join(asdf, "installs", "herdr")):
        out.append(os.path.join(d, "bin", "herdr"))
    seen, uniq = set(), []
    for cand in out:
        if not cand:
            continue
        real = os.path.realpath(cand)
        if real in seen:
            continue
        seen.add(real)
        uniq.append(cand)
    return uniq


def _usable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


# --------------------------------------------------- stable entry (§7.4)
def _newest_install(installs_dir, suffix):
    """Newest versioned install under installs_dir ending in suffix."""
    for d in _version_dirs(installs_dir):
        cand = os.path.join(d, suffix)
        if os.path.isfile(cand):
            return cand
    return None


def entry_of(path, env=None):
    """The stable, recordable ENTRY for a resolved candidate (§7.4):
    NEVER a version-manager shim (mise/asdf shims depend on cwd and
    .tool-versions — a shim maps to the realpath of what the installs
    dir holds) and never a version-pinned install dir when a stable
    alias exists (`mise installs/herdr/latest/herdr`). Everything else
    (PATH dirs, ~/.local/bin, Homebrew, /usr/bin, the latest alias
    itself) records as-is. None when only a shim exists and the installs
    dir cannot back it (nothing recordable — honest refusal)."""
    env = os.environ if env is None else env
    p = os.path.abspath(path)
    parts = p.split(os.sep)
    if len(parts) >= 2 and parts[-1] == "herdr" and parts[-2] == "shims":
        base = os.path.dirname(os.path.dirname(p))   # mise / .asdf data dir
        installs = os.path.join(base, "installs", "herdr")
        latest = os.path.join(installs, "latest", "herdr")
        if os.path.isfile(latest):
            return os.path.realpath(latest)          # what the shim serves
        newest = _newest_install(installs, os.path.join("bin", "herdr")) \
            or _newest_install(installs, "herdr")
        return os.path.realpath(newest) if newest else None
    d = os.path.dirname(p)
    # mise version dir .../installs/herdr/<ver>/herdr → the latest alias
    if os.path.basename(os.path.dirname(d)) == "herdr" \
            and os.path.basename(os.path.dirname(os.path.dirname(d))) \
            == "installs" and os.path.basename(d) != "latest":
        latest = os.path.join(os.path.dirname(d), "latest", "herdr")
        if os.path.isfile(latest):
            return latest                 # the symlink PATH, not its target
    return p


def _safe_target(real):
    """Symlink safety for recording: the realpath target must be a
    regular file owned by the user or root, not group/world-writable."""
    try:
        st = os.stat(real)               # follows symlinks
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid not in (0, os.getuid()):
        return False
    if st.st_mode & 0o022:               # group/world writable
        return False
    return True


# ------------------------------------------------------------------- probe
def _status_facts(path, run, timeout):
    """(running, compatible, version|None) from `status server --json`."""
    rc, out, _err = run([path, "status", "server", "--json"], timeout)
    if rc in (124, 127):
        return None, None, None
    try:
        import json
        doc = json.loads(out)
    except ValueError:
        return None, None, None
    if not isinstance(doc, dict):
        return None, None, None
    running = doc.get("running")
    running = True if running is True else False if running is False else None
    version = doc.get("version")
    if not (isinstance(version, str) and _SAFE_VERSION_RE.match(version)):
        version = None
    compat = doc.get("compatible")
    if compat is None:
        compat = doc.get("endpoint_compatible")
    compat = True if compat is True else False if compat is False else None
    return running, compat, version


def _status_legacy_facts(path, run, timeout):
    """Older clients: the plain `herdr status` regexes remote-agents used."""
    rc, out, err = run([path, "status"], timeout)
    if rc in (124, 127):
        return None, None, None
    txt = (out or "") + (err or "")
    running = re.search(r"status:\s*running", txt) is not None
    compat = None
    if re.search(r"endpoint_compatible:\s*yes", txt) \
            or re.search(r"private_protocol_compatible:\s*yes", txt):
        compat = True
    elif running and re.search(r"INCOMPATIBLE", txt):
        compat = False
    return running, compat, parse_version(txt)


def probe(path, run=None):
    """(compatible, version, detail): a client is compatible when no
    server is running, or the running server reports it compatible. The
    verdict comes from `status server --json` (compatible/
    endpoint_compatible) and falls back to the plain `herdr status`
    regexes when the JSON carries no verdict (older clients). A running
    server with NO verdict anywhere is treated as incompatible: an
    unverifiable client never wins by default."""
    run = run or run_bounded
    if not _usable(path):
        return False, None, "missing or not executable"
    version = version_of(path, run)
    running, compat, sver = _status_facts(path, run, STATUS_TIMEOUT_S)
    if running is None:                       # pre-JSON clients / garbage
        running, compat, sver = _status_legacy_facts(path, run,
                                                     STATUS_TIMEOUT_S)
    elif running and compat is None:          # JSON without a verdict
        _r, compat, _v = _status_legacy_facts(path, run, STATUS_TIMEOUT_S)
    label = version or "(no version output)"
    if running is None:
        return True, version, "%s (server status unreadable)" % label
    if not running:
        return True, version, "%s (no running server detected)" % label
    if compat is True:
        return True, version, ("%s (protocol-compatible with running "
                               "server %s)" % (label, sver or "?"))
    return False, version, ("%s (INCOMPATIBLE with running server %s)"
                            % (label, sver or "?"))


# ----------------------------------------------------------------- resolve
def detect(config=None, env=None, run=None):
    """Newest protocol-verified candidate, or (None, verdicts). The pick
    is normalized to its stable entry (never a shim; the latest alias
    over a version dir) so the same path is safe to record and to run."""
    verdicts = []
    picked = None
    picked_version = None
    cands = candidate_paths(config=config, env=env)
    usable = [c for c in cands if _usable(c)]
    # newest-first probe order: version scan up front (bounded), ties keep
    # the declared candidate order — PATH order never decides alone.
    scored = []
    for i, cand in enumerate(usable):
        v = version_of(cand, run)
        scored.append(((_vkey(v), -i), v, cand))
    scored.sort(key=lambda t: t[0], reverse=True)
    for _k, v, cand in scored:
        ok, _ver, detail = probe(cand, run)
        verdicts.append((cand, ok, detail))
        if ok:
            entry = entry_of(cand, env)
            is_shim = (os.path.basename(os.path.dirname(
                os.path.abspath(cand))) == "shims")
            follows = is_shim or (entry and _usable(entry)
                                  and entry != cand
                                  and os.path.samefile(entry, cand))
            if entry and _usable(entry) and entry != cand and follows:
                # prefer the stable entry at runtime too — but never
                # downgrade: an alias pointing at an OLDER build keeps
                # the version-dir candidate that just verified
                picked = entry
                picked_version = version_of(entry, run) or (_ver or v)
            else:
                picked = (entry if (entry and _usable(entry)
                                    and is_shim) else cand)
                picked_version = _ver or v
            break
    return picked, picked_version, verdicts


def _recorded_gate(recorded, run):
    """(gate_open, server_version, recorded_version) for the recorded
    path: gate_open means "keep the recorded binary" — no server is
    running, or the server reports it protocol-compatible / the same
    version. One bounded status probe (reused evidence, no extra scan)."""
    rver = version_of(recorded, run)
    runner = run or run_bounded
    running, compat, sver = _status_facts(recorded, runner, STATUS_TIMEOUT_S)
    if running is None:                        # pre-JSON clients / garbage
        running, compat, sver = _status_legacy_facts(recorded, runner,
                                                     STATUS_TIMEOUT_S)
    same = bool(sver and rver and sver == rver)
    keep = (running is not True) or compat is True or same
    return keep, sver, rver


def resolve(config=None, env=None, run=None):
    """{bin, provenance, version, verdicts, stale, realpath} —
    recorded → (server-gate) → env → detect.

    The recorded binary wins only while the world agrees with it: no
    running server, or the server reports it compatible / the same
    version. When a NEWER server is running (version differs and
    compatibility is not confirmed), detect() runs; a protocol-
    compatible candidate matching the server replaces the recorded one
    with provenance "detected" and stale=True — health then shows
    binary=detected + tendril degraded (hint: re-run tendril --upgrade).
    When the recorded path vanished, detection also runs with stale=True
    (§7.4: re-detect, binary detected, health degraded)."""
    config = config or {}
    verdicts = []
    recorded = os.path.expanduser(config.get("HERDR_BIN") or "")
    if recorded and _usable(recorded):
        keep, sver, rver = _recorded_gate(recorded, run)
        if keep:
            real = os.path.realpath(recorded)
            return {"bin": recorded, "provenance": "recorded",
                    "version": rver, "verdicts": verdicts, "stale": False,
                    "realpath": real}
        if sver and rver and sver != rver:   # a NEWER server is running
            det_bin, det_ver, verdicts = detect(config=config, env=env,
                                                run=run)
            # "matching the server": the detect pick must drive it —
            # same version when both versions are known
            if det_bin and (not sver or not det_ver or det_ver == sver):
                return {"bin": det_bin, "provenance": "detected",
                        "version": det_ver, "verdicts": verdicts,
                        "stale": True,
                        "realpath": os.path.realpath(det_bin)}
        # nothing better: keep the recorded binary; health shows the
        # mismatch from the version evidence it already has
        return {"bin": recorded, "provenance": "recorded",
                "version": rver, "verdicts": verdicts, "stale": False,
                "realpath": os.path.realpath(recorded)}
    env = os.environ if env is None else env
    from_env = os.path.expanduser(env.get("HERDR_BIN") or "")
    if _usable(from_env):
        return {"bin": from_env, "provenance": "detected",
                "version": version_of(from_env, run), "verdicts": verdicts,
                "stale": False, "realpath": os.path.realpath(from_env)}
    bin_path, version, verdicts = detect(config=config, env=env, run=run)
    return {"bin": bin_path, "provenance": "detected" if bin_path
            else "missing", "version": version, "verdicts": verdicts,
            "stale": bool(bin_path and recorded),       # recorded vanished
            "realpath": os.path.realpath(bin_path) if bin_path else None}


# ------------------------------------------------------------------ record
def record(config_path, bin_path, version=None, dry_run=False):
    """Rewrite the Herdr keys in the TENDRIL config: HERDR_BIN=<stable
    entry> (the given path, used verbatim — never a shim, stable across
    `mise upgrade`), HERDR_VERSION, and HERDR_REALPATH=<realpath> for
    provenance. Every other line (order included) stays byte-identical.
    Atomic 0600 write. False (honest refusal) when the entry is missing
    or its real target is not a user/root-owned regular file that is not
    group/world-writable."""
    if not _usable(bin_path):
        return False
    real = os.path.realpath(bin_path)
    if not _safe_target(real):
        return False
    try:
        with open(config_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    want = [("HERDR_BIN", bin_path), ("HERDR_VERSION", version or ""),
            ("HERDR_REALPATH", real)]
    seen = set()
    out = []
    for ln in lines:
        m = _KEY_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        key = m.group(1)
        seen.add(key)
        for k, v in want:
            if k == key:
                out.append("%s=%s" % (k, v))
    for k, v in want:
        if k not in seen:
            out.append("%s=%s" % (k, v))
    if dry_run:
        return True
    d = os.path.dirname(config_path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".herdr-record-", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_path)
        dirfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------- CLI
def _print_detect(res):
    for cand, ok, detail in res["verdicts"]:
        tag = "SELECTED" if cand == res["bin"] else "REJECT"
        print("[%s] %s\n        %s" % (tag, cand, detail))
    print("chosen:", res["bin"] or "NONE")
    print("version:", res["version"] or "")
    print("provenance:", res["provenance"])
    if res["bin"]:
        real = res.get("realpath") or os.path.realpath(res["bin"])
        print("entry:", res["bin"] if _safe_target(real) else "NONE")
        print("safe:", "yes" if _safe_target(real) else "no")
        print("realpath:", real)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    config_path = None
    if "--config" in args:
        i = args.index("--config")
        if i + 1 < len(args):
            config_path = args[i + 1]
            del args[i:i + 2]
    verb = args[0] if args else ""
    if verb not in ("detect", "record"):
        print("usage: tendril_herdr.py detect [--config PATH]\n"
              "       tendril_herdr.py record --config PATH [--dry-run]",
              file=sys.stderr)
        return 2
    config = parse_env_file(config_path) if config_path else {}
    res = resolve(config=config)
    if verb == "detect":
        return _print_detect(res)
    if not config_path:
        print("record needs --config PATH", file=sys.stderr)
        return 2
    if not res["bin"]:
        print("chosen: NONE\n"
              "note: no protocol-verified herdr found; recorded value left"
              " untouched", file=sys.stderr)
        return 1
    if not record(config_path, res["bin"], res["version"], dry_run=dry_run):
        print("record failed", file=sys.stderr)
        return 1
    print("herdr-bin: %s" % res["bin"])
    print("herdr-version: %s" % (res["version"] or ""))
    print("herdr-realpath: %s" % (res.get("realpath") or
                                  os.path.realpath(res["bin"])))
    print("herdr-provenance: %s" % res["provenance"])
    if res.get("stale"):
        print("herdr-note: recorded binary lagged the running server -"
              " re-detected; re-run tendril --upgrade to re-record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
