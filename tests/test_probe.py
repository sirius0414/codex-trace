"""M0 probe boundaries using only synthetic metadata and temporary files."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from platform_fixtures import assert_private_mode, symlink

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from codex_display.platform import filesystem as os
from codex_display import __version__
from codex_display.probe import (DIAGNOSTIC_CODES, DIAGNOSTIC_PHASES, MAX_DIAGNOSTIC_ELAPSED_MS,
                                 ProbeService, candidate_identity, inspect_source, probe_zip)

THREAD_A = "12345678-1234-1234-1234-123456789abc"
THREAD_B = "87654321-4321-4321-4321-cba987654321"
UI_INSTANCE = "abcdef12-1234-1234-1234-123456789abc"


class IdentityProbeTests(unittest.TestCase):
    def test_missing_identity_never_uses_process_environment(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_A}):
            result = candidate_identity({})
        self.assertIsNone(result["candidateThreadId"])
        self.assertEqual(result["identityReason"], "missing-identity")

    def test_candidate_is_never_declared_verified(self):
        service = ProbeService()
        result = service.activate({"thread_id": THREAD_A})
        self.assertEqual(result["candidateThreadId"], THREAD_A)
        self.assertEqual(result["identityReason"], "unverified-global-semantics")
        self.assertEqual(result["entrypoint"], "global")
        self.assertEqual(result["serverVersion"], __version__)
        self.assertFalse(result["automaticIdentityVerified"])
        self.assertFalse(result["readOnlyLiveEvents"])

    def test_thread_activation_records_its_surface_and_version_without_trusting_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            service = ProbeService(audit)
            result = service.activate({"thread_id": THREAD_A, "entrypoint": "global", "serverVersion": "fake"},
                                      entrypoint="thread")
            self.assertEqual(result["entrypoint"], "thread")
            self.assertEqual(result["serverVersion"], __version__)
            self.assertEqual(result["identityReason"], "unverified-thread-semantics")
            self.assertEqual(result["candidateThreadId"], THREAD_A)
            self.assertFalse(result["automaticIdentityVerified"])
            self.assertFalse(result["readOnlyLiveEvents"])
            record = json.loads(audit.read_text())
            self.assertEqual(record["entrypoint"], "thread")
            self.assertEqual(record["serverVersion"], __version__)
            self.assertEqual(record["identityReason"], "unverified-thread-semantics")

    def test_thread_entrypoint_keeps_missing_invalid_and_conflicting_identity_rules(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_A}):
            cases = [({}, "missing-identity"), ({"thread_id": "invalid"}, "invalid-identity"),
                     ({"thread_id": THREAD_A, "threadId": THREAD_B}, "conflicting-identities")]
            for metadata, reason in cases:
                with self.subTest(reason=reason):
                    result = candidate_identity(metadata, entrypoint="thread")
                    self.assertIsNone(result["candidateThreadId"])
                    self.assertEqual(result["identityReason"], reason)

    def test_unknown_entrypoint_is_rejected_without_activation_or_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            service = ProbeService(audit)
            with self.assertRaisesRegex(ValueError, "invalid-entrypoint"):
                service.activate({}, entrypoint="arbitrary")
            self.assertFalse(service.activations)
            self.assertFalse(audit.exists())

    def test_conflicting_metadata_refuses_to_choose(self):
        result = candidate_identity({"thread_id": THREAD_A, "threadId": THREAD_B})
        self.assertIsNone(result["candidateThreadId"])
        self.assertEqual(result["identityReason"], "conflicting-identities")

    def test_equal_case_normalized_ids_are_one_unverified_candidate(self):
        result = candidate_identity({"thread_id": THREAD_A.upper(), "threadId": THREAD_A})
        self.assertEqual(result["candidateThreadId"], THREAD_A)
        self.assertEqual(result["identityReason"], "unverified-global-semantics")

    def test_malformed_id_refuses_even_a_second_valid_value(self):
        for invalid in (None, 1, {}, [], True, "", "not-a-thread", THREAD_A.replace("-", ""), "{" + THREAD_A + "}"):
            with self.subTest(invalid=invalid):
                result = candidate_identity({"thread_id": invalid, "threadId": THREAD_A})
                self.assertIsNone(result["candidateThreadId"])
                self.assertEqual(result["identityReason"], "invalid-identity")

    def test_unrecognized_fields_do_not_supply_identity(self):
        result = candidate_identity({"currentThreadId": THREAD_A, "session": THREAD_B})
        self.assertEqual(result["identityReason"], "missing-identity")

    def test_audit_stores_keys_and_bound_ids_but_not_metadata_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit" / "events.jsonl"
            service = ProbeService(path)
            activation = service.activate({"thread_id": THREAD_A, "arbitraryField": "DO_NOT_LOG_PRIVATE_CONTENT"})
            service.observe(activation["activationId"], UI_INSTANCE, ["theme", "displayMode"], "fullscreen")
            data = path.read_text()
            self.assertNotIn("DO_NOT_LOG_PRIVATE_CONTENT", data)
            self.assertIn("arbitraryField", data)
            assert_private_mode(self, path, 0o600)
            assert_private_mode(self, path.parent, 0o700)
            self.assertEqual(len(data.splitlines()), 2)

    def test_observation_requires_known_activation_and_valid_instance(self):
        service = ProbeService()
        with self.assertRaisesRegex(ValueError, "unknown-activation"):
            service.observe("missing", UI_INSTANCE, [], None)
        activation = service.activate({})
        with self.assertRaises(ValueError):
            service.observe(activation["activationId"], "invalid", [], None)

    def test_activation_history_is_bounded(self):
        service = ProbeService()
        first = service.activate({})["activationId"]
        for _ in range(256):
            service.activate({})
        self.assertEqual(len(service.activations), 256)
        self.assertNotIn(first, service.activations)


class DiagnosticProbeTests(unittest.TestCase):
    def test_whitelisted_milestones_are_bound_and_only_small_metadata_is_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            service = ProbeService(audit)
            activation = service.activate({"privateValue": "SYNTHETIC_BODY_MUST_NOT_BE_LOGGED"})
            for phase in DIAGNOSTIC_PHASES:
                result = service.diagnostic_event(activation["activationId"], UI_INSTANCE.upper(), phase,
                                                  code="save-requested", elapsed_ms=0, visibility="visible",
                                                  context_keys=["theme", "displayMode", "theme"],
                                                  host_capability_keys=["serverTools", "downloadFile"])
                self.assertTrue(result["recorded"])
                self.assertEqual(result["uiInstanceId"], UI_INSTANCE)
                self.assertEqual(result["contextKeys"], ["theme", "displayMode"])
            self.assertIn("digest-failed", DIAGNOSTIC_CODES)
            self.assertIn("invalid-manifest", DIAGNOSTIC_CODES)
            for code in DIAGNOSTIC_CODES:
                self.assertTrue(service.diagnostic_event(activation["activationId"], UI_INSTANCE,
                                                        "download-error", code=code)["recorded"])
            text = audit.read_text()
            self.assertNotIn("SYNTHETIC_BODY_MUST_NOT_BE_LOGGED", text)
            rows = [json.loads(line) for line in text.splitlines()][1:]
            self.assertEqual(len(rows), len(DIAGNOSTIC_PHASES) + len(DIAGNOSTIC_CODES))
            expected = {"atMs", "kind", "activationId", "uiInstanceId", "phase", "code", "elapsedMs",
                        "visibility", "contextKeys", "hostCapabilityKeys"}
            for row in rows:
                self.assertEqual(row["kind"], "diagnostic-event")
                self.assertLessEqual(set(row), expected)
                self.assertEqual(row["activationId"], activation["activationId"])
            assert_private_mode(self, audit, 0o600)

    def test_invalid_or_unbound_diagnostics_are_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            service = ProbeService(audit)
            activation = service.activate({})["activationId"]
            baseline = audit.read_bytes()
            cases = [
                {"activation_id": THREAD_B}, {"activation_id": []},
                {"ui_instance_id": "private invalid id"}, {"ui_instance_id": UI_INSTANCE.replace("-", "")},
                {"phase": "arbitrary-error-message"}, {"code": "secret exception content"},
                {"elapsed_ms": -1}, {"elapsed_ms": MAX_DIAGNOSTIC_ELAPSED_MS + 1},
                {"elapsed_ms": True}, {"elapsed_ms": "10"}, {"elapsed_ms": 0.5},
                {"visibility": "background"}, {"context_keys": {"theme": "secret value"}},
                {"context_keys": ["field"] * 101}, {"host_capability_keys": ["field"] * 101},
                {"context_keys": ["x" * 201]}, {"host_capability_keys": ["raw body text"]},
                {"context_keys": [{"field": "private value"}]}, {"context_keys": ["line\nbody"]},
            ]
            for case in cases:
                with self.subTest(case=case):
                    kwargs = {"activation_id": activation, "ui_instance_id": UI_INSTANCE,
                              "phase": "page-ready", **case}
                    with self.assertRaises(ValueError):
                        service.diagnostic_event(**kwargs)
                    self.assertEqual(audit.read_bytes(), baseline)

    def test_optional_values_are_omitted_and_bounds_keep_true_zero(self):
        service = ProbeService()
        activation = service.activate({})["activationId"]
        minimal = service.diagnostic_event(activation, UI_INSTANCE, "page-ready")
        self.assertEqual(set(minimal), {"recorded", "activationId", "uiInstanceId", "phase"})
        for elapsed in (0, MAX_DIAGNOSTIC_ELAPSED_MS):
            result = service.diagnostic_event(activation, UI_INSTANCE, "visibility-change",
                                              visibility="hidden", elapsed_ms=elapsed,
                                              context_keys=["field"] * 100)
            self.assertEqual(result["elapsedMs"], elapsed)
            self.assertEqual(result["contextKeys"], ["field"])


class DownloadProbeTests(unittest.TestCase):
    def test_zip_preserves_unknown_bad_bytes_and_partial_line(self):
        first = probe_zip()
        self.assertEqual(first, probe_zip())
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "records/synthetic.jsonl"})
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("manifest.json"))
            raw = archive.read("records/synthetic.jsonl")
            self.assertFalse(manifest["realTaskRecords"])
            self.assertEqual(manifest["bytes"], len(raw))
            self.assertEqual(manifest["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIn(b"\r\n", raw)
            self.assertIn(b"\xffbad\n", raw)
            self.assertTrue(raw.endswith(b'{"partial":'))
            for entry in archive.infolist():
                self.assertEqual((entry.external_attr >> 16) & 0o777, 0o600)

    def test_download_payload_and_checksums_match_zip(self):
        service = ProbeService()
        result = service.download_chunk(0)
        decoded = base64.b64decode(result["data"], validate=True)
        self.assertEqual(decoded, service.download)
        self.assertEqual(result["chunkSha256"], hashlib.sha256(decoded).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(service.download).hexdigest())
        self.assertEqual(result["totalBytes"], len(decoded))
        self.assertEqual(result["nextOffset"], len(decoded))
        self.assertTrue(result["synthetic"])
        self.assertTrue(result["done"])
        self.assertEqual(result, service.download_chunk(0))

    def test_chunk_boundaries_retry_and_eof(self):
        service = ProbeService()
        service.download = bytes(range(256)) * 600
        offset = 0
        collected = []
        while offset < len(service.download):
            result = service.download_chunk(offset)
            self.assertEqual(result, service.download_chunk(offset))
            chunk = base64.b64decode(result["data"], validate=True)
            self.assertLessEqual(len(chunk), 65536)
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["chunkSha256"], hashlib.sha256(chunk).hexdigest())
            self.assertGreater(result["nextOffset"], offset)
            offset = result["nextOffset"]
            collected.append(chunk)
        self.assertEqual(b"".join(collected), service.download)
        final = service.download_chunk(offset)
        self.assertEqual(final["data"], "")
        self.assertEqual(final["nextOffset"], offset)
        self.assertTrue(final["done"])

    def test_every_successful_chunk_read_audits_counts_and_hashes_but_not_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            service = ProbeService(audit)
            service.download = b"SYNTHETIC_PRIVATE_DOWNLOAD_BODY"
            first = service.download_chunk(0)
            retry = service.download_chunk(0)
            eof = service.download_chunk(len(service.download))
            with self.assertRaises(ValueError):
                service.download_chunk(-1)
            self.assertEqual(first, retry)
            text = audit.read_text()
            self.assertNotIn(service.download.decode(), text)
            self.assertNotIn(first["data"], text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(len(rows), 3)
            for row, reply in zip(rows, (first, retry, eof)):
                self.assertEqual(set(row), {"atMs", "kind", "offset", "bytes", "sha256", "chunkSha256"})
                self.assertEqual(row["kind"], "download-read")
                self.assertEqual(row["offset"], reply["offset"])
                self.assertEqual(row["bytes"], len(base64.b64decode(reply["data"])))
                self.assertEqual(row["sha256"], reply["sha256"])
                self.assertEqual(row["chunkSha256"], reply["chunkSha256"])

    def test_invalid_offsets_have_one_controlled_error(self):
        service = ProbeService()
        for invalid in (-1, len(service.download) + 1, True, False, 0.0, 1.5, "0", None, [], {}):
            with self.subTest(offset=invalid), self.assertRaisesRegex(ValueError, "^invalid-offset$"):
                service.download_chunk(invalid)


class SourceProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS /var and /tmp may themselves be symlinks; fixture roots are canonical.
        self.base = Path(self.temporary.name).resolve()
        self.codex_home = self.base / "codex"
        self.sessions = self.codex_home / "sessions"
        self.archived = self.codex_home / "archived_sessions"
        self.sessions.mkdir(parents=True)
        self.archived.mkdir()

    def write_source(self, path=None, *, identifier=THREAD_A, payload=None, body=b"private synthetic body\n"):
        path = path or self.sessions / "trace.jsonl"
        metadata = {"id": identifier, "history_mode": "synthetic", **(payload or {})}
        header = json.dumps({"type": "session_meta", "payload": metadata}).encode() + b"\n"
        path.write_bytes(header + body)
        return path, header

    def inspect(self, path):
        return inspect_source(str(path) if path is not None else None, THREAD_A, self.codex_home)

    def test_regular_identity_checked_segment_is_read_only_and_metadata_only(self):
        path, header = self.write_source(payload={"history_base": {"opaque": "not returned"}})
        original = path.read_bytes()
        modified = path.stat().st_mtime_ns
        result = self.inspect(path)
        self.assertTrue(result["readable"])
        self.assertEqual(result["physicalBytes"], len(original))
        self.assertEqual(result["headerSha256"], hashlib.sha256(header).hexdigest())
        self.assertTrue(result["hasHistoryBase"])
        self.assertFalse(result["readOnlyLiveEvents"])
        self.assertNotIn("private synthetic body", json.dumps(result))
        self.assertNotIn("not returned", json.dumps(result))
        self.assertIn("lineage not traversed", result["coverage"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_archived_segment_is_an_allowed_root(self):
        path, _ = self.write_source(self.archived / "trace.jsonl")
        self.assertTrue(self.inspect(path)["readable"])

    def test_missing_or_outside_source_never_uses_another_file(self):
        self.assertEqual(self.inspect(None)["reason"], "source-not-recorded")
        outside, _ = self.write_source(self.base / "outside.jsonl")
        for path in (outside, Path("sessions/relative.jsonl"), self.base / "codex" / "sessions-other" / "trace.jsonl"):
            with self.subTest(path=path):
                self.assertEqual(self.inspect(path)["reason"], "outside-session-roots")
        self.assertFalse(self.inspect(self.sessions / "absent.jsonl")["readable"])

    def test_identity_mismatch_and_wrong_header_type_are_rejected(self):
        path, _ = self.write_source(identifier=THREAD_B)
        self.assertEqual(self.inspect(path)["reason"], "source-identity-mismatch")
        path.write_text(json.dumps({"type": "response_item", "payload": {"id": THREAD_A}}) + "\n")
        self.assertEqual(self.inspect(path)["reason"], "source-identity-mismatch")

    def test_invalid_and_nested_headers_return_bounded_failure(self):
        path = self.sessions / "invalid.jsonl"
        for header in (b"bad json\n", b"\xff\n", b"null\n", b"[]\n", b'{"type":"session_meta","payload":null}\n', b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"):
            path.write_bytes(header)
            with self.subTest(header=header[:60]):
                self.assertFalse(self.inspect(path)["readable"])

    def test_incomplete_and_oversized_headers_are_rejected_before_json_parse(self):
        path = self.sessions / "large.jsonl"
        for header in (b'{"type":"session_meta"}', b"x" * (1024 * 1024 + 1) + b"\n"):
            path.write_bytes(header)
            with self.subTest(length=len(header)):
                self.assertEqual(self.inspect(path)["reason"], "header-limit-or-incomplete")

    def test_file_and_parent_symlinks_are_rejected_even_with_matching_identity(self):
        target, _ = self.write_source(self.base / "target.jsonl")
        link = self.sessions / "link.jsonl"
        symlink(self, link, target)
        self.assertEqual(self.inspect(link)["reason"], "symlink-or-noncanonical-source")
        parent = self.sessions / "linked-parent"
        symlink(self, parent, self.base, target_is_directory=True)
        self.assertEqual(self.inspect(parent / "target.jsonl")["reason"], "symlink-or-noncanonical-source")

    def test_parent_swapped_after_canonical_check_is_not_followed(self):
        inside = self.sessions / "nested"
        inside.mkdir()
        path, _ = self.write_source(inside / "trace.jsonl")
        outside = self.base / "outside"
        outside.mkdir()
        self.write_source(outside / "trace.jsonl", body=b"must not read this outside source")
        original_resolve = Path.resolve

        def swap_after_resolve(current, *args, **kwargs):
            resolved = original_resolve(current, *args, **kwargs)
            if current == path:
                inside.rename(self.sessions / "old-nested")
                symlink(self, inside, outside, target_is_directory=True)
            return resolved

        with mock.patch.object(Path, "resolve", swap_after_resolve):
            self.assertFalse(self.inspect(path)["readable"])

    def test_canonical_path_failure_is_handled(self):
        path, _ = self.write_source()
        with mock.patch.object(Path, "resolve", side_effect=OSError("synthetic failure")):
            self.assertFalse(self.inspect(path)["readable"])

    def test_directory_is_rejected_as_a_source(self):
        self.assertEqual(self.inspect(self.sessions)["reason"], "not-regular")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture; Windows special handles have separate native tests")
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.sessions / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(self.inspect(fifo)["reason"], "not-regular")

    def test_non_scalar_history_mode_is_not_returned_or_logged(self):
        path, _ = self.write_source(payload={"history_mode": {"private": "DO_NOT_RETURN"}})
        result = self.inspect(path)
        self.assertTrue(result["readable"])
        self.assertIsNone(result["historyMode"])
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
