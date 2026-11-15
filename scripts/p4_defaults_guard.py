#!/usr/bin/env python3
"""Refuse a P4 port action on one of this repository's builds made from another selection.

    p4_defaults_guard.py <repository> <build directory> <expected SDKCONFIG_DEFAULTS>

scripts/idf.sh calls this before a P4 flash or monitor. It reads only the
build's project_description.json, never a configuration file. Paths are
compared resolved, so a symlinked checkout or a build started from another
directory is the same build. A build of another project is not compared.
An empty override, or a build that recorded no defaults, matches nothing and
is refused: the wrapper never builds a P4 image without a defaults chain.
"""
import json
import os
import sys


def identity(path):
    """The file itself where it exists (a case-insensitive volume spells one file many ways)."""
    try:
        status = os.stat(path)
        return (status.st_dev, status.st_ino)
    except OSError:
        return os.path.realpath(path)


def chain(defaults, base):
    entries = defaults if isinstance(defaults, list) else defaults.split(";")
    return [os.path.realpath(os.path.join(base, entry)) for entry in entries if entry]


def main(argv):
    root, build, expected = argv
    with open(os.path.join(build, "project_description.json")) as file:
        description = json.load(file)
    project = description["project_path"]
    if identity(project) != identity(root):
        return 0
    built, wanted = chain(description.get("config_defaults", ""), project), chain(expected, root)
    if built and [identity(entry) for entry in built] == [identity(entry) for entry in wanted]:
        return 0
    print(f"Refusing {build}: built with SDKCONFIG_DEFAULTS={';'.join(built)},", file=sys.stderr)
    print(f"  but this invocation gives {';'.join(wanted)}.", file=sys.stderr)
    local = os.path.realpath(os.path.join(root, "sdkconfig.local"))
    if built and [entry for entry in built if entry != local] == [entry for entry in wanted if entry != local]:
        print("  sdkconfig.local was added or removed since this build; rebuild it.", file=sys.stderr)
    else:
        print("  Re-run with the DG_* variables (and any -D SDKCONFIG_DEFAULTS=...) used for the build.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
