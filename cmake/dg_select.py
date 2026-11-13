#!/usr/bin/env python3
"""The one place that turns DG_BOARD / DG_VARIANT / DG_AEC / DG_DEMO_AUTOSTART /
DG_PUBLIC_DEMO into a build selection: target, defaults chain, build directory,
generated-config path and dependency lock.

scripts/idf.sh and the top-level CMakeLists.txt both ask this file, so a plain
`idf.py` build and a wrapper build resolve the same image. Nothing here reads a
configuration file; `sdkconfig.local` is only tested for existence.

It lives under cmake/ because the bundle tools (hil/duo_bundle.py,
hil/make_bundle.py) count that directory among the firmware inputs: a change
to the selection is then a change they ask a rebuild for.

    dg_select.py --shell   assignments for scripts/idf.sh to eval
    dg_select.py --cmake   set() commands for CMakeLists.txt to evaluate
    dg_select.py --json    the same selection, for tests and tools
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]


class SelectionError(ValueError):
    pass


def select(environ, *, root=ROOT, project=None):
    """Resolve one image. Paths are spelled exactly as the builds have always passed them."""
    root, project = str(root), Path(project or root)
    board = environ.get("DG_BOARD") or "echoear"
    variant = environ.get("DG_VARIANT") or ""
    demo, aec = environ.get("DG_DEMO_AUTOSTART", "0") == "1", environ.get("DG_AEC", "0") == "1"
    exports = {}
    if variant == "duo":
        if board not in ("echoear", "p4xc"):
            raise SelectionError("duo supports only echoear and p4xc")
        demo = aec = True
        # main/Kconfig.projbuild and the component manifests read these from the environment.
        exports = {"DG_DEMO_AUTOSTART": "1", "DG_AEC": "1"}
    elif variant:
        raise SelectionError("Unsupported DG_VARIANT")

    # The P4 chain starts from the board-common base; the S3 boards keep the legacy root base.
    defaults = [f"{root}/boards/common/sdkconfig.defaults" if board == "p4xc" else "sdkconfig.defaults"]
    # Local overrides: the project's own sdkconfig.local, else the repository root's.
    if (project / "sdkconfig.local").is_file():
        defaults.append("sdkconfig.local")
    elif (Path(root) / "sdkconfig.local").is_file():
        defaults.append(f"{root}/sdkconfig.local")

    config, build, target, lock = f"{root}/sdkconfig", "", "", ""
    if board == "echoear":
        # The legacy S3 chain and config paths stay byte for byte.
        defaults.append("sdkconfig.defaults.echoear")
        if demo:
            config = f"{root}/build/sdkconfig-demo"
            defaults.append("sdkconfig.defaults.demo")
    elif board == "waveshare_146":
        if demo or aec:
            raise SelectionError("waveshare_146 supports neither demo autostart nor AEC")
        defaults.append("sdkconfig.defaults.waveshare")
        build, target, lock = f"{root}/build-ws146", "esp32s3", f"{root}/dependencies.waveshare146.lock"
    elif board == "p4xc":
        defaults.append(f"{root}/boards/p4xc/sdkconfig.defaults")
        build, target, lock = f"{root}/build-p4xc", "esp32p4", f"{root}/dependencies.p4xc.lock"
        if aec:
            defaults.append(f"{root}/boards/p4xc/sdkconfig.defaults.aec")
            build += "-aec"
        if demo:
            defaults.append("sdkconfig.defaults.demo")
            build += "-demo"
    else:
        raise SelectionError("Unsupported DG_BOARD")

    if variant == "duo":
        if board == "echoear":
            defaults.append("sdkconfig.defaults.echoear-aec")
        defaults.append("sdkconfig.defaults.duo")
        build = f"{root}/build-{board}-duo"
    if environ.get("DG_PUBLIC_DEMO", "0") == "1":
        if not demo:
            raise SelectionError("DG_PUBLIC_DEMO=1 requires DG_DEMO_AUTOSTART=1 (or DG_VARIANT=duo)")
        defaults.append("sdkconfig.defaults.public-demo")
        build = (build or f"{root}/build") + "-public"
    return dict(board=board, variant=variant, target=target, defaults=defaults, config=config,
                build=build, lock=lock, exports=exports)


def shell(selection):
    """Assignments for scripts/idf.sh. An image with its own build directory keeps its config there."""
    lines = [f"export {name}={shlex.quote(value)}" for name, value in selection["exports"].items()]
    board_args = []
    if selection["target"]:
        board_args = [f"-DIDF_TARGET={selection['target']}", f"-DDEPENDENCIES_LOCK={selection['lock']}"]
    lines += [f"board={shlex.quote(selection['board'])}",
              f"defaults={shlex.quote(';'.join(selection['defaults']))}",
              f"config_file={shlex.quote(selection['config'])}",
              f"default_build={shlex.quote(selection['build'])}",
              "board_args=(" + " ".join(shlex.quote(argument) for argument in board_args) + ")"]
    return "\n".join(lines) + "\n"


def cmake(selection, binary_dir):
    """set() commands for CMakeLists.txt. The config of an image with its own build directory lives in it."""
    def quoted(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    config = f"{binary_dir}/sdkconfig" if selection["build"] else selection["config"]
    lines = [f"set(SDKCONFIG_DEFAULTS {quoted(';'.join(selection['defaults']))})",
             f"set(dg_selected_config {quoted(config)})",
             f"set(dg_selected_target {quoted(selection['target'])})",
             f"set(dg_selected_lock {quoted(selection['lock'])})"]
    lines += [f"set(ENV{{{name}}} {quoted(value)})" for name, value in selection["exports"].items()]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    form = parser.add_mutually_exclusive_group(required=True)
    form.add_argument("--shell", action="store_true")
    form.add_argument("--cmake", action="store_true")
    form.add_argument("--json", action="store_true")
    parser.add_argument("--project", default=None, help="project directory searched first for sdkconfig.local")
    parser.add_argument("--binary-dir", default="", help="the CMake build directory (--cmake)")
    arguments = parser.parse_args(argv)
    try:
        selection = select(os.environ, project=arguments.project or os.getcwd())
    except SelectionError as error:
        print(error, file=sys.stderr)
        return 1
    if arguments.shell:
        sys.stdout.write(shell(selection))
    elif arguments.cmake:
        sys.stdout.write(cmake(selection, arguments.binary_dir))
    else:
        json.dump(selection, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
