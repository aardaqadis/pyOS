import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pyOScli


class _Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _bare_cli(current_directory):
    app = object.__new__(pyOScli.PythonOS)
    app.current_directory = str(current_directory)
    app.current_drive = "C"
    app.drives = {"A": str(current_directory / "A"), "B": str(current_directory / "B"),
                  "C": str(current_directory)}
    app._auth_generation = 7
    app.authenticated = True
    app._locked = False
    app._closing = False
    app._worker_local = threading.local()
    app._mutation_lock = threading.RLock()
    app._command_tasks = queue.Queue(maxsize=app.COMMAND_QUEUE_LIMIT)
    app.input_var = _Variable()
    app.command_history = []
    app.history_index = -1
    app.ensure_authenticated = lambda: True
    app.log_message = lambda _message: None
    return app


class CommandSafetyTests(unittest.TestCase):
    def test_windows_powershell_uses_explicit_noninteractive_arguments(self):
        with (
            mock.patch("pyOScli.os.name", "nt"),
            mock.patch(
                "pyOScli.shutil.which",
                side_effect=lambda name: "powershell.exe" if name == "powershell.exe" else None,
            ),
        ):
            args = pyOScli.PythonOS._host_shell_arguments(
                "powershell", "Get-ChildItem -Force", r"C:\work",
            )

        self.assertEqual(args[0], "powershell.exe")
        self.assertIn("-NonInteractive", args)
        self.assertEqual(args[-2:], ["-Command", "Get-ChildItem -Force"])

    def test_wsl_runs_from_the_cli_working_directory(self):
        with (
            mock.patch("pyOScli.os.name", "nt"),
            mock.patch(
                "pyOScli.shutil.which",
                side_effect=lambda name: "wsl.exe" if name == "wsl.exe" else None,
            ),
            mock.patch("pyOScli.os.path.abspath", return_value=r"C:\work"),
        ):
            args = pyOScli.PythonOS._host_shell_arguments("wsl", "ls -la", r"C:\work")

        self.assertEqual(
            args,
            ["wsl.exe", "--cd", r"C:\work", "--exec", "sh", "-lc", "ls -la"],
        )

    def test_host_shell_is_windows_only(self):
        with mock.patch("pyOScli.os.name", "posix"):
            with self.assertRaisesRegex(OSError, "only on Windows"):
                pyOScli.PythonOS._host_shell_arguments("wsl", "pwd", "/tmp")

    def test_shell_output_is_bounded(self):
        output = "x" * (pyOScli.PythonOS.SHELL_OUTPUT_LIMIT + 12)
        bounded = pyOScli.PythonOS._bounded_shell_output(output)
        self.assertIn("12 characters omitted", bounded)
        self.assertLess(len(bounded), len(output) + 100)
    def test_enqueued_command_keeps_working_directory_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            app = _bare_cli(first)
            app.input_var.set("rm victim.txt")

            app.execute_command()
            task = app._command_tasks.get_nowait()
            app.current_directory = str(second)
            app._worker_local.command_task = task

            self.assertIsInstance(task, pyOScli.CommandTask)
            self.assertEqual(Path(task.working_directory), first)
            self.assertEqual(Path(app._resolve_path("victim.txt")), first / "victim.txt")

    def test_destructive_command_uses_snapshot_not_later_ui_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            app = _bare_cli(first)
            task = pyOScli.CommandTask("touch victim.txt", str(first), "C", app._auth_generation)
            app._worker_local.command_task = task
            app.current_directory = str(second)
            app.refresh_files = lambda: None

            app._run_command(task.command)

            self.assertTrue((first / "victim.txt").is_file())
            self.assertFalse((second / "victim.txt").exists())

    def test_cancelled_generation_cannot_enter_mutation_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = _bare_cli(Path(temporary))
            task = pyOScli.CommandTask("touch x", temporary, "C", app._auth_generation)
            task.cancel_event.set()
            called = []

            with self.assertRaises(pyOScli.CommandCancelled):
                app._perform_mutation(lambda: called.append(True), task)
            self.assertEqual(called, [])

    def test_optional_policy_gates_every_browser_and_media_surface(self):
        app = object.__new__(pyOScli.PythonOS)
        app.enabled_apps = set()
        for command in ("browser", "browse", "inspect", "savepage", "download_page",
                        "play", "media", "desktop_browser", "desktop_media"):
            self.assertFalse(app._optional_command_enabled(command), command)

        app.enabled_apps = {"browser"}
        for command in ("browser", "browse", "inspect", "savepage", "download_page"):
            self.assertTrue(app._optional_command_enabled(command), command)
        self.assertFalse(app._optional_command_enabled("media"))

    def test_auxiliary_queue_is_bounded(self):
        app = object.__new__(pyOScli.PythonOS)
        app._closing = False
        app._locked = False
        app.authenticated = True
        app._auth_generation = 1
        app._auxiliary_tasks = queue.Queue(maxsize=1)

        first = app._submit_auxiliary(lambda _cancel: None, lambda _value: None, lambda _error: None)
        second = app._submit_auxiliary(lambda _cancel: None, lambda _value: None, lambda _error: None)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_auxiliary_generation_is_rejected(self):
        app = object.__new__(pyOScli.PythonOS)
        app._closing = False
        app._locked = False
        app.authenticated = True
        app._auth_generation = 2
        stale = pyOScli.AuxiliaryTask(lambda _cancel: None, lambda _value: None,
                                     lambda _error: None, auth_generation=1)
        self.assertFalse(app._auxiliary_task_is_active(stale))

    def test_widget_methods_route_worker_calls_to_ui_dispatch(self):
        app = object.__new__(pyOScli.PythonOS)
        app._ui_thread_id = -1
        app._post_command_ui = mock.Mock(return_value=True)

        app.clear_console()
        app.refresh_files()

        callbacks = [call.args[0] for call in app._post_command_ui.call_args_list]
        self.assertEqual(callbacks, [app.clear_console, app.refresh_files])

    def test_tracked_process_receives_snapshotted_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = _bare_cli(Path(temporary))
            app._children_lock = threading.Lock()
            app._child_processes = set()
            task = pyOScli.CommandTask("tool", temporary, "C", app._auth_generation)
            app._worker_local.command_task = task

            process = mock.Mock()
            process.communicate.return_value = ("out", "")
            process.poll.return_value = 0
            process.returncode = 0
            with mock.patch("pyOScli.subprocess.Popen", return_value=process) as popen:
                result = app._run_tracked_capture("tool", cwd=temporary, shell=True)

            self.assertEqual(result, (0, "out", ""))
            self.assertEqual(popen.call_args.kwargs["cwd"], temporary)
            self.assertTrue(popen.call_args.kwargs["shell"])


class ThemePersistenceTests(unittest.TestCase):
    def test_theme_save_is_atomic_and_valid_backup_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "cli_settings.json"
            with mock.patch("pyOScli.get_cli_settings_path", return_value=settings_path):
                settings = pyOScli.ThemeSettings()
                settings.settings["console_bg"] = "#123456"
                settings.save_settings()
                settings.settings["console_fg"] = "#abcdef"
                settings.save_settings()

                settings_path.write_text("{broken", encoding="utf-8")
                recovered = pyOScli.ThemeSettings()

            self.assertEqual(recovered.settings["console_bg"], "#123456")
            self.assertEqual(recovered.settings["console_fg"], "#ffffff")
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["console_bg"],
                "#123456",
            )
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_corrupt_theme_and_backup_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "cli_settings.json"
            settings_path.write_text("{broken", encoding="utf-8")
            settings_path.with_name(settings_path.name + ".bak").write_text(
                "[]", encoding="utf-8"
            )
            with mock.patch("pyOScli.get_cli_settings_path", return_value=settings_path):
                with self.assertRaises(pyOScli.JSONPersistenceError):
                    pyOScli.ThemeSettings()


class StartupSafetyTests(unittest.TestCase):
    def test_none_authentication_never_constructs_authenticated_cli(self):
        root = mock.Mock()
        with (
            mock.patch("pyOScli.relaunch_in_configured_environment", return_value=False),
            mock.patch("pyOScli.tk.Tk", return_value=root),
            mock.patch("pyOScli.load_config", return_value={"install_dir": "x", "data_dir": "y"}),
            mock.patch("pyOScli.recover_source_update", return_value=False),
            mock.patch("pyOScli.authenticate", return_value=None),
            mock.patch("pyOScli.PythonOS") as application,
        ):
            pyOScli.main()

        application.assert_not_called()
        root.destroy.assert_called_once()


class ShutdownSafetyTests(unittest.TestCase):
    def test_drive_a_cleanup_waits_for_surviving_worker(self):
        app = object.__new__(pyOScli.PythonOS)
        app._closing = False
        app._locked = False
        app.authenticated = True
        app._auth_generation = 1
        app._state_lock = threading.RLock()
        app._mutation_lock = threading.RLock()
        app._command_stop = threading.Event()
        app._auxiliary_stop = threading.Event()
        app._command_tasks = queue.Queue(maxsize=8)
        app._auxiliary_tasks = queue.Queue(maxsize=8)
        app._active_command_lock = threading.Lock()
        app._active_command_task = None
        app._active_auxiliary_lock = threading.Lock()
        app._active_auxiliary_tasks = {}
        app._children_lock = threading.Lock()
        app._child_processes = set()
        app._auxiliary_workers = []
        app._ui_after = None
        app._lock_overlay = None
        app.root = mock.Mock()
        app.drive_a = mock.Mock()
        app.SHUTDOWN_WAIT_SECONDS = 0.01
        release = threading.Event()
        entered_mutation = threading.Event()
        def active_mutation():
            with app._mutation_lock:
                entered_mutation.set()
                release.wait()
        app._command_worker = threading.Thread(target=active_mutation, daemon=True)
        app._command_worker.start()
        self.assertTrue(entered_mutation.wait(timeout=1))

        app.shutdown()

        app.drive_a.cleanup.assert_not_called()
        self.assertIsNotNone(app._cleanup_thread)
        self.assertFalse(app._cleanup_thread.daemon)
        release.set()
        app._cleanup_thread.join(timeout=1)
        self.assertFalse(app._cleanup_thread.is_alive())
        app.drive_a.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
