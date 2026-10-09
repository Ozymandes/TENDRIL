#!/usr/bin/env python3
"""Safely plan and merge TENDRIL's Herdr detach bindings.

The installed Herdr CLI is the authority on key syntax. The installer probes a
candidate chord against an isolated config before asking this helper to add it.
"""
import os
import re
import subprocess
import sys
import tempfile

LEGACY_DETACH = "ctrl+]"
DEFAULT_DETACH = "prefix+q"
DETACH_KEYS = ("prefix+d", "alt+d", "ctrl+home")


def _uncomment(line):
    """Strip a TOML comment while respecting quoted strings."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _sections(text):
    section = ""
    for number, line in enumerate(text.splitlines(keepends=True)):
        clean = _uncomment(line).strip()
        match = re.fullmatch(r"\[([^]]+)\]", clean)
        if match:
            section = match.group(1).strip()
        yield number, section, line


def _quoted_values(value):
    return re.findall(r'"((?:\\.|[^"\\])*)"', value)


def _chords(value):
    return [item.lower() for item in _quoted_values(value)]


def _bracket_balance(value):
    balance = 0
    quote = None
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if ord(char) == 92 and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "[":
            balance += 1
        elif char == "]":
            balance -= 1
    return balance


def _detach_assignment(text):
    """Return (start, end, indent, entries) for [keys].detach, if present."""
    lines = list(text.splitlines(keepends=True))
    section = ""
    offset = 0
    for index, line in enumerate(lines):
        clean = _uncomment(line).strip()
        section_match = re.fullmatch(r"\[([^]]+)\]", clean)
        if section_match:
            section = section_match.group(1).strip()
            offset += len(line)
            continue
        if section == "keys":
            match = re.match(r"^([ \t]*)detach[ \t]*=[ \t]*(.*?)(?:\r?\n)?$", line)
            if match and not line.lstrip().startswith("#"):
                start = offset
                end = offset + len(line)
                value = _uncomment(match.group(2)).strip()
                # TOML permits a multiline array; replace only the assignment.
                if value.startswith("[") and _bracket_balance(value) > 0:
                    cursor = index + 1
                    while cursor < len(lines):
                        end += len(lines[cursor])
                        value += "\n" + _uncomment(lines[cursor]).strip()
                        if _bracket_balance(value) <= 0:
                            break
                        cursor += 1
                entries = _quoted_values(value)
                return start, end, match.group(1), entries
        offset += len(line)
    return None


def _conflicts(text):
    used = {}
    lines = list(_sections(text))
    index = 0
    while index < len(lines):
        _, section, line = lines[index]
        clean = _uncomment(line).strip()
        if not clean or "=" not in clean or clean.startswith("["):
            index += 1
            continue
        key, value = clean.split("=", 1)
        value = value.strip()
        if _bracket_balance(value) > 0:
            cursor = index + 1
            while cursor < len(lines) and _bracket_balance(value) > 0:
                value += " " + _uncomment(lines[cursor][2]).strip()
                cursor += 1
        action = (section + "." if section else "") + key.strip()
        for chord in _chords(value):
            used.setdefault(chord, set()).add(action)
        index += 1
    return used


def _toml_list(entries):
    return "detach = [" + ", ".join('"%s"' % entry for entry in entries) + "]"


def plan_detach(text, ctrl_home_supported):
    """Return a config-preserving detach merge plan."""
    assignment = _detach_assignment(text)
    existing = assignment[3] if assignment else []
    current = list(existing)
    used = _conflicts(text)
    own_action = "keys.detach"

    conflicts = {}
    candidates = DETACH_KEYS + (() if assignment else (DEFAULT_DETACH,))
    for chord in candidates:
        others = sorted(name for name in used.get(chord, ()) if name != own_action)
        if others:
            conflicts[chord] = others

    supported = bool(ctrl_home_supported)
    home_free = supported and "ctrl+home" not in conflicts
    migrated = False
    if home_free and any(chord.lower() == LEGACY_DETACH for chord in current):
        current = [chord for chord in current
                   if chord.lower() != LEGACY_DETACH]
        migrated = True

    added = []
    # Creating an explicit detach list replaces Herdr's implicit prefix+q default.
    # Preserve that default only when the user had no detach list to begin with.
    desired = [] if assignment else [DEFAULT_DETACH]
    desired.extend(("prefix+d", "alt+d"))
    if home_free:
        desired.append("ctrl+home")
    present = {chord.lower() for chord in current}
    for chord in desired:
        if chord not in present and chord not in conflicts:
            current.append(chord)
            present.add(chord)
            added.append(chord)

    changed = current != existing
    line = _toml_list(current) if changed else ""
    return {
        "text": text,
        "entries": current,
        "existing": existing,
        "added": added,
        "removed": [LEGACY_DETACH] if migrated else [],
        "conflicts": conflicts,
        "supported": supported,
        "changed": changed,
        "line": line,
    }


def apply_detach(text, plan):
    if not plan["changed"]:
        return text
    assignment = _detach_assignment(text)
    newline = plan["line"]
    if assignment:
        start, end, indent, _ = assignment
        ending = "\r\n" if text[max(start, end - 2):end] == "\r\n" else (
            "\n" if text[max(start, end - 1):end] == "\n" else "")
        replacement = indent + newline + ending
        return text[:start] + replacement + text[end:]

    offset = 0
    for _, section, line in _sections(text):
        if section == "keys" and _uncomment(line).strip() == "[keys]":
            ending = "\r\n" if line.endswith("\r\n") else (
                "\n" if line.endswith("\n") else "\n")
            position = offset + len(line)
            return text[:position] + newline + ending + text[position:]
        offset += len(line)
    if not text:
        return "[keys]\n" + newline + "\n"
    prefix = text if text.endswith("\n") else text + "\n"
    return prefix + "\n[keys]\n" + newline + "\n"


# ---------------------------------------------------------------- switching
# Each proposal: (section, key, TOML value, human description). Scalars are
# only ever added when absent; list entries are appended. A proposal is
# skipped - never forced - when any chord it would bind is already claimed
# by another action (exact chords after range/modifier expansion, so
# alt+d never collides with alt+down).
SWITCH_PROPOSALS = (
    ("keys", "switch_workspace", '"prefix+1..9"', "prefix workspace switching (Ctrl+B 1..9)"),
    ("keys.indexed", "workspaces", '"alt"', "Alt indexed workspaces (Alt+1..9)"),
    ("keys.indexed", "tabs", '"ctrl"', "Ctrl indexed tabs (Ctrl+1..9)"),
    ("keys", "next_tab", '"alt+right"', "next tab on Alt+Right"),
    ("keys", "previous_tab", '"alt+left"', "previous tab on Alt+Left"),
    ("keys", "next_workspace", '"alt+down"', "next workspace on Alt+Down"),
)
# list keys keep Herdr's phone-safe default when we create them
LIST_DEFAULTS = {"next_tab": "prefix+n", "previous_tab": "prefix+p"}
INDEXED = "keys.indexed"


def _norm(chord):
    """Canonical chord: lowercase, modifiers sorted, key last."""
    parts = [p for p in chord.strip().lower().split("+") if p]
    if not parts:
        return ""
    key = parts[-1]
    mods = sorted(parts[:-1], key=lambda m: (m != "prefix", m))
    return "+".join(mods + [key])


def _expand(chord):
    """'alt+1..9' -> {'alt+1', ..., 'alt+9'}; anything else -> {itself}."""
    c = chord.strip().lower()
    m = re.fullmatch(r"(.*\+)?(\d)\.\.(\d)", c)
    if m:
        lo, hi = int(m.group(2)), int(m.group(3))
        return {_norm((m.group(1) or "") + str(d)) for d in range(lo, hi + 1)}
    return {_norm(c)}


def _claims(action, chord):
    """Chords bound by one config value: [keys.indexed] values are a
    modifier set for the digits 1..9."""
    if action.startswith(INDEXED + "."):
        return _expand(chord + "+1..9") if chord.strip() else set()
    return _expand(chord)


def claimed(text):
    """expanded chord -> set of actions that bind it in this config."""
    out = {}
    for chord, actions in _conflicts(text).items():
        for action in actions:
            for c in _claims(action, chord):
                out.setdefault(c, set()).add(action)
    return out


def _assignment(text, section, key):
    """(start, end, value) of section.key's single-line assignment."""
    offset = 0
    for _, sec, line in _sections(text):
        if sec == section and not line.lstrip().startswith("#"):
            m = re.match(r"^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*(.*?)(?:\r?\n)?$", line)
            if m:
                return offset, offset + len(line), _uncomment(m.group(1)).strip()
        offset += len(line)
    return None


def _insert(text, section, line):
    """Append `line` at the end of [section] (creating it at the end)."""
    offset, end_of_section = 0, None
    inside = False
    for _, sec, raw in _sections(text):
        header = re.fullmatch(r"\[([^]]+)\]", _uncomment(raw).strip())
        if header:
            if inside:
                break
            inside = header.group(1).strip() == section
        if inside:
            end_of_section = offset + len(raw)
            if raw.strip():                 # skip trailing blank lines
                last_content = end_of_section
        offset += len(raw)
    if end_of_section is None:
        base = text if not text or text.endswith("\n") else text + "\n"
        return base + ("\n" if base else "") + "[%s]\n%s\n" % (section, line)
    pos = last_content
    if not text[:pos].endswith("\n"):
        return text[:pos] + "\n" + line + "\n" + text[pos:]
    return text[:pos] + line + "\n" + text[pos:]


def _with(text, proposal):
    """(new_text, None) or (text, reason it does not apply)."""
    section, key, value, _ = proposal
    action = section + "." + key
    chords = _chords(value)
    taken = claimed(text)
    want = set()
    for c in chords:
        want |= _claims(action, c)
    clash = sorted({a for c in want for a in taken.get(c, ()) if a != action})
    found = _assignment(text, section, key)
    if key in LIST_DEFAULTS and found and _bracket_balance(found[2]) > 0:
        return text, "multi-line list; left as you wrote it"
    if key in LIST_DEFAULTS and found and found[2].startswith("["):
        have = {_norm(c) for c in _chords(found[2])}
        if want <= have:
            return text, "already present"
        if clash:
            return text, "conflict with " + ", ".join(clash)
        start, end, old = found
        new = old.rstrip()[:-1].rstrip()
        new = (new + (", " if new.rstrip("[").strip() else "")) + value + "]"
        line = text[start:end]
        return text[:start] + line.replace(old, new, 1) + text[end:], None
    if found:
        return text, "already set by you (%s = %s)" % (key, found[2])
    if clash:
        return text, "conflict with " + ", ".join(clash)
    if key in LIST_DEFAULTS:
        value = '["%s", %s]' % (LIST_DEFAULTS[key], value)
    return _insert(text, section, "%s = %s" % (key, value)), None


def herdr_issues(binary, text):
    """(rc, set of issue lines) from `herdr config check` on an isolated
    copy of text; never touches the live config."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        env = dict(os.environ, HERDR_CONFIG_PATH=path)
        try:
            r = subprocess.run([binary, "config", "check"], env=env,
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 125, {"herdr config check could not run: %s" % e}
    lines = {ln.strip() for ln in (r.stdout + r.stderr).splitlines()
             if ln.strip() and not ln.startswith("config:")}
    return r.returncode, lines


def plan_switch(text, binary=None):
    """Decide each proposal on its own. Returns (new_text, results) with
    results = [(action, description, 'ADD'|'SKIP', reason)]."""
    base_issues = herdr_issues(binary, text)[1] if binary else set()
    results = []
    for proposal in SWITCH_PROPOSALS:
        section, key, _, desc = proposal
        action = section + "." + key
        candidate, reason = _with(text, proposal)
        if reason is None and binary:
            _, issues = herdr_issues(binary, candidate)
            new = sorted(issues - base_issues)
            if new:
                reason = "rejected by herdr config check: " + new[0]
        if reason is None:
            text = candidate
            results.append((action, desc, "ADD", ""))
        else:
            results.append((action, desc, "SKIP", reason))
    return text, results


def digit_map(text):
    """What Ctrl+B/Alt/Ctrl + digit do under this config (Herdr's default
    switch_tab = prefix+1..9 applies when nothing claims those chords)."""
    taken = claimed(text)
    out = {}
    for label, mod in (("Ctrl+B 1..9", "prefix+"), ("Alt+1..9", "alt+"),
                       ("Ctrl+1..9", "ctrl+")):
        actions = sorted(taken.get(_norm(mod + "1"), ()))
        if not actions and mod == "prefix+":
            actions = ["keys.switch_tab"]
        out[label] = actions[0] if actions else ""
    return out


_MEANING = {"keys.switch_tab": "switch tab", "keys.indexed.tabs": "switch tab",
            "keys.switch_workspace": "switch workspace",
            "keys.indexed.workspaces": "switch workspace",
            "keys.focus_agent": "focus agent", "keys.indexed.agents": "focus agent"}


def describe_digits(text):
    return [(label, _MEANING.get(action, action.split(".")[-1]) if action else "unbound")
            for label, action in digit_map(text).items()]


def _write(path, text):
    if os.path.exists(path):
        d = os.path.dirname(os.path.abspath(path))
        mode = os.stat(path).st_mode & 0o777
        fd, tmp = tempfile.mkstemp(prefix=".tendril-", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, os.path.realpath(path) if os.path.islink(path) else path)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)


def switch_main(mode, path, binary):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if mode == "digits":
        for label, meaning in describe_digits(text):
            print("DIGITS=%s|%s" % (label, meaning))
        return 0
    new, results = plan_switch(text, binary or None)
    for action, desc, verdict, reason in results:
        print("%s=%s|%s|%s" % (verdict, action, desc, reason))
    if mode == "apply" and new != text:
        if binary:                         # final whole-file gate
            rc, issues = herdr_issues(binary, new)
            base = herdr_issues(binary, text)[1]
            if issues - base:
                print("REFUSED=" + sorted(issues - base)[0])
                return 3
        _write(path, new)
        print("WROTE=" + path)
    for label, meaning in describe_digits(new if mode == "apply" else text):
        print("DIGITS=%s|%s" % (label, meaning))
    return 0


def herdr_supports_chord(binary, chord):
    """Ask this installed Herdr CLI to validate a chord without touching config."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
        config.write('[keys]\ndetach = ["%s"]\n' % chord)
        config.flush()
        env = dict(os.environ)
        env["HERDR_CONFIG_PATH"] = config.name
        try:
            result = subprocess.run([binary, "config", "check"], env=env,
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return result.returncode == 0


def main(argv):
    if len(argv) == 4 and argv[1] == "supports":
        return 0 if herdr_supports_chord(argv[2], argv[3]) else 1
    # switch plan|apply|digits CONFIG [HERDR_BIN]
    if len(argv) in (4, 5) and argv[1] == "switch" and argv[2] in ("plan", "apply", "digits"):
        return switch_main(argv[2], argv[3], argv[4] if len(argv) == 5 else "")
    if len(argv) != 4 or argv[1] not in ("plan", "apply"):
        print("usage: herdr-bindings.py plan|apply CONFIG ctrl-home-supported(0|1)",
              file=sys.stderr)
        return 2
    mode, path, supported = argv[1], argv[2], argv[3]
    # Keep the final argument contract strict to avoid accidental unsafe edits.
    if supported not in ("0", "1"):
        print("ctrl-home-supported must be 0 or 1", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as config:
            text = config.read()
    except FileNotFoundError:
        text = ""
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    plan = plan_detach(text, supported == "1")
    if mode == "plan":
        print("SUPPORTED=" + ("1" if plan["supported"] else "0"))
        print("CONFLICT=" + ";".join(
            "%s:%s" % (key, ",".join(names))
            for key, names in sorted(plan["conflicts"].items())))
        print("EXISTING=" + ",".join(plan["existing"]))
        print("ADDED=" + ",".join(plan["added"]))
        print("REMOVED=" + ",".join(plan["removed"]))
        print("LINE=" + plan["line"])
    else:
        updated = apply_detach(text, plan)
        if updated != text:
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as config:
                    config.write(updated)
            else:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as config:
                    config.write(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
