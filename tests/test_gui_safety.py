import codecs
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pyOSgui

from pyOSgui import (
    RequestGate,
    TkTaskManager,
    acknowledge_update_startup,
    atomic_save_image,
    atomic_write_bytes,
    count_sudoku_solutions,
    decode_text_document,
    document_needs_save_as,
    encode_text_document,
    recover_startup_source_update,
)


class _FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        token = object()
        self.scheduled.append((token, delay, callback))
        return token

    def after_cancel(self, token):
        self.scheduled = [item for item in self.scheduled if item[0] is not token]

    def report_callback_exception(self, *_args):
        raise AssertionError("unexpected callback exception")


class TextDocumentTests(unittest.TestCase):
    def test_new_documents_and_edited_destinations_require_save_as(self):
        self.assertTrue(document_needs_save_as(None, Path.home() / "untitled.txt"))
        self.assertFalse(document_needs_save_as("same.txt", "same.txt"))
        self.assertTrue(document_needs_save_as("one.txt", "two.txt"))

    def test_utf16_bom_and_crlf_round_trip_losslessly(self):
        original = codecs.BOM_UTF16_LE + "café\r\nsecond\r\n".encode("utf-16-le")

        text, encoding, bom, newline = decode_text_document(original)

        self.assertEqual(text, "café\nsecond\n")
        self.assertEqual(encoding, "utf-16-le")
        self.assertEqual(bom, codecs.BOM_UTF16_LE)
        self.assertEqual(newline, "\r\n")
        self.assertEqual(encode_text_document(text, encoding, bom, newline), original)

    def test_mixed_newlines_round_trip_exactly(self):
        original = b"first\r\nsecond\nthird\rfourth\r\n"

        text, encoding, bom, newline = decode_text_document(original)

        self.assertEqual(text, "first\nsecond\nthird\nfourth\n")
        self.assertIsInstance(newline, dict)
        self.assertEqual(encode_text_document(text, encoding, bom, newline), original)

    def test_edit_retains_bom_encoding_and_existing_mixed_newline_layout(self):
        original = codecs.BOM_UTF16_BE + "first\r\nsecond\nthird\r".encode("utf-16-be")
        text, encoding, bom, newline = decode_text_document(original)

        edited = text.replace("second", "changed")
        payload = encode_text_document(edited, encoding, bom, newline)

        self.assertTrue(payload.startswith(codecs.BOM_UTF16_BE))
        self.assertEqual(
            payload,
            codecs.BOM_UTF16_BE + "first\r\nchanged\nthird\r".encode("utf-16-be"),
        )

    def test_unrepresentable_edit_requires_an_explicit_encoding_change(self):
        text, _encoding, bom, newline = decode_text_document(b"caf\xe9\r\n")
        encoding = "latin-1"

        with self.assertRaises(UnicodeEncodeError):
            encode_text_document(text + "\U0001f642", encoding, bom, newline)

        converted = encode_text_document(text + "\U0001f642", "utf-8", b"", newline)
        self.assertEqual(converted, "caf\xe9\r\n\U0001f642".encode("utf-8"))

    def test_binary_content_is_rejected_instead_of_rewritten_as_latin1(self):
        with self.assertRaises(UnicodeError):
            decode_text_document(bytes(range(256)))

    def test_atomic_write_replaces_content_without_shared_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "document.txt"
            target.write_bytes(b"old")

            atomic_write_bytes(target, b"new")

            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_atomic_image_encode_failure_leaves_original_untouched(self):
        class FailingImage:
            def save(self, output, format=None):
                self.format = format
                output.write(b"partial image")
                raise OSError("encoder failed")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "picture.png"
            target.write_bytes(b"original image")
            image = FailingImage()

            with self.assertRaisesRegex(OSError, "encoder failed"):
                atomic_save_image(image, target)

            self.assertEqual(image.format, "PNG")
            self.assertEqual(target.read_bytes(), b"original image")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*{target.suffix}")), [])

    def test_atomic_image_save_replaces_only_after_successful_encode(self):
        class SuccessfulImage:
            def save(self, output, format=None):
                self.format = format
                output.write(b"complete image")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "picture.jpg"
            target.write_bytes(b"original image")
            image = SuccessfulImage()

            atomic_save_image(image, target)

            self.assertEqual(image.format, "JPEG")
            self.assertEqual(target.read_bytes(), b"complete image")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*{target.suffix}")), [])


class GuiSoundTests(unittest.TestCase):
    def test_missing_or_unknown_sound_is_safely_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            player = pyOSgui.GuiSoundPlayer(temporary)
            self.assertFalse(player.play("startup"))
            self.assertFalse(player.play("unknown"))

    def test_windows_sound_uses_nonblocking_bundled_wave(self):
        with tempfile.TemporaryDirectory() as temporary:
            sound_dir = Path(temporary) / "sounds"
            sound_dir.mkdir()
            sound = sound_dir / "Windows Startup.wav"
            sound.write_bytes(b"RIFF")
            winsound = SimpleNamespace(
                SND_FILENAME=1, SND_ASYNC=2, SND_NODEFAULT=4, PlaySound=Mock(),
            )
            with (
                patch("pyOSgui.os.name", "nt"),
                patch.dict("sys.modules", {"winsound": winsound}),
            ):
                self.assertTrue(pyOSgui.GuiSoundPlayer(temporary).play("startup"))

            winsound.PlaySound.assert_called_once_with(str(sound), 7)

    def test_linux_sound_uses_nonblocking_available_player(self):
        with tempfile.TemporaryDirectory() as temporary:
            sound_dir = Path(temporary) / "sounds"
            sound_dir.mkdir()
            sound = sound_dir / "Windows Notify.wav"
            sound.write_bytes(b"RIFF")
            popen = Mock()
            with (
                patch("pyOSgui.os.name", "posix"),
                patch("pyOSgui.sys.platform", "linux"),
                patch("pyOSgui.shutil.which", side_effect=lambda name: "/usr/bin/paplay" if name == "paplay" else None),
                patch("pyOSgui.subprocess.Popen", popen),
            ):
                self.assertTrue(pyOSgui.GuiSoundPlayer(temporary).play("notification"))

            popen.assert_called_once_with(
                ["/usr/bin/paplay", str(sound)],
                stdout=pyOSgui.subprocess.DEVNULL,
                stderr=pyOSgui.subprocess.DEVNULL,
            )
    def test_desktop_sound_preference_can_disable_playback(self):
        app = object.__new__(pyOSgui.DesktopGUI)
        app.preferences = {"sounds_enabled": False}
        app.sound_player = Mock()
        self.assertFalse(app.play_sound("notification"))
        app.sound_player.play.assert_not_called()


class CustomAppStorageTests(unittest.TestCase):
    def test_custom_apps_directory_is_registered_as_an_owned_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = object.__new__(pyOSgui.DesktopGUI)
            app.settings_path = Path(temporary) / "profile" / "gui_settings.json"
            with patch("pyOSgui.register_owned_path") as register:
                path = app._custom_apps_directory()

            self.assertTrue(path.is_dir())
            register.assert_called_once_with(path, kind=pyOSgui.pyos_storage.OWNED_TREE)


class AsyncSafetyTests(unittest.TestCase):
    def test_request_gate_rejects_stale_and_closed_results(self):
        gate = RequestGate()
        first = gate.next()
        second = gate.next()
        self.assertFalse(gate.valid(first))
        self.assertTrue(gate.valid(second))
        gate.close()
        self.assertFalse(gate.valid(second))

    def test_task_manager_bounds_pending_work_and_drains_on_owner(self):
        root = _FakeRoot()
        manager = TkTaskManager(root, max_workers=1, max_pending=1, max_callbacks=2)
        started = threading.Event()
        release = threading.Event()
        callback_threads = []

        def blocking_work():
            started.set()
            release.wait(2)

        try:
            manager.submit(blocking_work)
            self.assertTrue(started.wait(1))
            manager.submit(lambda: None)
            with self.assertRaises(RuntimeError):
                manager.submit(lambda: None)

            self.assertTrue(manager.post(lambda: callback_threads.append(threading.get_ident())))
            self.assertTrue(manager.post(lambda: None))
            self.assertTrue(manager.post_event(lambda: None))
            self.assertTrue(manager.post_event(lambda: None))
            self.assertFalse(manager.post_event(lambda: None))
            manager._drain()
            self.assertEqual(callback_threads, [threading.get_ident()])
        finally:
            release.set()
            manager.shutdown()


class UpdateAcknowledgementTests(unittest.TestCase):
    def test_acknowledgement_is_constrained_and_contains_the_commit(self):
        reference = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            pending = data_dir / "pending_updates"
            pending.mkdir()
            acknowledgement = pending / ("update-" + "b" * 32 + ".ack")

            acknowledge_update_startup(reference, acknowledgement, data_dir=data_dir)

            self.assertEqual(acknowledgement.read_text(encoding="ascii"), reference)
            with self.assertRaises(ValueError):
                acknowledge_update_startup(reference, data_dir / acknowledgement.name, data_dir=data_dir)
            with self.assertRaises(ValueError):
                acknowledge_update_startup("main", acknowledgement, data_dir=data_dir)

    def test_executable_handoff_does_not_reacquire_the_helpers_update_lock(self):
        config = {"install_dir": "install", "data_dir": "data"}
        with patch("pyOSgui.recover_source_update", return_value={"state": "recovered"}) as recover:
            self.assertIsNone(
                recover_startup_source_update(config, executable_handoff=True)
            )
            recover.assert_not_called()
            self.assertEqual(
                recover_startup_source_update(config), {"state": "recovered"}
            )
            recover.assert_called_once_with("install", "data")


class StartupStatusFormattingTests(unittest.TestCase):
    def test_status_lines_are_normalized_and_aligned(self):
        self.assertEqual(
            pyOSgui.format_startup_status("warn", "Disk space\n is low", "12:34:56"),
            "[12:34:56] [WARN ] Disk space is low",
        )

    def test_unknown_status_levels_are_treated_as_debug(self):
        line = pyOSgui.format_startup_status("trace", "checking", "01:02:03")
        self.assertEqual(line, "[01:02:03] [DEBUG] checking")

class SudokuSafetyTests(unittest.TestCase):
    UNIQUE_PUZZLE = [
        [5, 3, 0, 0, 7, 0, 0, 0, 0],
        [6, 0, 0, 1, 9, 5, 0, 0, 0],
        [0, 9, 8, 0, 0, 0, 0, 6, 0],
        [8, 0, 0, 0, 6, 0, 0, 0, 3],
        [4, 0, 0, 8, 0, 3, 0, 0, 1],
        [7, 0, 0, 0, 2, 0, 0, 0, 6],
        [0, 6, 0, 0, 0, 0, 2, 8, 0],
        [0, 0, 0, 4, 1, 9, 0, 0, 5],
        [0, 0, 0, 0, 8, 0, 0, 7, 9],
    ]

    def test_unique_puzzle_has_one_solution(self):
        self.assertEqual(count_sudoku_solutions(self.UNIQUE_PUZZLE), 1)

    def test_invalid_or_ambiguous_boards_are_not_accepted_as_unique(self):
        duplicate = [row[:] for row in self.UNIQUE_PUZZLE]
        duplicate[0][2] = 5
        self.assertEqual(count_sudoku_solutions(duplicate), 0)

        ambiguous = [row[:] for row in self.UNIQUE_PUZZLE]
        for row in ambiguous:
            for column, value in enumerate(row):
                if value in {1, 2}:
                    row[column] = 0
        self.assertEqual(count_sudoku_solutions(ambiguous, limit=2), 2)

    def test_board_shape_and_cell_types_are_validated(self):
        with self.assertRaises(ValueError):
            count_sudoku_solutions([[0] * 9] * 8)
        malformed = [[0] * 9 for _ in range(9)]
        malformed[0][0] = "1"
        with self.assertRaises(ValueError):
            count_sudoku_solutions(malformed)


if __name__ == "__main__":
    unittest.main()
