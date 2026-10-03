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
