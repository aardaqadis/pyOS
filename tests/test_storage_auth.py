import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pyos_auth as auth
import pyos_config as storage


def _legacy_account(username="Legacy User", password="password123"):
    salt = b"s" * 32
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, auth.MIN_ITERATIONS
    )
    return {
        "version": 1,
        "username": username,
        "algorithm": "pbkdf2-sha256",
        "iterations": auth.MIN_ITERATIONS,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
    }


class StorageAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.legacy_home = self.base / "home"
        self.standalone = self.base / "standalone"
        self.legacy_home.mkdir()
        self.originals = {
            "legacy_home": storage.LEGACY_HOME,
            "standalone": storage.STANDALONE_ROOT,
            "config": storage.CONFIG_FILE,
            "legacy_config": storage.LEGACY_CONFIG_FILE,
            "migrate_legacy": storage.MIGRATE_LEGACY_STATE,
            "config_user": storage._ACTIVE_USERNAME,
            "config_profile": storage._ACTIVE_PROFILE_ID,
            "auth_user": auth._active_username,
            "iterations": auth.PBKDF2_ITERATIONS,
        }
        storage.LEGACY_HOME = self.legacy_home
        storage.STANDALONE_ROOT = self.standalone
        storage.CONFIG_FILE = self.standalone / "install.json"
        storage.LEGACY_CONFIG_FILE = self.legacy_home / ".pyos_install.json"
        storage.MIGRATE_LEGACY_STATE = True
        storage._ACTIVE_USERNAME = None
        storage._ACTIVE_PROFILE_ID = None
        auth._active_username = None
        auth.PBKDF2_ITERATIONS = auth.MIN_ITERATIONS

    def tearDown(self):
        storage.LEGACY_HOME = self.originals["legacy_home"]
        storage.STANDALONE_ROOT = self.originals["standalone"]
        storage.CONFIG_FILE = self.originals["config"]
        storage.LEGACY_CONFIG_FILE = self.originals["legacy_config"]
        storage.MIGRATE_LEGACY_STATE = self.originals["migrate_legacy"]
        storage._ACTIVE_USERNAME = self.originals["config_user"]
        storage._ACTIVE_PROFILE_ID = self.originals["config_profile"]
        auth._active_username = self.originals["auth_user"]
        auth.PBKDF2_ITERATIONS = self.originals["iterations"]
        self.temporary.cleanup()

    def test_missing_credentials_are_first_run_and_unicode_names_match(self):
        self.assertFalse(auth.has_account())

        self.assertEqual(auth.create_account("jos\u00e9", "password123"), "jos\u00e9")
        self.assertTrue(auth.verify_credentials("JOSE\u0301", "password123"))
        self.assertEqual(auth.load_credentials("JOSE\u0301")["username"], "jos\u00e9")

        with self.assertRaisesRegex(ValueError, "already exists"):
            auth.create_account("JOS\u00c9", "different123")

    def test_corrupt_credentials_recover_only_from_validated_backup(self):
        auth.create_account("Administrator", "password123")
        primary = auth.credentials_path()
        backup = primary.with_name(primary.name + ".bak")
        self.assertTrue(backup.exists())

        primary.write_text("{not-json", encoding="utf-8")
        self.assertTrue(auth.verify_credentials("administrator", "password123"))

        # Parseable JSON is not enough: every account and the admin invariant
        # must validate before a primary or backup is trusted.
        invalid_database = {"version": 2, "accounts": [{"username": "broken"}]}
        primary.write_text(json.dumps(invalid_database), encoding="utf-8")
        self.assertTrue(auth.verify_credentials("administrator", "password123"))

        # A persisted empty v2 store is damaged initialized state, not a new
        # installation.  Recover it from the last valid database.
        empty_database = {"version": 2, "accounts": []}
        primary.write_text(json.dumps(empty_database), encoding="utf-8")
        self.assertTrue(auth.verify_credentials("administrator", "password123"))

        primary.write_text(json.dumps(empty_database), encoding="utf-8")
        backup.write_text(json.dumps(empty_database), encoding="utf-8")
        with self.assertRaises(auth.CredentialStoreError):
            auth.has_account()

    def test_corrupt_config_recovers_or_fails_closed(self):
        storage.save_config(storage._default_config())
        primary = storage.CONFIG_FILE
        backup = primary.with_name(primary.name + ".bak")

        primary.write_text("{not-json", encoding="utf-8")
        recovered = storage.load_config()
        self.assertFalse(recovered["configured"])
        self.assertEqual(Path(recovered["data_dir"]), storage.get_standalone_root())

        # Parseable-but-incomplete installed config must use the valid backup,
        # never standalone defaults inferred at load time.
        primary.write_text("{}", encoding="utf-8")
        recovered = storage.load_config()
        self.assertFalse(recovered["configured"])
        primary.write_text(json.dumps({"configured": False}), encoding="utf-8")
        recovered = storage.load_config()
        self.assertFalse(recovered["configured"])

        invalid_config = {"configured": False}
        primary.write_text(json.dumps(invalid_config), encoding="utf-8")
        backup.write_text(json.dumps(invalid_config), encoding="utf-8")
        with self.assertRaises(storage.ConfigurationError):
            storage.load_config()

    def test_ownership_failure_does_not_commit_config_or_backup(self):
        initial = storage.save_config(storage._default_config())
        primary = storage.CONFIG_FILE
        backup = primary.with_name(primary.name + ".bak")
        primary_before = primary.read_bytes()
        backup_before = backup.read_bytes()
        replacement = dict(initial)
        replacement.update({
            "configured": True,
            "data_dir": str(self.base / "unavailable-data"),
            "drive_b_dir": str(self.base / "unavailable-data" / "Drive_B"),
        })

        with mock.patch.object(
            storage,
            "ensure_storage_owner",
            side_effect=storage.StorageOwnershipError("ownership refused"),
        ):
            with self.assertRaises(storage.StorageOwnershipError):
                storage.save_config(replacement)

        self.assertEqual(primary.read_bytes(), primary_before)
        self.assertEqual(backup.read_bytes(), backup_before)

    def test_legacy_artifacts_and_account_migrate_without_deleting_source(self):
        account_path = self.legacy_home / ".pyos_credentials.json"
        account_path.write_text(json.dumps(_legacy_account()), encoding="utf-8")
        settings_path = self.legacy_home / ".pyos_gui_settings.json"
        settings_path.write_text('{"theme": "legacy"}', encoding="utf-8")
        apps = self.legacy_home / "apps"
        apps.mkdir()
        (apps / "made_for_pyos.py").write_text(
            "APP_NAME = 'Legacy'\n\ndef build(app, window):\n    pass\n", encoding="utf-8"
        )
        (apps / "unrelated.py").write_text(
            "print('not an App Maker app')\n", encoding="utf-8"
        )

        self.assertTrue(auth.verify_credentials("legacy user", "password123"))
        root = storage.get_standalone_root()
        self.assertEqual(
            (root / "gui_settings.json").read_text(encoding="utf-8"),
            '{"theme": "legacy"}',
        )
        self.assertTrue((root / "apps" / "made_for_pyos.py").exists())
        self.assertFalse((root / "apps" / "unrelated.py").exists())
        self.assertTrue(account_path.exists() and settings_path.exists())

    def test_standalone_account_moves_to_configured_data_directory(self):
        auth.create_account("Administrator", "password123")
        standalone_credentials = auth.credentials_path()
        configured_data = self.base / "configured-data"
        storage.save_config({
            "configured": True,
            "install_dir": str(self.base / "application"),
            "data_dir": str(configured_data),
            "downloads_dir": str(self.base / "downloads"),
            "drive_b_dir": str(configured_data / "Drive_B"),
            "enabled_apps": None,
        })

        self.assertNotEqual(auth.credentials_path(), standalone_credentials)
        self.assertTrue(auth.verify_credentials("administrator", "password123"))
        self.assertTrue(standalone_credentials.exists())
        self.assertTrue((configured_data / "credentials.json").exists())

    def test_owner_manifest_rejects_paths_outside_root(self):
        root = storage.get_standalone_root(create=True)
        inside = storage.register_owned_path(root / "apps", root)
        self.assertIn(inside, storage.owned_paths(root))
        self.assertTrue(storage.verify_storage_owner(root, kind="standalone"))

        with self.assertRaises(storage.StorageOwnershipError):
            storage.register_owned_path(self.base / "outside", root)

    def test_owned_file_replaced_by_directory_is_never_removed_recursively(self):
        root = storage.get_standalone_root(create=True)
        settings = root / "settings.json"
        settings.write_text("owned", encoding="utf-8")
        storage.register_owned_path(settings, root, kind=storage.OWNED_FILE)

        settings.unlink()
        settings.mkdir()
        unrelated = settings / "unrelated.txt"
        unrelated.write_text("preserve", encoding="utf-8")

        with self.assertRaisesRegex(
            storage.StorageOwnershipError, "replaced by a directory"
        ):
            storage.remove_owned_storage_paths(root)

        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")

    def test_legacy_untyped_manifest_never_grants_recursive_authority(self):
        root = storage.get_standalone_root(create=True)
        apps = root / "apps"
        apps.mkdir()
        unrelated = apps / "unrelated.py"
        unrelated.write_text("preserve", encoding="utf-8")
        marker = root / storage.OWNER_FILENAME
        owner = json.loads(marker.read_text(encoding="utf-8"))
        owner["version"] = min(storage.LEGACY_OWNER_VERSIONS)
        owner["owned_paths"].append("apps")
        owner.pop("owned_path_types", None)
        marker.write_text(json.dumps(owner), encoding="utf-8")

        with self.assertRaisesRegex(
            storage.StorageOwnershipError, "replaced by a directory"
        ):
            storage.remove_owned_storage_paths(root)

        self.assertTrue(unrelated.is_file())

    def test_only_explicitly_approved_trees_receive_recursive_authority(self):
        root = storage.get_standalone_root(create=True)
        tree = root / "apps"
        tree.mkdir()
        (tree / "owned.py").write_text("remove", encoding="utf-8")
        storage.register_owned_path(tree, root, kind=storage.OWNED_TREE)

        removed = storage.remove_owned_storage_paths(root)

        self.assertIn(tree, removed)
        self.assertFalse(tree.exists())

    def test_update_json_file_is_cross_process_safe(self):
        counter = self.base / "counter.json"
        repository = Path(__file__).resolve().parents[1]
        script = """
import sys
from pathlib import Path
from pyos_config import update_json_file
path = Path(sys.argv[1])
for _ in range(20):
    update_json_file(path, lambda value: {"count": value["count"] + 1}, default={"count": 0})
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = (
            str(repository) + os.pathsep + environment.get("PYTHONPATH", "")
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(counter)],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(
            json.loads(counter.read_text(encoding="utf-8")), {"count": 80}
        )

    def test_account_creation_is_cross_process_safe(self):
        root = self.base / "process-auth"
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.update({
            "PYOS_HOME": str(root),
            "PYOS_CONFIG_FILE": str(root / "install.json"),
            "PYTHONPATH": str(repository) + os.pathsep + environment.get("PYTHONPATH", ""),
        })
        prefix = (
            "import pyos_auth as auth; "
            "auth.PBKDF2_ITERATIONS = auth.MIN_ITERATIONS; "
        )
        subprocess.run(
            [sys.executable, "-c", prefix + "auth.create_account('Admin', 'password123')"],
            cwd=repository,
            env=environment,
            check=True,
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    prefix +
                    f"auth._set_authenticated_user('admin'); "
                    f"auth.create_account('User{index}', 'password123')",
                ],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(3)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                prefix + "import json; print(json.dumps(auth.list_accounts()))",
            ],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        names = {account["username"] for account in json.loads(result.stdout)}
        self.assertEqual(names, {"Admin", "User0", "User1", "User2"})

    def test_first_profile_legacy_migration_is_cross_process_atomic(self):
        storage.save_config(storage._default_config())
        legacy_settings = self.standalone / "gui_settings.json"
        legacy_settings.write_text('{"theme": "legacy"}', encoding="utf-8")
        storage.register_owned_path(legacy_settings, self.standalone)

        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.update({
            "PYOS_HOME": str(self.standalone),
            "PYOS_CONFIG_FILE": str(storage.CONFIG_FILE),
            "PYTHONPATH": str(repository) + os.pathsep + environment.get("PYTHONPATH", ""),
        })
        environment.pop("PYOS_MIGRATE_LEGACY_STATE", None)
        script = (
            "import sys, time; import pyos_config as storage; "
            "time.sleep(0.15); storage.set_active_user(sys.argv[1])"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, username],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for username in ("First User", "Second User")
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)

        migrated = list((self.standalone / "profiles").glob("*/gui_settings.json"))
        self.assertEqual(len(migrated), 1)
        marker = self.standalone / ".legacy_profile_migration_complete"
        self.assertTrue(marker.is_file())
        self.assertTrue(marker.read_text(encoding="ascii").strip())

    def test_authenticate_reports_fail_closed_store_errors(self):
        parent = object()
        with mock.patch.object(
            auth, "has_account", side_effect=auth.CredentialStoreError("damaged store")
        ), mock.patch.object(auth.messagebox, "showerror") as show_error:
            result = auth.authenticate(parent, allow_remembered=False)

        self.assertIsNone(result)
        show_error.assert_called_once()
        title, message = show_error.call_args.args
        self.assertIn("Recovery Required", title)
        self.assertIn("kept the desktop locked", message)
        self.assertIs(show_error.call_args.kwargs["parent"], parent)


if __name__ == "__main__":
    unittest.main()
