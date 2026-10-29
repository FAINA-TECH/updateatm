#!/usr/bin/env python3
"""
Backward-compatibility guard for this fleet's config drift.

device_configs/*.py are the REAL, currently-deployed per-device globals.py
files (what update_global_file() serves to each device over OTA) - they
update independently of, and often lag behind, the firmware .py files. Any
firmware code that reads a NEW globals.NAME directly (not via getattr/
hasattr, and not already defaulted by the firmware itself writing
globals.NAME = ... before any read) will raise AttributeError on every real
device whose config predates that variable, the moment the new firmware
reaches it.

Run this before pushing new firmware files to the update server. Exit code
1 means: some live device's config is missing a variable the new code
reads unguarded - fix the code (getattr with a default, or a module-level
`if not hasattr(globals, 'NAME'): globals.NAME = default`), or push updated
configs to those devices FIRST, before the firmware that needs them.

Known limitation: this is a line-based heuristic, not a real parser. It
strips '#' comments and triple-quoted docstrings, and treats any
`globals.NAME = ...` write (anywhere, even conditional) as proof the
firmware guarantees NAME exists before use - but it can't see control flow,
so a write that's reachable only in some code paths is still treated as a
guarantee. Treat its output as a strong lead, not gospel.
"""
import re
import sys
import glob
import os

READ_RE = re.compile(r'globals\.([A-Z_][A-Z0-9_]*)')
WRITE_RE = re.compile(r'globals\.([A-Z_][A-Z0-9_]*)\s*=(?!=)')
GETATTR_RE = re.compile(r'(?:getattr|hasattr)\(\s*globals\s*,\s*[\'"]([A-Z_][A-Z0-9_]*)[\'"]')
ASSIGN_RE = re.compile(r'^([A-Z_][A-Z0-9_]*)\s*=')

def strip_comment(line):
    # Naive: good enough here since no real globals.NAME reference in this
    # codebase appears after a '#' inside a string literal on the same line.
    idx = line.find('#')
    return line if idx == -1 else line[:idx]

def iter_code_lines(path):
    """Yields (lineno, line) for lines outside triple-quoted docstrings/strings and comments."""
    in_triple = None
    with open(path, 'r') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw
            if in_triple:
                end = line.find(in_triple)
                if end == -1:
                    continue
                line = line[end + 3:]
                in_triple = None
            for quote in ('"""', "'''"):
                while quote in line:
                    start = line.find(quote)
                    end = line.find(quote, start + 3)
                    if end == -1:
                        line = line[:start]
                        in_triple = quote
                        break
                    line = line[:start] + line[end + 3:]
            if in_triple:
                yield lineno, strip_comment(line)
                continue
            yield lineno, strip_comment(line)

def find_unguarded_reads(repo_dir):
    reads = {}        # name -> set of (file, lineno)
    self_defaulted = set()  # names the firmware itself writes before any read
    paths = [p for p in glob.glob(os.path.join(repo_dir, '*.py'))
             if os.path.basename(p) not in ('globals.py', 'check_globals_compat.py')]

    for path in paths:
        for _, line in iter_code_lines(path):
            self_defaulted.update(WRITE_RE.findall(line))

    for path in paths:
        fname = os.path.basename(path)
        for lineno, line in iter_code_lines(path):
            guarded_here = set(GETATTR_RE.findall(line))
            written_here = set(WRITE_RE.findall(line))
            for name in READ_RE.findall(line):
                if name in guarded_here or name in written_here or name in self_defaulted:
                    continue
                reads.setdefault(name, set()).add((fname, lineno))
    return reads

def find_device_config_names(repo_dir):
    configs = {}  # device_file -> set of defined names
    for path in glob.glob(os.path.join(repo_dir, 'device_configs', '*.py')):
        names = set()
        for _, line in iter_code_lines(path):
            m = ASSIGN_RE.match(line.lstrip())
            if m:
                names.add(m.group(1))
        configs[os.path.basename(path)] = names
    return configs

def main():
    repo_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    unguarded = find_unguarded_reads(repo_dir)
    configs = find_device_config_names(repo_dir)

    if not configs:
        print("No device_configs/*.py found under {} - nothing to check against.".format(repo_dir))
        return 0

    failures = []
    for name, sites in sorted(unguarded.items()):
        missing = [dev for dev, names in configs.items() if name not in names]
        if missing:
            failures.append((name, sites, missing))

    if not failures:
        print("OK: every unguarded globals.NAME read is present in all {} device configs.".format(len(configs)))
        return 0

    print("UNSAFE: these firmware files read a global directly (no getattr/hasattr default,")
    print("no firmware-side self-default) that is MISSING from one or more real, deployed")
    print("device configs:\n")
    for name, sites, missing in failures:
        site_str = ', '.join('{}:{}'.format(f, l) for f, l in sorted(sites))
        print("  globals.{}".format(name))
        print("    read (unguarded) at: {}".format(site_str))
        print("    missing from {}/{} device configs: {}".format(
            len(missing), len(configs), ', '.join(sorted(missing)[:8]) + (' ...' if len(missing) > 8 else '')))
        print()
    print("Fix: wrap these reads in getattr(globals, 'NAME', <default>), or push")
    print("updated device_configs to the listed devices before shipping this firmware.")
    return 1

if __name__ == '__main__':
    sys.exit(main())
