"""Synthetic contract validation; these do not stand in for adapter/host tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
FIXTURES = ROOT / "tests" / "fixtures" / "contracts"
SCHEMA = json.loads((CONTRACTS / "trajectory.schema.json").read_text())


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def validate(definition: str, instance: object) -> None:
    schema = {**SCHEMA, "$ref": f"#/$defs/{definition}"}
    Draft202012Validator(schema).validate(instance)


class ContractTests(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(SCHEMA)

    def test_generated_types_match_schema(self):
        subprocess.run(
            [sys.executable, str(CONTRACTS / "generate_types.py"), "--check"],
            check=True,
            cwd=ROOT,
        )

    def test_every_catalogued_fixture(self):
        catalog = read("catalog.json")
        self.assertTrue(catalog["syntheticOnly"])
        for fixture in catalog["fixtures"]:
            with self.subTest(fixture=fixture["file"]):
                validate(fixture["definition"], read(fixture["file"]))

    def test_all_service_inputs_and_outputs_have_defined_contracts(self):
        tools = json.loads((CONTRACTS / "tools.json").read_text())
        requests = read("requests.json")
        self.assertEqual(len(tools["tools"]), 9)
        for name, contract in tools["tools"].items():
            with self.subTest(tool=name):
                self.assertIn(contract["input"], SCHEMA["$defs"])
                self.assertIn(contract["output"], SCHEMA["$defs"])
                validate(contract["input"], requests[contract["input"]])

    def test_unknown_record_kinds_cannot_become_ui_events(self):
        event = read("snapshot-running.json")["events"][0]
        event["kind"] = "unknown"
        with self.assertRaises(ValidationError):
            validate("TraceEvent", event)

    def test_previews_are_single_line(self):
        event = read("snapshot-running.json")["events"][0]
        for text in ("line one\nline two", "line one\rline two"):
            with self.subTest(text=text):
                event["preview"] = text
                with self.assertRaises(ValidationError):
                    validate("TraceEvent", event)

    def test_zero_and_missing_usage_are_distinct(self):
        validate("UsageMetric", {"availability": "recorded", "value": 0})
        validate("UsageMetric", {"availability": "notRecorded", "value": None})
        for invalid in (
            {"availability": "notRecorded", "value": 0},
            {"availability": "recorded", "value": None},
            {"availability": "recorded", "value": -1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate("UsageMetric", invalid)

    def test_timeline_origin_requires_recorded_creation_precision_or_explicit_missing(self):
        known = {"timestampMs": 0, "basis": "threadCreatedAt", "precision": "second", "explanation": "Recorded creation reference; not execution duration."}
        absent = {"timestampMs": None, "basis": "notRecorded", "precision": "notRecorded", "explanation": "Creation was not recorded."}
        validate("TimelineOrigin", known)
        validate("TimelineOrigin", absent)
        for invalid in (
            {**known, "timestampMs": None}, {**absent, "timestampMs": 0},
            {**known, "timestampMs": 1}, {**known, "timestampMs": -1000},
            {**known, "precision": "millisecond"}, {**known, "basis": "firstChildEvent"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate("TimelineOrigin", invalid)
        for definition, fixture in (("ThreadSnapshot", "snapshot-running.json"), ("ChangeBatch", "change-complete.json")):
            payload = read(fixture)
            del payload["timelineOrigin"]
            with self.subTest(definition=definition), self.assertRaises(ValidationError):
                validate(definition, payload)

    def test_task_bound_responses_require_full_binding(self):
        for field in ("sourceInstanceId", "viewSessionId", "selectionEpoch", "threadId"):
            snapshot = read("snapshot-running.json")
            del snapshot["binding"][field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate("ThreadSnapshot", snapshot)

    def test_execution_intervals_are_optional_turn_metadata_using_timing_contract(self):
        turn = read("snapshot-running.json")["turns"][0]
        validate("Turn", turn)
        timing = {"startMs": 0, "endMs": None, "durationMs": None, "precision": "startOnly",
                  "originalUnit": "seconds", "basis": "Synthetic recorded turn start", "source": None}
        validate("Turn", {**turn, "executionIntervals": [timing]})
        for invalid in (None, [], [True], [{**timing, "startMs": True}], [{**timing, "endMs": 10}]):
            with self.subTest(intervals=invalid), self.assertRaises(ValidationError):
                validate("Turn", {**turn, "executionIntervals": invalid})
        event = read("snapshot-running.json")["events"][0]
        with self.assertRaises(ValidationError):
            validate("TraceEvent", {**event, "executionIntervals": [timing]})

    def test_client_cannot_supply_source_file_paths(self):
        source = read("snapshot-running.json")["events"][0]["sources"][0]
        source["path"] = "/private/unrelated.txt"
        with self.assertRaises(ValidationError):
            validate("SourceRef", source)
        request = read("requests.json")["ReadEventRequest"]
        request["path"] = "/private/unrelated.txt"
        with self.assertRaises(ValidationError):
            validate("ReadEventRequest", request)

    def test_export_request_rejects_ui_filters_and_arbitrary_offsets(self):
        for extra in ({"filters": {}}, {"listCursor": "page-2"}, {"offset": 0}, {"path": "/private/file"}):
            request = {**read("requests.json")["DownloadRequest"], **extra}
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                validate("DownloadRequest", request)

    def test_export_request_actions_are_separate(self):
        binding = read("snapshot-running.json")["binding"]
        for action in ("status", "release"):
            validate("DownloadRequest", {"binding": binding, "action": action, "exportId": "export-1"})
        validate("DownloadRequest", {"binding": binding, "action": "read", "exportId": "export-1", "cursor": None})
        validate("DownloadRequest", {"binding": binding, "action": "commit", "exportId": "export-1", "requestId": "commit-synthetic-a"})
        # Recovery needs the original request key even if the commit response was lost.
        validate("DownloadRequest", {"binding": binding, "action": "result", "requestId": "commit-synthetic-a"})
        with self.assertRaises(ValidationError):
            validate("DownloadRequest", {"binding": binding, "action": "read", "exportId": "export-1"})

    def test_create_commit_and_result_require_a_bounded_idempotency_key(self):
        binding = read("snapshot-running.json")["binding"]
        for action in ("create", "commit", "result"):
            request = {"binding": binding, "action": action}
            if action == "commit":
                request["exportId"] = "export-1"
            with self.subTest(action=action), self.assertRaises(ValidationError):
                validate("DownloadRequest", request)
            for key in ("", "short", "x" * 129, "../destination", "key-with-newline\n"):
                with self.subTest(action=action, key=key), self.assertRaises(ValidationError):
                    validate("DownloadRequest", {**request, "requestId": key})

    def test_no_download_action_accepts_a_client_save_destination(self):
        binding = read("snapshot-running.json")["binding"]
        requests = [
            {"action": "create", "requestId": "create-synthetic-a"},
            {"action": "status", "exportId": "export-1"},
            {"action": "read", "exportId": "export-1", "cursor": None},
            {"action": "release", "exportId": "export-1"},
            {"action": "commit", "exportId": "export-1", "requestId": "commit-synthetic-a"},
            {"action": "result", "requestId": "commit-synthetic-a"},
        ]
        for request in requests:
            for field in ("path", "targetPath", "directory", "filename", "overwrite"):
                with self.subTest(action=request["action"], field=field), self.assertRaises(ValidationError):
                    validate("DownloadRequest", {"binding": binding, **request, field: "/private/other.zip"})

    def test_verified_save_requires_a_complete_receipt(self):
        result = read("export-save-verified.json")
        for field in ("requestId", "filename", "path", "bytes", "sha256", "verifiedAt"):
            changed = read("export-save-verified.json")
            del changed["save"][field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate("DownloadResult", changed)
        for field, invalid in (("bytes", 0), ("sha256", "not-a-hash"), ("sha256", "a" * 64 + "\n"), ("verifiedAt", -1)):
            changed = read("export-save-verified.json")
            changed["save"][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate("DownloadResult", changed)
        self.assertEqual(result["save"]["filename"], result["filename"])
        self.assertEqual(result["save"]["bytes"], result["totalBytes"])
        self.assertEqual(Path(result["save"]["path"]).name, result["save"]["filename"])

    def test_receipt_path_and_filename_are_local_zip_outputs(self):
        for path in (
            "https://example.test/file.zip", "~/Downloads/Codex Display/codex-display-a.zip",
            "/Users/synthetic/Desktop/codex-display-a.zip",
            "/Users/synthetic/../other/Downloads/Codex Display/codex-display-a.zip",
            "/Users/synthetic/Downloads/Codex Display/codex-display-a.zip\n",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                validate("LocalExportPath", path)
        for name in ("../codex-display-a.zip", "codex-display-a.txt", "codex-display-a.zip\n"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate("ExportFilename", name)

    def test_ready_and_uncertain_save_states_cannot_claim_verified_delivery(self):
        ready = read("export-ready.json")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["save"], {"status": "notRequested", "requestId": None})
        for name in ("export-save-pending.json", "export-save-unknown.json", "export-save-failed.json"):
            result = read(name)
            result["save"]["path"] = "/Users/synthetic/Downloads/Codex Display/codex-display-a.zip"
            with self.subTest(fixture=name), self.assertRaises(ValidationError):
                validate("DownloadResult", result)
        failed = read("export-save-failed.json")
        for code in ("timeout", "cancelled", "connectionLost", "requestNotRecorded"):
            failed["save"]["code"] = code
            with self.subTest(code=code), self.assertRaises(ValidationError):
                validate("DownloadResult", failed)

    def test_missing_commit_record_has_an_explicit_unknown_result(self):
        result = read("export-save-unknown.json")
        validate("DownloadResult", result)
        self.assertIsNone(result["exportId"])
        self.assertEqual(result["save"]["reason"], "requestNotRecorded")
        result["save"] = {"status": "notRequested", "requestId": None}
        with self.assertRaises(ValidationError):
            validate("DownloadResult", result)

    def test_release_can_retain_pending_or_verified_save_receipts(self):
        for name in ("export-save-pending.json", "export-save-verified.json"):
            result = read(name)
            result.update(status="released", expiresAt=None, manifest=None, chunk=None)
            with self.subTest(fixture=name):
                validate("DownloadResult", result)
            result["status"] = "collecting"
            with self.subTest(fixture=name), self.assertRaises(ValidationError):
                validate("DownloadResult", result)

    def test_download_tool_preserves_one_interface_and_declares_fixed_delivery(self):
        tools = json.loads((CONTRACTS / "tools.json").read_text())["tools"]
        delivery = tools["download_records"]["delivery"]
        self.assertEqual(delivery["directory"], "~/Downloads/Codex Display/")
        self.assertFalse(delivery["clientSelectsDestination"])
        self.assertEqual(delivery["saveActions"], ["commit", "result"])
        self.assertEqual(delivery["successCondition"], "save.status == verified")

    def test_export_cannot_claim_a_cross_file_atomic_snapshot(self):
        manifest = read("export-manifest-partial.json")
        manifest["atomicSnapshot"] = True
        with self.assertRaises(ValidationError):
            validate("ExportManifest", manifest)

    def test_export_entry_names_cannot_escape_the_records_directory(self):
        source = read("export-manifest-partial.json")["sources"][0]
        for name in ("records/../escape.bin", "records/task/../../escape.bin", "/private/data.bin", "records/task/\\escape.bin"):
            source["entryName"] = name
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate("ExportSource", source)

    def test_start_only_timing_cannot_claim_a_duration(self):
        timing = read("snapshot-running.json")["events"][0]["timing"]
        timing["durationMs"] = 1000
        with self.assertRaises(ValidationError):
            validate("Timing", timing)

    def test_confirmed_relationship_requires_evidence(self):
        relationship = read("snapshot-running.json")["relationships"][0]
        relationship["evidence"] = []
        with self.assertRaises(ValidationError):
            validate("Relationship", relationship)

    def test_group_scope_requires_an_id_and_main_scope_cannot_carry_one(self):
        for invalid in ({"kind": "group", "groupId": None}, {"kind": "main", "groupId": "other-group"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate("AgentScope", invalid)

    def test_selected_agent_scope_accepts_exact_ids_or_none_and_rejects_ambiguous_shapes(self):
        for scope in ({"kind": "selected", "groupIds": ["main", "nested"]}, {"kind": "selected", "groupIds": []},
                      {"kind": "all", "groupId": None}, {"kind": "main", "groupId": None}, {"kind": "group", "groupId": "child"}):
            validate("AgentScope", scope)
            validate("ViewState", {**read("view-state.json"), "agentScope": scope})
        for invalid in ({"kind": "selected"}, {"kind": "selected", "groupIds": ["child", "child"]},
                        {"kind": "selected", "groupIds": [""]}, {"kind": "selected", "groupIds": ["x" * 513]},
                        {"kind": "selected", "groupIds": None}, {"kind": "selected", "groupIds": [], "groupId": None},
                        {"kind": "main", "groupId": None, "groupIds": []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate("AgentScope", invalid)

    def test_view_state_rejects_body_payload(self):
        state = read("view-state.json")
        state["eventContent"] = "private task body"
        with self.assertRaises(ValidationError):
            validate("ViewState", state)

    def test_turn_range_is_optional_absolute_view_state_outside_event_filters(self):
        state = read("view-state.json")
        validate("ViewState", state)  # 已保存的旧偏好不要求补上新字段。
        for turn_range in (None, {"startMs": 0, "endMs": 1000}, {"startMs": 1790000000000, "endMs": 1790000001000}):
            with self.subTest(turn_range=turn_range):
                validate("ViewState", {**state, "turnRange": turn_range})
        for invalid in (True, [], {"startMs": 0}, {"startMs": -1, "endMs": 100},
                        {"startMs": 0.5, "endMs": 100}, {"startMs": 0, "endMs": 100, "turnIds": ["one"]}):
            with self.subTest(turn_range=invalid), self.assertRaises(ValidationError):
                validate("ViewState", {**state, "turnRange": invalid})
        with self.assertRaises(ValidationError):
            validate("Filters", {**state["filters"], "turnRange": {"startMs": 0, "endMs": 100}})
        request = read("requests.json")["QueryLoadedEventsRequest"]
        with self.assertRaises(ValidationError):
            validate("QueryLoadedEventsRequest", {**request, "turnRange": {"startMs": 0, "endMs": 100}})

    def test_timeline_spacing_is_optional_display_preference_outside_filters_and_queries(self):
        state = read("view-state.json")
        validate("ViewState", state)  # 旧偏好缺少显示模式时由 UI 默认采用 compact。
        for spacing in ("compact", "runtime"):
            with self.subTest(spacing=spacing):
                validate("ViewState", {**state, "timelineSpacing": spacing})
        for invalid in (None, True, "absolute", "", [], {}):
            with self.subTest(spacing=invalid), self.assertRaises(ValidationError):
                validate("ViewState", {**state, "timelineSpacing": invalid})
        with self.assertRaises(ValidationError):
            validate("Filters", {**state["filters"], "timelineSpacing": "compact"})
        request = read("requests.json")["QueryLoadedEventsRequest"]
        with self.assertRaises(ValidationError):
            validate("QueryLoadedEventsRequest", {**request, "timelineSpacing": "compact"})

    def test_raw_fixture_is_exact_binary_with_unknown_bad_and_partial_records(self):
        raw = (FIXTURES / "unknown-and-partial.bin").read_bytes()
        expected = read("scenarios.json")["unknownBytes"]
        self.assertEqual(len(raw), expected["byteLength"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected["sha256"])
        self.assertIn(b"\r\n", raw)
        self.assertIn(b"{malformed json}\n", raw)
        self.assertIn(b"\xff\xfe", raw)
        self.assertTrue(raw.endswith(b"\xe4\xb8"))
        source = read("export-manifest-partial.json")["sources"][0]
        self.assertEqual(source["copiedRange"], {"start": 0, "end": len(raw)})
        self.assertEqual(source["sha256"], expected["sha256"])

    def test_scenario_usage_and_relationship_inputs_are_valid(self):
        scenarios = read("scenarios.json")
        self.assertTrue(scenarios["syntheticOnly"])
        for scenario in scenarios["usageSequences"]:
            for usage in scenario["snapshots"]:
                validate("UsageSnapshot", usage)
        for kind in ("spawn", "communication"):
            validate("Relationship", scenarios["relationships"][kind])

    def test_synthetic_update_sequence_preserves_identity_and_advances_revision(self):
        # Fixture integrity only: the domain reducer is tested in its own suite.
        first = read("change-fragment.json")
        second = read("change-complete.json")
        self.assertEqual(first["upsertEvents"][0]["eventId"], second["upsertEvents"][0]["eventId"])
        self.assertEqual(first["revision"], second["baseRevision"])
        self.assertLess(first["revision"], second["revision"])
        self.assertEqual(first["status"]["value"], "unknown")
        self.assertEqual(second["status"]["value"], "unknown")


if __name__ == "__main__":
    unittest.main()
