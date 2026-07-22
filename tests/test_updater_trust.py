import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyos_updater


REF = "a" * 40


class UpdaterTrustTests(unittest.TestCase):
    def _source_archive(self, root, files):
        archive = root / "source.zip"
        files = dict(files)
        for name in pyos_updater.REQUIRED_SOURCE_FILES:
            files.setdefault(name, f"updated {name}\n")
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, contents in files.items():
                bundle.writestr(f"pyOS-{REF}/{name}", contents)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, digest

    def _update(self, digest):
        return {
            "channel": "stable",
            "ref": REF,
            "archive_url": f"https://updates.invalid/archive/{REF}.zip",
            "digest": f"sha256:{digest}",
        }

    @staticmethod
    def _trust_source(digest):
        return mock.patch.object(
            pyos_updater,
            "TRUSTED_SOURCE_RELEASE_BINDINGS",
            frozenset({(REF, digest)}),
        )

    @staticmethod
    def _download_from(archive):
        def download(_url, destination, _progress=None):
            shutil.copy2(archive, destination)
            return hashlib.sha256(archive.read_bytes()).hexdigest()
        return download

    def _interrupted_source_transaction(self, root):
        install, data = root / "install", root / "data"
        install.mkdir()
        data.mkdir()
        transaction_id = uuid.uuid4().hex
        backup = data / "update_backups" / transaction_id
        backup.mkdir(parents=True)

        current = install / "pyOSgui.py"
        current.write_text("new gui", encoding="utf-8")
        saved = backup / "pyOSgui.py"
        saved.write_text("old gui", encoding="utf-8")

        created_file = install / "newdir" / "added.py"
        created_file.parent.mkdir()
        created_file.write_text("new nested file", encoding="utf-8")
        preexisting_dir = install / "preexisting"
        preexisting_dir.mkdir()
        preexisting_file = preexisting_dir / "added.py"
        preexisting_file.write_text("new file", encoding="utf-8")

        def entry(path, existed, backup_path=None):
            return {
                "path": path.relative_to(install).as_posix(),
                "existed": existed,
                "installed_sha256": pyos_updater._sha256_file(path),
                "backup_sha256": (
                    pyos_updater._sha256_file(backup_path) if backup_path else ""
                ),
            }

        entries = [
            entry(current, True, saved),
            entry(created_file, False),
            entry(preexisting_file, False),
        ]
        journal_path = backup / "transaction.json"
        journal = {
            "version": pyos_updater.SOURCE_TRANSACTION_VERSION,
            "transaction_id": transaction_id,
            "state": "applying",
            "install_dir": str(install.resolve()),
            "ref": REF,
            "sha256": "1" * 64,
            "entries": entries,
            "applied": [value["path"] for value in entries],
            "created_dirs": ["newdir"],
        }
        pyos_updater._atomic_write_json(journal_path, journal)
        active_path = data / pyos_updater.SOURCE_TRANSACTION_FILE
        pyos_updater._atomic_write_json(active_path, {
            "version": pyos_updater.SOURCE_TRANSACTION_VERSION,
            "transaction_id": transaction_id,
            "install_dir": str(install.resolve()),
            "journal": journal_path.relative_to(data).as_posix(),
        })
        return install, data, journal_path, active_path

    def test_stable_metadata_resolves_tag_and_propagates_asset_trust(self):
        source_digest = "1" * 64
        executable_digest = "2" * 64
        release = {
            "tag_name": "v2.0",
            "name": "pyOS 2.0",
            "assets": [
                {"id": 10, "name": "pyOS-source.zip", "size": 100,
                 "browser_download_url": "https://example.invalid/source.zip",
                 "digest": f"sha256:{source_digest}"},
                {"id": 11, "name": "pyOS.exe", "size": 200,
                 "browser_download_url": "https://example.invalid/pyOS.exe",
                 "digest": f"sha256:{executable_digest}"},
            ],
        }

        def request(url):
            return release if url.endswith("/releases/latest") else {"sha": REF, "commit": {}}

        with mock.patch.object(pyos_updater, "_json_request", side_effect=request):
            update = pyos_updater.latest_update("stable")
        self.assertEqual(update["ref"], REF)
        self.assertEqual(update["digest"], f"sha256:{source_digest}")
        self.assertEqual(update["archive_url"], "https://example.invalid/source.zip")
        self.assertEqual({asset["ref"] for asset in update["assets"]}, {REF})
        self.assertEqual(pyos_updater.executable_asset(update)["asset_id"], 11)

    def test_source_metadata_without_digest_or_immutable_ref_fails_before_download(self):
        with mock.patch.object(pyos_updater, "_download") as download:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                pyos_updater.install_source_update(
                    {"ref": REF, "archive_url": f"https://invalid/{REF}.zip"}, ".", "data"
                )
            with self.assertRaisesRegex(ValueError, "immutable"):
                pyos_updater.install_source_update(
                    {"ref": "main", "archive_url": "https://invalid/main.zip",
                     "digest": "sha256:" + "0" * 64}, ".", "data"
                )
            with self.assertRaisesRegex(ValueError, "not bound"):
                pyos_updater.install_source_update(
                    {"ref": REF, "archive_url": f"https://invalid/main.zip?ref={REF}",
                     "digest": "sha256:" + "0" * 64}, ".", "data"
                )
        download.assert_not_called()

    def test_source_requires_an_official_pinned_commit_digest_binding(self):
        digest = "1" * 64
        update = self._update(digest)
        with mock.patch.object(
                pyos_updater, "TRUSTED_SOURCE_RELEASE_BINDINGS", frozenset()
        ), mock.patch.object(pyos_updater, "_download") as download:
            with self.assertRaisesRegex(ValueError, "source release binding is pinned"):
                pyos_updater.install_source_update(update, ".", "data")
        download.assert_not_called()

        with mock.patch.object(
                pyos_updater,
                "TRUSTED_SOURCE_RELEASE_BINDINGS",
                frozenset({(REF, "2" * 64)}),
        ), mock.patch.object(pyos_updater, "_download") as download:
            with self.assertRaisesRegex(ValueError, "commit-to-digest allowlist"):
                pyos_updater.install_source_update(update, ".", "data")
        download.assert_not_called()

    def test_source_digest_mismatch_does_not_modify_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            (install / "pyOSgui.py").write_text("old gui", encoding="utf-8")
            (install / "setup.py").write_text("old setup", encoding="utf-8")
            archive, digest = self._source_archive(
                root, {"pyOSgui.py": "new gui", "setup.py": "new setup"}
            )
            update = self._update("0" * 64)
            with self._trust_source("0" * 64), mock.patch.object(
                pyos_updater, "_download", side_effect=self._download_from(archive)
            ):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    pyos_updater.install_source_update(update, install, data)
            self.assertEqual((install / "pyOSgui.py").read_text(encoding="utf-8"), "old gui")
            self.assertEqual((install / "setup.py").read_text(encoding="utf-8"), "old setup")
            self.assertNotEqual(digest, "0" * 64)

    def test_mid_overlay_failure_rolls_back_replaced_and_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            originals = {"pyOSgui.py": "old gui", "setup.py": "old setup"}
            for name, contents in originals.items():
                (install / name).write_text(contents, encoding="utf-8")
            archive, digest = self._source_archive(
                root,
                {"pyOSgui.py": "new gui", "setup.py": "new setup", "added.py": "new file"},
            )
            real_replace = os.replace
            replacements = 0

            def fail_second_overlay(source, destination):
                nonlocal replacements
                if ".update-" in Path(source).name:
                    replacements += 1
                    if replacements == 2:
                        raise OSError("simulated replace failure")
                return real_replace(source, destination)

            with self._trust_source(digest), mock.patch.object(
                pyos_updater, "_download", side_effect=self._download_from(archive)
            ), mock.patch.object(pyos_updater.os, "replace", side_effect=fail_second_overlay):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    pyos_updater.install_source_update(self._update(digest), install, data)

            for name, contents in originals.items():
                self.assertEqual((install / name).read_text(encoding="utf-8"), contents)
            self.assertFalse((install / "added.py").exists())

    def test_verified_source_overlay_commits_with_restore_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            (install / "pyOSgui.py").write_text("old gui", encoding="utf-8")
            (install / "setup.py").write_text("old setup", encoding="utf-8")
            archive, digest = self._source_archive(
                root, {"pyOSgui.py": "new gui", "setup.py": "new setup", "added.py": "new"}
            )
            with self._trust_source(digest), mock.patch.object(
                pyos_updater, "_download", side_effect=self._download_from(archive)
            ):
                result = pyos_updater.install_source_update(self._update(digest), install, data)
            backup = Path(result["backup"])
            self.assertEqual((install / "pyOSgui.py").read_text(encoding="utf-8"), "new gui")
            self.assertEqual((install / "added.py").read_text(encoding="utf-8"), "new")
            self.assertEqual((backup / "pyOSgui.py").read_text(encoding="utf-8"), "old gui")
            self.assertTrue((backup / "update.json").is_file())

    def test_source_overlay_registers_new_files_in_owned_install_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            (install / "pyOSgui.py").write_text("old gui", encoding="utf-8")
            (install / "setup.py").write_text("old setup", encoding="utf-8")
            installation_id = str(uuid.uuid4())
            marker = {"product": "pyOS", "schema_version": 2,
                      "installation_id": installation_id}
            manifest = {
                **marker, "install_dir": str(install),
                "owned_paths": [".pyos-installation-owner.json", "install_manifest.json",
                                "pyOSgui.py", "setup.py"],
                "external_files": [],
            }
            (install / ".pyos-installation-owner.json").write_text(
                json.dumps(marker), encoding="utf-8"
            )
            (install / "install_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            archive, digest = self._source_archive(
                root, {"pyOSgui.py": "new gui", "setup.py": "new setup", "new_module.py": "new"}
            )
            with self._trust_source(digest), mock.patch.object(
                pyos_updater, "_download", side_effect=self._download_from(archive)
            ):
                pyos_updater.install_source_update(self._update(digest), install, data)
            updated = json.loads((install / "install_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("new_module.py", updated["owned_paths"])
            self.assertEqual(updated["last_update_ref"], REF)

    def test_source_update_retires_only_previously_managed_files_transactionally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            for name, contents in {
                "pyOSgui.py": "old gui", "setup.py": "old setup",
                "retired_module.py": "retired code", "user-notes.txt": "preserve me",
            }.items():
                (install / name).write_text(contents, encoding="utf-8")
            (install / pyos_updater.SOURCE_INVENTORY_FILE).write_text(json.dumps({
                "product": "pyOS", "version": 1, "ref": "b" * 40,
                "files": [
                    pyos_updater.SOURCE_INVENTORY_FILE,
                    "pyOSgui.py", "retired_module.py", "setup.py",
                ],
            }), encoding="utf-8")
            archive, digest = self._source_archive(
                root, {"pyOSgui.py": "new gui", "setup.py": "new setup"}
            )

            with self._trust_source(digest), mock.patch.object(
                pyos_updater, "_download", side_effect=self._download_from(archive)
            ):
                result = pyos_updater.install_source_update(
                    self._update(digest), install, data
                )

            self.assertFalse((install / "retired_module.py").exists())
            self.assertEqual(
                (install / "user-notes.txt").read_text(encoding="utf-8"), "preserve me"
            )
            self.assertEqual(
                (Path(result["backup"]) / "retired_module.py").read_text(encoding="utf-8"),
                "retired code",
            )
            record = json.loads(
                (Path(result["backup"]) / "update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["retired_files"], ["retired_module.py"])
            inventory = json.loads(
                (install / pyos_updater.SOURCE_INVENTORY_FILE).read_text(encoding="utf-8")
            )
            self.assertNotIn("retired_module.py", inventory["files"])

    def test_interrupted_source_transaction_recovers_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            install, data, journal_path, active_path = self._interrupted_source_transaction(
                Path(temporary)
            )

            result = pyos_updater.recover_source_update(install, data)

            self.assertEqual(result["state"], "recovered")
            self.assertEqual((install / "pyOSgui.py").read_text(encoding="utf-8"), "old gui")
            self.assertFalse((install / "newdir").exists())
            self.assertFalse((install / "preexisting" / "added.py").exists())
            self.assertTrue((install / "preexisting").is_dir())
            self.assertFalse(active_path.exists())
            self.assertEqual(
                json.loads(journal_path.read_text(encoding="utf-8"))["state"], "recovered"
            )
            self.assertIsNone(pyos_updater.recover_source_update(install, data))

    def test_interrupted_retirement_restores_the_removed_managed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, data = root / "install", root / "data"
            install.mkdir()
            data.mkdir()
            transaction_id = uuid.uuid4().hex
            backup = data / "update_backups" / transaction_id
            backup.mkdir(parents=True)
            saved = backup / "retired.py"
            saved.write_text("trusted old code", encoding="utf-8")
            entry = {
                "path": "retired.py", "operation": "delete", "existed": True,
                "installed_sha256": "",
                "backup_sha256": pyos_updater._sha256_file(saved),
            }
            journal = {
                "version": pyos_updater.SOURCE_TRANSACTION_VERSION,
                "transaction_id": transaction_id, "state": "applying",
                "install_dir": str(install.resolve()), "ref": REF,
                "sha256": "1" * 64, "entries": [entry],
                "applied": ["retired.py"], "created_dirs": [],
            }
            journal_path = backup / "transaction.json"
            pyos_updater._atomic_write_json(journal_path, journal)
            pyos_updater._atomic_write_json(
                data / pyos_updater.SOURCE_TRANSACTION_FILE,
                {
                    "version": pyos_updater.SOURCE_TRANSACTION_VERSION,
                    "transaction_id": transaction_id,
                    "install_dir": str(install.resolve()),
                    "journal": journal_path.relative_to(data).as_posix(),
                },
            )

            result = pyos_updater.recover_source_update(install, data)

            self.assertEqual(result["state"], "recovered")
            self.assertEqual(
                (install / "retired.py").read_text(encoding="utf-8"), "trusted old code"
            )

    def test_interrupted_recovery_refuses_a_locally_modified_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            install, data, journal_path, active_path = self._interrupted_source_transaction(
                Path(temporary)
            )
            target = install / "pyOSgui.py"
            target.write_text("local edit after crash", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed after the interrupted update"):
                pyos_updater.recover_source_update(install, data)

            self.assertEqual(target.read_text(encoding="utf-8"), "local edit after crash")
            self.assertTrue((install / "newdir" / "added.py").is_file())
            self.assertTrue(active_path.is_file())
            self.assertEqual(
                json.loads(journal_path.read_text(encoding="utf-8"))["state"], "applying"
            )

    def test_interrupted_recovery_refuses_a_missing_original_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            install, data, journal_path, active_path = self._interrupted_source_transaction(
                Path(temporary)
            )
            target = install / "pyOSgui.py"
            target.unlink()

            with self.assertRaisesRegex(ValueError, "missing after the interrupted update"):
                pyos_updater.recover_source_update(install, data)

            self.assertFalse(target.exists())
            self.assertTrue(active_path.is_file())
            self.assertEqual(
                json.loads(journal_path.read_text(encoding="utf-8"))["state"], "applying"
            )

    def test_executable_requires_digest_ref_and_authenticode(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with mock.patch.object(pyos_updater, "_download") as download:
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    pyos_updater.stage_executable_update({"url": "https://invalid/pyOS.exe"}, data)
            download.assert_not_called()

            payload = b"MZ" + b"\0" * (1024 * 1024)
            digest = hashlib.sha256(payload).hexdigest()

            def download_executable(_url, destination, _progress=None):
                destination.write_bytes(payload)
                return digest

            asset = {
                "url": "https://invalid/pyOS.exe", "ref": REF,
                "digest": f"sha256:{digest}", "asset_id": 123,
            }
            with mock.patch.object(
                    pyos_updater, "TRUSTED_AUTHENTICODE_THUMBPRINTS", frozenset()
            ), mock.patch.object(pyos_updater, "_download") as untrusted_download:
                with self.assertRaisesRegex(ValueError, "signer is pinned"):
                    pyos_updater.stage_executable_update(asset, data)
            untrusted_download.assert_not_called()

            trusted = frozenset({"A" * 40})
            with mock.patch.object(
                    pyos_updater, "TRUSTED_AUTHENTICODE_THUMBPRINTS", trusted
            ), mock.patch.object(pyos_updater, "_download", side_effect=download_executable), \
                    mock.patch.object(
                        pyos_updater, "_validate_authenticode",
                        side_effect=ValueError("signature invalid"),
                    ):
                with self.assertRaisesRegex(ValueError, "signature invalid"):
                    pyos_updater.stage_executable_update(asset, data)
            self.assertFalse((data / "pending_updates" / "pyOS-update.exe").exists())

            signature = {"Status": "Valid", "Subject": "CN=pyOS", "Thumbprint": "A" * 40}
            with mock.patch.object(
                    pyos_updater, "TRUSTED_AUTHENTICODE_THUMBPRINTS", trusted
            ), mock.patch.object(pyos_updater, "_download", side_effect=download_executable), \
                    mock.patch.object(pyos_updater, "_validate_authenticode", return_value=signature):
                staged = pyos_updater.stage_executable_update(asset, data)
            self.assertEqual(Path(staged["path"]).read_bytes(), payload)
            self.assertIn(REF[:16], Path(staged["path"]).name)
            self.assertIn(staged["update_id"], Path(staged["path"]).name)
            self.assertEqual(staged["signature"], signature)

    @unittest.skipUnless(os.name == "nt", "Authenticode is a Windows trust mechanism")
    def test_authenticode_uses_an_out_of_band_literal_path(self):
        response = SimpleNamespace(
            returncode=0,
            stdout='{"Status":"Valid","Subject":"CN=pyOS","Thumbprint":"' + "A" * 40 + '"}',
            stderr="",
        )
        target = Path("signed update with spaces.exe").resolve()
        with mock.patch.object(
                pyos_updater, "TRUSTED_AUTHENTICODE_THUMBPRINTS", frozenset({"A" * 40})
        ), mock.patch.object(pyos_updater.shutil, "which", return_value="powershell.exe"), \
                mock.patch.object(pyos_updater.subprocess, "run", return_value=response) as run:
            details = pyos_updater._validate_authenticode(target)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn(str(target), command)
        self.assertEqual(environment["PYOS_AUTHENTICODE_TARGET"], str(target))
        self.assertEqual(details["Status"], "Valid")

    def test_update_lock_excludes_a_second_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = (
                "import sys\n"
                "from pathlib import Path\n"
                "import pyos_updater\n"
                "with pyos_updater._update_lock(Path(sys.argv[1])):\n"
                " print('locked', flush=True)\n"
                " sys.stdin.readline()\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, temporary], cwd=str(Path(__file__).resolve().parents[1]),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            child_error = ""
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(ValueError, "already in progress"):
                    with pyos_updater._update_lock(temporary):
                        self.fail("second process acquired the update lock")
            finally:
                _output, child_error = child.communicate(input="\n", timeout=10)
            self.assertEqual(child.returncode, 0, child_error)


if __name__ == "__main__":
    unittest.main()
