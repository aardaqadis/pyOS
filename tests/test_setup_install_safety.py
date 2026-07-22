import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import setup
from pyos_config import verify_storage_owner


class SetupInstallSafetyTests(unittest.TestCase):
    def test_setup_requires_a_paramiko_release_after_the_vulnerable_line(self):
        paramiko = [package for package in setup.PYTHON_PACKAGES
                    if package.casefold().startswith("paramiko")]
        self.assertEqual(paramiko, ["paramiko>=5.0,<6.0"])

    def test_explicit_empty_optional_app_selection_stays_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = setup.InstallerCore(
                root / "install", root / "data", root / "downloads",
                enabled_apps=[], dry_run=True, logger=lambda _message: None,
            )
            self.assertEqual(installer.enabled_apps, [])

    def test_every_overlapping_location_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (root / "same", root / "same", root / "downloads"),
                (root / "install", root / "install" / "data", root / "downloads"),
                (root / "data" / "install", root / "data", root / "downloads"),
                (root / "install", root / "data", root / "data" / "downloads"),
                (root / "install", root / "downloads" / "data", root / "downloads"),
            )
            for locations in cases:
                with self.subTest(locations=locations):
                    installer = setup.InstallerCore(
                        *locations, install_vlc=False, install_ollama=False,
                        create_shortcuts=False, dry_run=True, logger=lambda _message: None,
                    )
                    with self.assertRaisesRegex(ValueError, "must not"):
                        installer.validate()

    def test_nonempty_unowned_install_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            (install / "someone-elses-file.txt").write_text("keep", encoding="utf-8")
            installer = setup.InstallerCore(
                install, root / "data", root / "downloads", install_vlc=False,
                install_ollama=False, create_shortcuts=False, dry_run=True,
                logger=lambda _message: None,
            )
            with self.assertRaisesRegex(ValueError, "not owned"):
                installer.validate()

    def test_managed_roots_reject_home_and_its_ancestors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for unsafe in (Path.home(), Path.home().parent, Path(Path.home().anchor)):
                with self.subTest(unsafe=unsafe):
                    installer = setup.InstallerCore(
                        unsafe, root / "data", root / "downloads",
                        install_vlc=False, install_ollama=False,
                        create_shortcuts=False, dry_run=True,
                        logger=lambda _message: None,
                    )
                    with self.assertRaisesRegex(ValueError, "unsafe"):
                        installer.validate()

    def test_nonempty_unowned_data_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            unrelated = data / "unrelated.txt"
            unrelated.write_text("keep", encoding="utf-8")
            installer = setup.InstallerCore(
                root / "install", data, root / "downloads",
                install_vlc=False, install_ollama=False,
                create_shortcuts=False, dry_run=True, logger=lambda _message: None,
            )

            with self.assertRaisesRegex(ValueError, "ownership marker"):
                installer.validate()

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def _partial_owned_install(self, root):
        install, data, downloads = root / "install", root / "data", root / "downloads"
        installer = setup.InstallerCore(
            install, data, downloads, install_vlc=False, install_ollama=False,
            create_shortcuts=False, logger=lambda _message: None,
        )
        installer.validate()
        installer.create_directories()
        installer._claim_owned("owned.txt")
        installer._claim_owned_tree(".venv")
        (install / "owned.txt").write_text("owned", encoding="utf-8")
        (install / ".venv").mkdir()
        (install / ".venv" / "library.bin").write_bytes(b"owned")
        return installer

    def test_upgrade_refuses_existing_target_absent_from_prior_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self._partial_owned_install(root)
            collision = original.install_dir / "pyOSgui.py"
            collision.write_text("personal file", encoding="utf-8")
            upgrade = setup.InstallerCore(
                original.install_dir, original.data_dir, original.downloads_dir,
                install_vlc=False, install_ollama=False, create_shortcuts=False,
                dry_run=True, logger=lambda _message: None,
            )

            with self.assertRaisesRegex(ValueError, "absent from the prior manifest"):
                upgrade.validate()

            self.assertEqual(collision.read_text(encoding="utf-8"), "personal file")

    def test_manifest_uninstall_preserves_unknown_files_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = self._partial_owned_install(root)
            unknown = installer.install_dir / "personal-notes.txt"
            unknown.write_text("keep", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            config_backup = root / "config.json.bak"
            config_backup.write_text("{}", encoding="utf-8")
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({
                "configured": True,
                "install_dir": str(installer.install_dir),
                "data_dir": str(installer.data_dir),
                "downloads_dir": str(installer.downloads_dir),
            }), encoding="utf-8")

            result = setup.uninstall_managed_install(installer.install_dir, config, legacy)

            self.assertGreaterEqual(result["removed"], 4)
            self.assertTrue(unknown.is_file())
            self.assertFalse((installer.install_dir / "owned.txt").exists())
            self.assertFalse((installer.install_dir / ".venv").exists())
            self.assertFalse((installer.install_dir / setup.OWNERSHIP_MARKER).exists())
            self.assertFalse((installer.install_dir / setup.INSTALL_MANIFEST).exists())
            self.assertFalse(config.exists())
            self.assertFalse(config_backup.exists())
            self.assertFalse(legacy.exists())
            self.assertTrue((installer.data_dir / "Drive_B").is_dir())
            self.assertTrue(verify_storage_owner(installer.data_dir, kind="data"))
            owner = json.loads((installer.data_dir / ".pyos-owner.json").read_text(encoding="utf-8"))
            self.assertIn("Drive_B", owner["owned_paths"])

    def test_failed_payload_removal_keeps_config_and_ownership_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = self._partial_owned_install(root)
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            config_backup = root / "config.json.bak"
            config_backup.write_text("{}", encoding="utf-8")
            real_rmtree = setup.shutil.rmtree

            def fail_owned_tree(path, *args, **kwargs):
                if Path(path).name == ".venv":
                    raise OSError("simulated sharing violation")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(setup.shutil, "rmtree", side_effect=fail_owned_tree):
                with self.assertRaisesRegex(OSError, "sharing violation"):
                    setup.uninstall_managed_install(installer.install_dir, config)

            self.assertTrue(config.is_file())
            self.assertTrue(config_backup.is_file())
            self.assertTrue((installer.install_dir / setup.OWNERSHIP_MARKER).is_file())
            self.assertTrue((installer.install_dir / setup.INSTALL_MANIFEST).is_file())

    def test_owned_file_replaced_by_directory_is_never_removed_recursively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = self._partial_owned_install(root)
            owned_file = installer.install_dir / "owned.txt"
            owned_file.unlink()
            owned_file.mkdir()
            nested = owned_file / "unrelated.txt"
            nested.write_text("keep", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "recursively own"):
                setup.uninstall_managed_install(
                    installer.install_dir, config, root / "legacy.json"
                )

            self.assertTrue(nested.is_file())
            self.assertTrue(config.is_file())
            self.assertTrue((installer.install_dir / setup.OWNERSHIP_MARKER).is_file())

    def test_unrelated_or_malformed_legacy_config_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.json"
            legacy.write_text("not json", encoding="utf-8")
            self.assertFalse(setup._matching_legacy_config(legacy, root / "install"))
            legacy.write_text(json.dumps({
                "configured": True,
                "install_dir": str(root / "other"),
                "data_dir": str(root / "data"),
                "downloads_dir": str(root / "downloads"),
            }), encoding="utf-8")
            self.assertFalse(setup._matching_legacy_config(legacy, root / "install"))
            self.assertTrue(legacy.is_file())


if __name__ == "__main__":
    unittest.main()
