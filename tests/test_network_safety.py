import json
import tempfile
import threading
import unittest
from pathlib import Path

from pyOSgui import (
    MAX_MESSENGER_HISTORY,
    MAX_MESSENGER_HISTORY_BYTES,
    MAX_MESSENGER_HISTORY_IMAGE_BYTES,
    PeerMessenger,
    persist_ssh_host_key,
)


class _FakeKey:
    def __init__(self, name, value):
        self._name = name
        self.value = value

    def get_name(self):
        return self._name


class _FakeHostKeys:
    def __init__(self):
        self.entries = {}

    def add(self, hostname, key_type, key):
        self.entries[f"{hostname}|{key_type}"] = key.value

    def load(self, path):
        self.entries = json.loads(Path(path).read_text(encoding="utf-8"))

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.entries, sort_keys=True), encoding="utf-8"
        )


class _FakeParamiko:
    class SSHException(Exception):
        pass

    HostKeys = _FakeHostKeys


class MessengerHistoryTests(unittest.TestCase):
    def test_history_enforces_count_and_total_byte_limits(self):
        service = PeerMessenger("tester", None)
        for index in range(MAX_MESSENGER_HISTORY + 10):
            service._remember({"kind": "text", "text": str(index)})

        self.assertEqual(service.history_usage()["messages"], MAX_MESSENGER_HISTORY)
        self.assertEqual(service.history_snapshot()[0]["text"], "10")

        for index in range(8):
            service._remember(
                {"kind": "text", "text": f"{index}:" + ("x" * (4 * 1024 * 1024))}
            )

        usage = service.history_usage()
        self.assertLessEqual(usage["bytes"], MAX_MESSENGER_HISTORY_BYTES)
        self.assertLess(usage["messages"], MAX_MESSENGER_HISTORY)
        self.assertTrue(all(message["kind"] == "text" for message in service.history_snapshot()))

    def test_old_image_payloads_are_stripped_without_mutating_live_messages(self):
        service = PeerMessenger("tester", None)
        original = {"kind": "image", "filename": "picture.png", "data": b"x" * (5 * 1024 * 1024)}
        service._remember(original)
        for index in range(3):
            service._remember(
                {
                    "kind": "image",
                    "filename": f"picture-{index}.png",
                    "data": bytes([index]) * (5 * 1024 * 1024),
                }
            )

        usage = service.history_usage()
        snapshot = service.history_snapshot()
        self.assertLessEqual(
            usage["image_bytes"], MAX_MESSENGER_HISTORY_IMAGE_BYTES
        )
        self.assertLessEqual(usage["bytes"], MAX_MESSENGER_HISTORY_BYTES)
        self.assertTrue(any(message.get("data_evicted") for message in snapshot))
        self.assertIn("data", original)
        snapshot[0]["filename"] = "changed"
        self.assertNotEqual(
            service.history_snapshot()[0]["filename"], "changed"
        )


class SSHKnownHostsTests(unittest.TestCase):
    def test_concurrent_accepts_merge_into_one_atomic_known_hosts_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "known_hosts"
            failures = []

            def save(index):
                try:
                    persist_ssh_host_key(
                        _FakeParamiko,
                        path,
                        f"host-{index}.example",
                        _FakeKey("ssh-ed25519", f"key-{index}"),
                    )
                except Exception as error:
                    failures.append(error)

            threads = [threading.Thread(target=save, args=(index,)) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored), 16)
            for index in range(16):
                self.assertEqual(
                    stored[f"host-{index}.example|ssh-ed25519"], f"key-{index}"
                )
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

