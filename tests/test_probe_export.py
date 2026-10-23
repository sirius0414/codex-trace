"""M0 export retry scope and concurrency, without filesystem or task access."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys
import threading
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from codex_display.probe import ProbeService
from codex_display.export_store import ExportUnconfirmedError


class Store:
    def __init__(self):
        self.calls = 0
        self.present = True
        self.started = threading.Event()
        self.release = threading.Event()

    def save_bytes(self, data, prefix):
        self.calls += 1
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test barrier")
        return {"path": "/synthetic/export.zip", "filename": "export.zip", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "verified": True}

    def verify_saved(self, saved):
        if not self.present:
            raise ValueError("saved-file-unavailable")
        return saved


class ProbeExportTests(unittest.TestCase):
    def setUp(self):
        self.service = ProbeService()
        self.activation = self.service.activate({}, entrypoint="thread")["activationId"]
        self.ui, self.request = str(uuid4()), str(uuid4())
        self.store = Store()

    def export(self):
        return self.service.export_records(self.activation, self.ui, self.request, self.store)

    def test_inflight_retry_cannot_create_a_second_file(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.export)
            self.assertTrue(self.store.started.wait(5))
            retry_entered = threading.Event()
            def retry():
                retry_entered.set()
                return self.export()
            second = pool.submit(retry)
            self.assertTrue(retry_entered.wait(5))
            self.store.release.set()
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        self.assertEqual(self.store.calls, 1)

    def test_retry_checks_existing_file_instead_of_recreating_missing_output(self):
        self.store.release.set()
        self.export()
        self.store.present = False
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.export()
        self.assertEqual(self.store.calls, 1)

    def test_request_cannot_be_rebound_to_another_ui_or_activation(self):
        self.store.release.set()
        self.export()
        another = self.service.activate({}, entrypoint="thread")["activationId"]
        for activation, ui in [(self.activation, str(uuid4())), (another, self.ui)]:
            with self.assertRaisesRegex(ValueError, "scope-mismatch"):
                self.service.export_records(activation, ui, self.request, self.store)
        self.assertEqual(self.store.calls, 1)

    def test_restart_rejects_old_activation_without_new_write(self):
        with self.assertRaisesRegex(ValueError, "unknown-activation"):
            ProbeService().export_records(self.activation, self.ui, self.request, self.store)
        self.assertEqual(self.store.calls, 0)

    def test_retry_recovers_published_but_unconfirmed_file_without_second_save(self):
        class UnconfirmedStore(Store):
            def save_bytes(self, data, prefix):
                saved = super().save_bytes(data, prefix)
                raise ExportUnconfirmedError(saved)

            def verify_saved(self, saved):
                if not self.present:
                    raise ValueError("saved-file-unavailable")
                return {**saved, "verified": True}

        self.store = UnconfirmedStore()
        self.store.release.set()
        with self.assertRaisesRegex(ExportUnconfirmedError, "export-unconfirmed"):
            self.export()
        self.assertFalse(self.service.exports[self.request][1]["verified"])
        recovered = self.export()
        self.assertTrue(recovered["verified"])
        self.assertEqual(recovered["requestId"], self.request)
        self.assertEqual(recovered, self.export())
        self.assertEqual(self.store.calls, 1)


if __name__ == "__main__":
    unittest.main()
