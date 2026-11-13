#!/usr/bin/env python3
"""The build selection for every matrix image, spelled out. No build, no configuration read."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cmake"))
import dg_select  # noqa: E402  (cmake/dg_select.py)

R = "/repo"  # select() only builds strings from the root; nothing under it is opened except sdkconfig.local.
S3, P4_BASE, P4 = "sdkconfig.defaults", f"{R}/boards/common/sdkconfig.defaults", f"{R}/boards/p4xc/sdkconfig.defaults"
P4_AEC = f"{R}/boards/p4xc/sdkconfig.defaults.aec"
P4_ARGS = ("esp32p4", f"{R}/dependencies.p4xc.lock")
# name: (environment, defaults chain, generated config, default build directory, (target, lock))
MATRIX = {
    "s3": (dict(DG_BOARD="echoear", DG_DEMO_AUTOSTART="0"), [S3, "sdkconfig.defaults.echoear"], f"{R}/sdkconfig", "", ("", "")),
    "s3-default-board": ({}, [S3, "sdkconfig.defaults.echoear"], f"{R}/sdkconfig", "", ("", "")),
    "s3-demo": (dict(DG_BOARD="echoear", DG_DEMO_AUTOSTART="1"), [S3, "sdkconfig.defaults.echoear", "sdkconfig.defaults.demo"],
                f"{R}/build/sdkconfig-demo", "", ("", "")),
    # DG_AEC alone does not select the S3 AEC overlay; CLAUDE.md's explicit chain does.
    "s3-aec-flag-only": (dict(DG_BOARD="echoear", DG_AEC="1"), [S3, "sdkconfig.defaults.echoear"], f"{R}/sdkconfig", "", ("", "")),
    "p4": (dict(DG_BOARD="p4xc", DG_AEC="0", DG_DEMO_AUTOSTART="0"), [P4_BASE, P4], f"{R}/sdkconfig", f"{R}/build-p4xc", P4_ARGS),
    "p4-demo": (dict(DG_BOARD="p4xc", DG_AEC="0", DG_DEMO_AUTOSTART="1"), [P4_BASE, P4, "sdkconfig.defaults.demo"],
                f"{R}/sdkconfig", f"{R}/build-p4xc-demo", P4_ARGS),
    "p4-aec": (dict(DG_BOARD="p4xc", DG_AEC="1", DG_DEMO_AUTOSTART="0"), [P4_BASE, P4, P4_AEC], f"{R}/sdkconfig",
               f"{R}/build-p4xc-aec", P4_ARGS),
    "p4-aec-demo": (dict(DG_BOARD="p4xc", DG_AEC="1", DG_DEMO_AUTOSTART="1"), [P4_BASE, P4, P4_AEC, "sdkconfig.defaults.demo"],
                    f"{R}/sdkconfig", f"{R}/build-p4xc-aec-demo", P4_ARGS),
    "ws146": (dict(DG_BOARD="waveshare_146"), [S3, "sdkconfig.defaults.waveshare"], f"{R}/sdkconfig", f"{R}/build-ws146",
              ("esp32s3", f"{R}/dependencies.waveshare146.lock")),
    "s3-duo": (dict(DG_BOARD="echoear", DG_VARIANT="duo"),
               [S3, "sdkconfig.defaults.echoear", "sdkconfig.defaults.demo", "sdkconfig.defaults.echoear-aec", "sdkconfig.defaults.duo"],
               f"{R}/build/sdkconfig-demo", f"{R}/build-echoear-duo", ("", "")),
    "s3-duo-public": (dict(DG_BOARD="echoear", DG_VARIANT="duo", DG_PUBLIC_DEMO="1"),
                      [S3, "sdkconfig.defaults.echoear", "sdkconfig.defaults.demo", "sdkconfig.defaults.echoear-aec",
                       "sdkconfig.defaults.duo", "sdkconfig.defaults.public-demo"],
                      f"{R}/build/sdkconfig-demo", f"{R}/build-echoear-duo-public", ("", "")),
    "s3-demo-public": (dict(DG_BOARD="echoear", DG_DEMO_AUTOSTART="1", DG_PUBLIC_DEMO="1"),
                       [S3, "sdkconfig.defaults.echoear", "sdkconfig.defaults.demo", "sdkconfig.defaults.public-demo"],
                       f"{R}/build/sdkconfig-demo", f"{R}/build-public", ("", "")),
    "p4-duo": (dict(DG_BOARD="p4xc", DG_VARIANT="duo"), [P4_BASE, P4, P4_AEC, "sdkconfig.defaults.demo", "sdkconfig.defaults.duo"],
               f"{R}/sdkconfig", f"{R}/build-p4xc-duo", P4_ARGS),
}
REFUSED = {
    "Unsupported DG_BOARD": [dict(DG_BOARD="bogus")],
    "Unsupported DG_VARIANT": [dict(DG_VARIANT="trio"), dict(DG_BOARD="p4xc", DG_VARIANT="Duo")],
    "duo supports only echoear and p4xc": [dict(DG_BOARD="waveshare_146", DG_VARIANT="duo"), dict(DG_BOARD="bogus", DG_VARIANT="duo")],
    "waveshare_146 supports neither demo autostart nor AEC": [dict(DG_BOARD="waveshare_146", DG_AEC="1"),
                                                             dict(DG_BOARD="waveshare_146", DG_DEMO_AUTOSTART="1")],
    "DG_PUBLIC_DEMO=1 requires DG_DEMO_AUTOSTART=1 (or DG_VARIANT=duo)": [dict(DG_PUBLIC_DEMO="1"),
                                                                         dict(DG_BOARD="p4xc", DG_AEC="1", DG_PUBLIC_DEMO="1")],
}


class Selection(unittest.TestCase):
    def test_every_matrix_image(self):
        for name, (environ, defaults, config, build, (target, lock)) in MATRIX.items():
            got = dg_select.select(environ, root=R, project=R)
            self.assertEqual((got["defaults"], got["config"], got["build"], got["target"], got["lock"]),
                             (defaults, config, build, target, lock), name)
            duo = environ.get("DG_VARIANT") == "duo"
            self.assertEqual(got["exports"], {"DG_DEMO_AUTOSTART": "1", "DG_AEC": "1"} if duo else {}, name)

    def test_refusals_keep_their_wording(self):
        for message, environments in REFUSED.items():
            for environ in environments:
                with self.assertRaises(dg_select.SelectionError) as refused:
                    dg_select.select(environ, root=R, project=R)
                self.assertEqual(str(refused.exception), message, environ)

    def test_local_overrides_come_second_from_the_project_else_from_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project = Path(directory) / "root", Path(directory) / "elsewhere"
            root.mkdir(); project.mkdir()
            base = [f"{root}/boards/common/sdkconfig.defaults", f"{root}/boards/p4xc/sdkconfig.defaults"]
            p4 = dict(DG_BOARD="p4xc")
            self.assertEqual(dg_select.select(p4, root=root, project=project)["defaults"], base)
            (root / "sdkconfig.local").write_text("")  # Existence only; never read.
            self.assertEqual(dg_select.select(p4, root=root, project=project)["defaults"],
                             [base[0], f"{root}/sdkconfig.local", base[1]])
            self.assertEqual(dg_select.select({}, root=root, project=root)["defaults"],
                             ["sdkconfig.defaults", "sdkconfig.local", "sdkconfig.defaults.echoear"])
            (project / "sdkconfig.local").write_text("")
            self.assertEqual(dg_select.select(p4, root=root, project=project)["defaults"], [base[0], "sdkconfig.local", base[1]])

    def test_shell_form_is_what_idf_sh_evaluates(self):
        text = dg_select.shell(dg_select.select(MATRIX["p4-duo"][0], root=R, project=R))
        script = text + 'printf "%s|%s|%s|%s|%s|%s" "$defaults" "$config_file" "$default_build" "${board_args[*]}" "$DG_AEC" "$board"'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, env={}).stdout
        _, defaults, config, build, (target, lock) = MATRIX["p4-duo"]
        self.assertEqual(out, f"{';'.join(defaults)}|{config}|{build}|-DIDF_TARGET={target} -DDEPENDENCIES_LOCK={lock}|1|p4xc")
        plain = dg_select.shell(dg_select.select(MATRIX["s3"][0], root=R, project=R))
        self.assertIn("board_args=()\n", plain)
        self.assertIn("default_build=''\n", plain)
        self.assertNotIn("export ", plain)
        spaced = dg_select.shell(dg_select.select(dict(DG_BOARD="p4xc"), root="/re po", project="/re po"))
        out = subprocess.run(["bash", "-c", spaced + 'printf "%s" "${board_args[1]}"'], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "-DDEPENDENCIES_LOCK=/re po/dependencies.p4xc.lock")

    def test_cmake_form_keeps_an_owned_build_directory_s_config_inside_it(self):
        owned = dg_select.cmake(dg_select.select(MATRIX["p4-aec"][0], root=R, project=R), "/somewhere/build-x")
        self.assertIn('set(dg_selected_config "/somewhere/build-x/sdkconfig")\n', owned)
        self.assertIn(f'set(SDKCONFIG_DEFAULTS "{";".join(MATRIX["p4-aec"][1])}")\n', owned)
        self.assertIn('set(dg_selected_target "esp32p4")\n', owned)
        legacy = dg_select.cmake(dg_select.select(MATRIX["s3-demo"][0], root=R, project=R), "/somewhere/build")
        self.assertIn(f'set(dg_selected_config "{R}/build/sdkconfig-demo")\n', legacy)
        self.assertIn('set(dg_selected_target "")\n', legacy)
        duo = dg_select.cmake(dg_select.select(MATRIX["s3-duo"][0], root=R, project=R), "/b")
        self.assertIn('set(ENV{DG_AEC} "1")\n', duo)

    def test_command_line_reports_a_refusal_on_stderr_with_status_one(self):
        script = Path(dg_select.__file__)
        run = subprocess.run([sys.executable, str(script), "--shell"], capture_output=True, text=True,
                             env={"DG_BOARD": "bogus"})
        self.assertEqual((run.returncode, run.stdout, run.stderr), (1, "", "Unsupported DG_BOARD\n"))


class ConfigureInputs(unittest.TestCase):
    def test_a_change_to_the_resolver_reconfigures_the_image(self):
        # CMake runs this file to get the selection. Unless it is a configure input, a plain
        # idf.py build keeps the selection it had before the edit. The proof is a build
        # (touch the resolver, and the next build must re-run CMake); this keeps the line in place.
        text = (Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text()
        self.assertIn('set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS '
                      '"${CMAKE_CURRENT_SOURCE_DIR}/cmake/dg_select.py")', text)


class SharedComponents(unittest.TestCase):
    def test_a_managed_component_that_changed_reconfigures_the_image(self):
        # managed_components/ is shared by every image and each target has its own lock. Without
        # these two lines an EchoEar image built after a P4 configure links the P4's
        # esp_audio_codec, with no error. The proof is a build; this keeps the lines in place.
        text = (Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text()
        self.assertIn('file(GLOB dg_component_hashes "${CMAKE_SOURCE_DIR}/managed_components/*/.component_hash")', text)
        self.assertIn("set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${dg_component_hashes})", text)
        # After project(): by then the component manager has put this image's versions on disk.
        self.assertLess(text.index("project(dg-esp32)"), text.index("dg_component_hashes"))


if __name__ == "__main__":
    unittest.main()
