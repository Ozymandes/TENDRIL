#!/usr/bin/env python3
"""tendril_host — the few host facts and lifecycle verbs that differ by OS.

Everything here is chosen by capability, not by OS name: the watcher runs
under launchd where `launchctl` exists and under a systemd user manager
where `systemctl --user` answers; host facts come from /proc where it
exists and from sysctl/vm_stat/pmset otherwise. Stdlib only, Python 3.9+
(the Xcode Command Line Tools python3 on macOS).

    tendril_host.py service install|start|stop|restart|status|uninstall|logs
                    [--dry-run] [--pkg DIR] [--python PATH]
    tendril_host.py remote-path check|plan|apply|remove [--dry-run]
    tendril_host.py facts | hostname | tailscale-ip | ssh-server
"""
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

LABEL = "com.tendril.herdr-notify"
UNIT = "herdr-notify.service"
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# Where macOS package managers put binaries. Neither is on the PATH that
# sshd hands a non-interactive session (`/usr/bin:/bin:/usr/sbin:/sbin`).
BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")
SSHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _run(args, timeout=6, env=None, socket_stdin=False):
    """socket_stdin: give the child a socket on stdin, as sshd does (bash
    only reads ~/.bashrc for `bash -c` when stdin is a network socket)."""
    pair = socket.socketpair() if socket_stdin else None
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, env=env,
                           stdin=pair[0].fileno() if pair else None)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)
    finally:
        if pair:
            pair[0].close()
            pair[1].close()


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


# ---------------------------------------------------------------- facts
def tailscale_cli(which=shutil.which, exists=os.path.isfile):
    """The Tailscale CLI: PATH first, then the macOS app bundle (the App
    Store / standalone app ships its CLI inside and only optionally links
    it onto PATH)."""
    if which("tailscale"):
        return "tailscale"
    return TAILSCALE_APP if exists(TAILSCALE_APP) else None


def tailscale_ip(run=_run, cli=None):
    cli = cli or tailscale_cli()
    if not cli:
        return ""
    rc, out, _ = run([cli, "ip", "-4"], 4)
    lines = out.strip().splitlines() if rc == 0 else []
    return lines[0].strip() if lines else ""


def tailscale_name(run=_run, cli=None):
    """This node's MagicDNS short name ('' when Tailscale is down/absent).
    cli: the Tailscale command to use (default: found via tailscale_cli)."""
    cli = cli or tailscale_cli()
    if not cli:
        return ""
    rc, out, _ = run([cli, "status", "--json"], 4)
    if rc != 0:
        return ""
    try:
        dns = (json.loads(out).get("Self") or {}).get("DNSName") or ""
    except ValueError:
        return ""
    return dns.split(".", 1)[0]


def short_hostname(nodename=None):
    """uname -n without the mDNS '.local' suffix macOS adds."""
    name = nodename if nodename is not None else os.uname().nodename
    return name[:-6] if name.lower().endswith(".local") else name


def load_average():
    try:
        return tuple("%.2f" % x for x in os.getloadavg())
    except OSError:
        return ("?", "?", "?")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def memory_mb(run=_run, read=_read):
    """(available, total) in MiB, or None when the host will not say."""
    meminfo = read("/proc/meminfo")
    if meminfo is not None or os.path.exists("/proc/meminfo"):
        meminfo = meminfo or ""
        mem = {}
        for ln in meminfo.splitlines():
            k, _, v = ln.partition(":")
            if v.split():
                mem[k] = int(v.split()[0]) // 1024
        if "MemTotal" in mem:
            return mem.get("MemAvailable", mem.get("MemFree", 0)), mem["MemTotal"]
        return None
    rc, total, _ = run(["sysctl", "-n", "hw.memsize"], 3)
    if rc != 0 or not total.strip().isdigit():
        return None
    total_mb = int(total.strip()) // (1024 * 1024)
    rc, vm, _ = run(["vm_stat"], 3)
    if rc != 0:
        return None
    m = re.search(r"page size of (\d+) bytes", vm)
    page = int(m.group(1)) if m else 4096
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(re.escape(key) + r":\s+(\d+)", vm)
        if m:
            pages += int(m.group(1))
    return pages * page // (1024 * 1024), total_mb


def battery(run=_run, read=_read):
    for b in ("/sys/class/power_supply/BAT0/capacity",
              "/sys/class/power_supply/BAT1/capacity"):
        cap = read(b)
        if cap is not None:
            return cap.strip() + "%"
    if shutil.which("pmset"):
        rc, out, _ = run(["pmset", "-g", "batt"], 3)
        m = re.search(r"(\d+)%", out) if rc == 0 else None
        if m:
            return m.group(1) + "%"
    return ""


def uptime_seconds(run=_run, read=_read, now=time.time, procfs=None):
    if procfs if procfs is not None else os.path.exists("/proc/uptime"):
        try:
            return int(float((read("/proc/uptime") or "").split()[0]))
        except (ValueError, IndexError):
            return None
    rc, out, _ = run(["sysctl", "-n", "kern.boottime"], 3)
    m = re.search(r"sec\s*=\s*(\d+)", out) if rc == 0 else None
    return int(now()) - int(m.group(1)) if m else None


def format_uptime(seconds):
    if seconds is None or seconds < 0:
        return "?"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    return "%dm" % minutes


def ssh_server_listening(port=22, host="127.0.0.1", timeout=2.0):
    """True when something accepts TCP on the SSH port (macOS: Remote Login)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_name(run=_run):
    """The name phones should dial: MagicDNS short name, else hostname."""
    return tailscale_name(run) or short_hostname()


def facts(run=_run, read=_read):
    mem = memory_mb(run, read)
    return {
        "host": short_hostname(),
        "tailscale": tailscale_ip(run) or "down",
        "uptime": format_uptime(uptime_seconds(run, read)),
        "load": list(load_average()),
        "mem_available_mb": mem[0] if mem else None,
        "mem_total_mb": mem[1] if mem else None,
        "battery": battery(run, read),
    }


# ---------------------------------------------------------------- service
class _Backend:
    name = "none"

    def __init__(self, run=_run, dry_run=False, out=None):
        self.run = run
        self.dry_run = dry_run
        self.out = out or sys.stdout

    def say(self, msg):
        print(msg, file=self.out)

    def do(self, desc, args, check=True):
        """Run a mutating command (or show it under --dry-run)."""
        if self.dry_run:
            self.say("  [dry-run] %s" % desc)
            self.say("            -> %s" % " ".join(args))
            return 0
        rc, out, err = self.run(args, 15)
        if rc != 0 and check:
            self.say("  %s: FAILED (%s)" % (desc, (err or out).strip() or rc))
        return rc


class Launchd(_Backend):
    """A per-user LaunchAgent. launchd has no watchdog: a crashed watcher is
    restarted (KeepAlive on unsuccessful exit), a hung one is not."""
    name = "launchd"

    def __init__(self, run=_run, dry_run=False, out=None, uid=None):
        super().__init__(run, dry_run, out)
        self.uid = os.getuid() if uid is None else uid
        self.plist = home("Library", "LaunchAgents", LABEL + ".plist")
        self.log = home("Library", "Logs", "tendril", "herdr-notify.log")

    def domain(self):
        """gui/<uid> in a desktop login; user/<uid> over a bare SSH login."""
        gui = "gui/%d" % self.uid
        rc, _, _ = self.run(["launchctl", "print", gui], 5)
        return gui if rc == 0 else "user/%d" % self.uid

    def target(self):
        return "%s/%s" % (self.domain(), LABEL)

    def render(self, python, script):
        path = [home(".local", "bin")] + list(BREW_BINS) + SSHD_PATH.split(":")
        return plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": [python, script],
            "EnvironmentVariables": {"PATH": ":".join(path)},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # restart on crash only
            "ThrottleInterval": 5,
            "ProcessType": "Background",
            "Umask": 0o077,                         # log names workspaces
            "StandardOutPath": self.log,
            "StandardErrorPath": self.log,
        }, sort_keys=False)

    def loaded(self):
        rc, _, _ = self.run(["launchctl", "print", self.target()], 5)
        return rc == 0

    def install(self, pkg_dir, python):
        script = home(".local", "bin", "herdr-notify")
        body = self.render(python, script)
        if self.dry_run:
            self.say("  [dry-run] write %s:" % self.plist)
            for ln in body.decode().splitlines():
                self.say("      " + ln)
        else:
            os.makedirs(os.path.dirname(self.plist), exist_ok=True)
            os.makedirs(os.path.dirname(self.log), mode=0o700, exist_ok=True)
            _atomic_write(self.plist, body, 0o644)
            self.say("  wrote %s" % self.plist)
        domain = self.domain()
        # bootout first so a re-install picks up the new plist and binary
        self.do("unload previous agent (if any)",
                ["launchctl", "bootout", "%s/%s" % (domain, LABEL)], check=False)
        self.do("enable %s" % LABEL,
                ["launchctl", "enable", "%s/%s" % (domain, LABEL)], check=False)
        return self.do("load %s into %s" % (LABEL, domain),
                       ["launchctl", "bootstrap", domain, self.plist])

    def start(self):
        if not self.dry_run and not os.path.isfile(self.plist):
            self.say("  not installed: %s missing (run ./install)" % self.plist)
            return 1
        if self.loaded():
            return self.do("start", ["launchctl", "kickstart", self.target()])
        return self.do("load + start",
                       ["launchctl", "bootstrap", self.domain(), self.plist])

    def stop(self):
        # bootout, not kill: KeepAlive would resurrect a killed watcher.
        # The plist stays, so the watcher starts again at next login.
        return self.do("stop (unload until next login)",
                       ["launchctl", "bootout", self.target()], check=False)

    def restart(self):
        if self.loaded():
            return self.do("restart", ["launchctl", "kickstart", "-k", self.target()])
        return self.start()

    def status(self):
        if not os.path.isfile(self.plist):
            return "not installed"
        rc, out, _ = self.run(["launchctl", "print", self.target()], 5)
        if rc != 0:
            return "stopped"
        return "running" if re.search(r"^\s*state = running", out, re.M) else "stopped"

    def uninstall(self):
        self.stop()
        if os.path.isfile(self.plist) or os.path.islink(self.plist):
            if self.dry_run:
                self.say("  [dry-run] remove %s" % self.plist)
            else:
                os.remove(self.plist)
                self.say("removed: %s (watcher unloaded)" % self.plist)
        return 0

    def logs(self):
        return "tail -f %s" % _shq(self.log)


class Systemd(_Backend):
    """A systemd user unit, with WatchdogSec: a hung watcher is restarted."""
    name = "systemd"

    def __init__(self, run=_run, dry_run=False, out=None):
        super().__init__(run, dry_run, out)
        self.unit = home(".config", "systemd", "user", UNIT)

    def install(self, pkg_dir, python):
        src = os.path.join(pkg_dir, "systemd", UNIT)
        if self.dry_run:
            self.say("  [dry-run] install %s" % self.unit)
            self.say("            -> install -m 600 %s %s" % (src, self.unit))
        else:
            os.makedirs(os.path.dirname(self.unit), exist_ok=True)
            with open(src, "rb") as f:
                _atomic_write(self.unit, f.read(), 0o600)
            self.say("  install %s" % self.unit)
        self.do("daemon-reload", ["systemctl", "--user", "daemon-reload"])
        return self.do("enable --now %s" % UNIT,
                       ["systemctl", "--user", "enable", "--now", UNIT])

    def start(self):
        return self.do("start", ["systemctl", "--user", "start", UNIT])

    def stop(self):
        return self.do("stop", ["systemctl", "--user", "stop", UNIT])

    def restart(self):
        return self.do("restart", ["systemctl", "--user", "restart", UNIT])

    def status(self):
        if not os.path.isfile(self.unit):
            return "not installed"
        rc, _, _ = self.run(["systemctl", "--user", "is-active", UNIT], 3)
        return "running" if rc == 0 else "stopped"

    def uninstall(self):
        if not os.path.isfile(self.unit):
            return 0
        self.do("disable --now", ["systemctl", "--user", "disable", "--now", UNIT],
                check=False)
        if self.dry_run:
            self.say("  [dry-run] remove %s" % self.unit)
        else:
            os.remove(self.unit)
            self.say("removed: %s (watcher disabled)" % self.unit)
        self.do("daemon-reload", ["systemctl", "--user", "daemon-reload"], check=False)
        return 0

    def logs(self):
        return "journalctl --user -u herdr-notify -f"


def backend(run=_run, which=shutil.which, env=None, dry_run=False, out=None):
    """launchd where launchctl exists; a *live* systemd user manager next;
    otherwise None (the watcher can still be run by hand)."""
    env = os.environ if env is None else env
    if which("launchctl"):
        return Launchd(run, dry_run, out)
    if which("systemctl") and env.get("XDG_RUNTIME_DIR"):
        _, state, _ = run(["systemctl", "--user", "is-system-running"], 5)
        if state.strip() in ("running", "degraded"):
            return Systemd(run, dry_run, out)
    return None


# ---------------------------------------------------------------- remote PATH
# SSH and Mosh run TENDRIL's remote command in a non-interactive shell:
# sshd runs `$SHELL -c` (zsh reads only ~/.zshenv, bash only ~/.bashrc) and
# the launcher's Mosh path runs `sh -lc` (reads only ~/.profile). On macOS
# none of those put ~/.local/bin or Homebrew on PATH, so `remote-agents` and
# `mosh-server` are "command not found". One managed block fixes both.
BEGIN = "# >>> tendril remote PATH (managed by tendril install) >>>"
END = "# <<< tendril remote PATH <<<"
BLOCK = BEGIN + """
for _tendril_d in /usr/local/bin /opt/homebrew/bin "$HOME/.local/bin"; do
    case ":$PATH:" in
        *":$_tendril_d:"*) ;;
        *) [ -d "$_tendril_d" ] && PATH="$_tendril_d:$PATH" ;;
    esac
done
unset _tendril_d
export PATH
""" + END + "\n"


def remote_rc_files(shell=None, every=False):
    """Files a non-interactive SSH/Mosh command reads for this user.
    every=True: all candidates, for removal after a login-shell change."""
    if every:
        return [home(".zshenv"), home(".bashrc"), home(".profile")]
    shell = os.path.basename(shell if shell is not None
                             else os.environ.get("SHELL", "/bin/sh"))
    files = [home(".profile")]                      # `sh -lc` (Mosh path)
    if shell == "zsh":
        files.insert(0, home(".zshenv"))            # `zsh -c` (sshd)
    elif shell == "bash":
        files.insert(0, home(".bashrc"))            # `bash -c` (sshd)
    return files


def remote_path_missing(names=("remote-agents",), shell=None, run=_run):
    """Commands a fresh SSH/Mosh command line would not find, for each of
    the two shells TENDRIL's remote command runs in. An approximation of
    sshd's environment: empty env + its default PATH."""
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    probe = "; ".join("command -v %s >/dev/null 2>&1 || echo %s" % (n, n)
                      for n in names)
    env = {"HOME": os.path.expanduser("~"), "PATH": SSHD_PATH,
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("USER", ""),
           "SHELL": shell, "SSH_CLIENT": "127.0.0.1 22 22"}
    missing = {}
    for label, argv in (("ssh", [shell, "-c", probe]),
                        ("mosh", ["/bin/sh", "-lc", probe])):
        rc, out, _ = run(argv, 8, env, socket_stdin=True)
        gone = [n for n in out.split() if n in names]
        if rc in (124, 126, 127):                 # hung, or no such shell
            gone = list(names)
        if gone:
            missing[label] = gone
    return missing


def strip_block(text):
    """Remove our exact block (and the blank line we put before it). An
    edited or half-deleted block is the user's now and is left alone."""
    return text.replace("\n" + BLOCK, "").replace(BLOCK, "")


def with_block(text):
    """text with exactly one, current managed block appended (idempotent)."""
    if BLOCK in text:
        return text
    base = text
    if base and not base.endswith("\n"):
        base += "\n"
    return base + ("\n" if base else "") + BLOCK


def edit_rc(path, remove=False, dry_run=False, out=None, stamp=None):
    """Add/refresh (or remove) the managed block; back the file up first.
    Never follows a symlinked rc file's target by replacing the link."""
    out = out or sys.stdout
    try:
        with open(path, "rb") as f:                 # any bytes survive intact
            text = f.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        text = None
    except OSError as e:
        print("  skipped %s: %s" % (path, e.strerror), file=out)
        return False
    current = text or ""
    new = strip_block(current) if remove else with_block(current)
    if remove and text is None:
        return False
    if new == current:
        return False
    if dry_run:
        print("  [dry-run] %s managed PATH block in %s"
              % ("remove" if remove else "write", path), file=out)
        return True
    if text is not None:
        shutil.copy2(path, "%s.bak.%d" % (path, stamp or int(time.time())))
    real = os.path.realpath(path)                   # edit the link's target
    mode = os.stat(real).st_mode & 0o777 if text is not None else 0o644
    _atomic_write(real, new.encode("utf-8", "surrogateescape"), mode)
    print("  %s managed PATH block in %s" % ("removed" if remove else "wrote", path),
          file=out)
    return True


# ---------------------------------------------------------------- helpers
def _atomic_write(path, data, mode):
    """Replace path (a symlink there is replaced, never followed; edit_rc
    resolves rc-file links itself on purpose, for dotfile managers)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tendril-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    words = [a for a in argv if not a.startswith("--")
             and a not in (_opt(argv, "--pkg"), _opt(argv, "--python"))]
    if words[:1] == ["facts"]:
        print(json.dumps(facts(), indent=2))
        return 0
    if words[:1] == ["hostname"]:
        print(default_name())
        return 0
    if words[:1] == ["tailscale-ip"]:
        ip = tailscale_ip()
        if ip:
            print(ip)
        return 0 if ip else 1
    if words[:1] == ["ssh-server"]:
        return 0 if ssh_server_listening() else 1
    if words[:1] == ["service"] and len(words) == 2:
        verb = words[1]
        b = backend(dry_run=dry)
        if b is None:
            print("no service manager: neither launchctl nor a running "
                  "`systemctl --user` session. Run the watcher by hand: "
                  "herdr-notify")
            return 3
        if verb == "status":
            print("%s (%s)" % (b.status(), b.name))
            return 0
        if verb == "logs":
            print(b.logs())
            return 0
        if verb == "install":
            pkg = _opt(argv, "--pkg", os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
            py = _opt(argv, "--python", sys.executable)
            return 0 if b.install(pkg, py) == 0 else 1
        if verb in ("start", "stop", "restart", "uninstall"):
            return 0 if getattr(b, verb)() == 0 else 1
    if words[:1] == ["remote-path"] and len(words) == 2:
        verb = words[1]
        if verb == "check":
            names = ["remote-agents"]
            if shutil.which("mosh-server"):           # Mosh is set up here
                names.append("mosh-server")
            missing = remote_path_missing(tuple(names))
            for label, names in sorted(missing.items()):
                print("%s: %s not on PATH" % (label, " ".join(names)))
            return 1 if missing else 0
        if verb in ("plan", "apply", "remove"):
            # plan: show what apply would change; exit 1 when nothing would
            changed = False
            for path in remote_rc_files(every=(verb == "remove")):
                changed |= edit_rc(path, remove=(verb == "remove"),
                                   dry_run=dry or verb == "plan")
            return 1 if verb == "plan" and not changed else 0
    print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
