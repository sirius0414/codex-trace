"""Synthetic M3 collectors, immutable change batches and independent I/O.

Every source is in memory. Blocking fixtures use explicit gates rather than
real Codex data, processes, network services or timing-dependent sleeps.
"""
from copy import deepcopy
import hashlib
import json
import threading
import unittest
from unittest import mock

from codex_display.adapters.raw import RawSnapshot
from codex_display.domain.projection import project
from codex_display.service import ServiceError, TrajectoryService, default_view
from contracts.test_contracts import validate

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


def item(index, *, output=None, status="completed", turn="turn-main"):
    return {"turnId": turn, "startedAtMs": index * 10,
            "completedAtMs": None if status == "inProgress" else index * 10 + 5,
            "item": {"id": f"item-{index}", "type": "commandExecution",
                     "command": "synthetic command", "aggregatedOutput": output or f"synthetic {index}",
                     "status": status, "exitCode": 0 if status == "completed" else None}}


def raw_item(index, *, identifier=A, output=None, generation=0):
    wrapper = item(index, output=output)
    record = {"type": "event_msg", "timestamp": "1970-01-01T00:00:00Z",
              "payload": {"type": "item_completed", "thread_id": identifier,
                          "turn_id": wrapper["turnId"], "started_at_ms": wrapper["startedAtMs"],
                          "completed_at_ms": wrapper["completedAtMs"], "item": wrapper["item"]}}
    encoded = json.dumps(record, sort_keys=True).encode() + b"\n"
    return {"record": record, "raw": encoded, "ordinal": index,
            "source": {"kind": "raw", "threadId": identifier, "segmentId": f"segment-{identifier}",
                       "generation": generation, "byteStart": index * 1000,
                       "byteEnd": index * 1000 + len(encoded), "rawType": "event_msg",
                       "producerVersion": "synthetic", "association": "physicalRecord"}}


def spawn(child=B, *, tool="spawn_agent", sender=A):
    return {"turnId": "turn-main", "startedAtMs": 10, "completedAtMs": 15,
            "item": {"id": "spawn-" + child, "type": "collabAgentToolCall", "tool": tool,
                     "senderThreadId": sender, "receiverThreadIds": [child], "status": "completed",
                     "agentsStates": {child: {"completed": "synthetic result"}}}}


class MemoryRaw:
    def __init__(self):
        self.snapshots = {}
        self.calls = []
        self.fail = set()
        self.gate = None

    def put(self, identifier=A, rows=None, *, generation=0, state="readable", reason=None):
        rows = [raw_item(0, identifier=identifier)] if rows is None else rows
        payload = b"".join(row["raw"] for row in rows)
        source = {"sourceId": "raw-" + identifier, "threadId": identifier, "generation": generation,
                  "format": "jsonl", "state": state, "readableRange": {"start": 0, "end": len(payload)},
                  "consumedThrough": len(payload), "inheritedRange": None, "reason": reason}
        fingerprint = hashlib.sha256(payload + json.dumps([generation, state, reason]).encode()).hexdigest()
        self.snapshots[identifier] = RawSnapshot(records=rows, sources=[source], segments=[],
                                               fingerprint=fingerprint, generation=generation,
                                               issues=[reason] if reason else [])

    def read(self, identifier, _metadata):
        self.calls.append(identifier)
        if self.gate:
            self.gate(identifier)
        if identifier in self.fail:
            raise OSError("synthetic raw unavailable")
        if identifier not in self.snapshots:
            self.put(identifier, [])
        return deepcopy(self.snapshots[identifier])

    def close(self):
        pass


class MemoryHistory:
    def __init__(self):
        self.rows = {A: [item(0)], B: [item(0)], C: [item(0)]}
        self.calls, self.identity_calls, self.item_calls = [], [], []
        self.fail = set()
        self.gate = None
        self.page_duplicates = []
        self.unfinished_rows = {}

    def read(self, identifier):
        self.identity_calls.append(identifier)
        return {"id": identifier, "name": "合成任务", "path": "/synthetic/" + identifier}

    def read_history(self, identifier, *, max_items=200, cursor=None):
        self.calls.append((identifier, cursor))
        # Freeze the source response before a test deliberately delays it.
        rows = deepcopy(self.rows.get(identifier, []))
        end = len(rows) if cursor is None else int(cursor.rsplit(":", 1)[1])
        start = max(0, end - max_items)
        result = {"items": list(reversed(rows[start:end])) + deepcopy(self.page_duplicates),
                  "turns": [{"id": row["turnId"], "status": "completed"} for row in rows[start:end]],
                  "nextCursor": f"history:{identifier}:{start}" if start else None}
        if self.gate:
            self.gate(identifier, cursor)
        if identifier in self.fail:
            raise OSError("synthetic API unavailable")
        return result

    def list_items(self, identifier, *, turn_id, cursor=None, limit=200):
        self.item_calls.append((identifier, turn_id, cursor))
        rows = self.unfinished_rows.get((identifier, turn_id), [])
        return {"data": list(reversed(deepcopy(rows))), "nextCursor": None}

    def close(self):
        pass


class MemoryStore:
    source_instance_id = "observation-synthetic"

    def save_projection(self, *_):
        pass

    def close(self):
        pass


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.history = MemoryRaw(), MemoryHistory()
        self.raw.put()
        self.service = TrajectoryService(self.history, self.raw, MemoryStore())
        self.addCleanup(self.service.close)
        self.binding = self.bind(A)
        self.initial = self.read()

    def bind(self, identifier):
        opened = self.service.open({"threadId": identifier}, "thread")
        return {"schemaVersion": "1.0.0", "sourceInstanceId": self.service.source_id,
                "viewSessionId": opened["viewSessionId"], "selectionEpoch": 1, "threadId": identifier}

    def read(self, binding=None, cursor=None):
        return self.service.read_trajectory({"binding": binding or self.binding, "cursor": cursor, "anchorEventId": None})

    def changes(self, *, after=None, generation=None, cursor=None, binding=None):
        return self.service.read_changes({"binding": binding or self.binding,
                                          "afterRevision": self.initial["revision"] if after is None else after,
                                          "generation": self.initial["generation"] if generation is None else generation,
                                          "continuationCursor": cursor})

    def eid(self, index, identifier=A):
        return project(identifier, [], [item(index)], mode="api")["events"][0]["eventId"]

    def test_creation_origin_arriving_without_new_events_advances_revision(self):
        original = self.history.read_history
        def with_metadata(identifier, **kwargs):
            return {**original(identifier, **kwargs), 'thread': {
                **self.history.read(identifier), 'createdAt': 1790000000}}
        self.history.read_history = with_metadata
        self.assertTrue(self.service.refresh_history(A, force=True))
        change = self.changes()
        validate('ChangeBatch',change)
        self.assertGreater(change['revision'],self.initial['revision'])
        self.assertEqual(change['timelineOrigin']['timestampMs'],1790000000000)
        self.assertEqual(change['upsertEvents'],[])
        self.assertEqual(self.read()['timelineOrigin'],change['timelineOrigin'])
        self.assertFalse(self.service.refresh_history(A, force=True))

    def start(self, target):
        errors = []
        done = threading.Event()
        def run():
            try:
                target()
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, done, errors

    def test_ui_change_reads_are_cache_only_and_do_not_advance_progress_without_changes(self):
        before = (len(self.raw.calls), len(self.history.calls), self.service.tasks[A].last_progress)
        for _ in range(3):
            batch = self.changes()
            validate("ChangeBatch", batch)
            self.assertEqual(batch["upsertEvents"], [])
            self.assertEqual(batch["revision"], self.initial["revision"])
        self.assertEqual(before, (len(self.raw.calls), len(self.history.calls), self.service.tasks[A].last_progress))

    def test_idle_and_single_page_changes_never_allocate_or_discard_history_cursors(self):
        self.raw.put(B, [])
        self.history.rows[B] = [item(index) for index in range(450)]
        other = self.bind(B)
        tail = self.read(other)
        historical = tail["coverage"]["nextCursor"]
        before = set(self.service.cursors)
        self.assertIn(historical, before)
        for _ in range(512):
            self.assertIsNone(self.changes()["continuationCursor"])
        self.assertEqual(set(self.service.cursors), before)
        self.raw.put(rows=[raw_item(0), raw_item(1)])
        self.service.refresh_raw(A, force=True)
        changed = self.changes()
        self.assertEqual(changed, self.changes(), "unacknowledged single-page payload stays retryable")
        self.assertEqual(set(self.service.cursors), before)
        self.changes(after=changed["revision"])
        self.assertIsNone(self.service.views[self.binding["viewSessionId"]].batch_context)
        self.assertEqual(len(self.read(other, historical)["events"]), 200)

    def test_ack_retires_only_the_old_batch_continuations_after_the_final_page(self):
        self.raw.put(B, [])
        self.history.rows[B] = [item(index) for index in range(450)]
        other = self.bind(B)
        historical = self.read(other)["coverage"]["nextCursor"]
        self.raw.put(rows=[raw_item(index) for index in range(451)])
        self.service.refresh_raw(A, force=True)
        first = self.changes()
        batch_tokens = {token for token, value in self.service.cursors.items() if value["kind"] == "changes"}
        self.assertEqual(len(batch_tokens), 2, "three pages require only two continuation handles")
        second = self.changes(cursor=first["continuationCursor"])
        final = self.changes(cursor=second["continuationCursor"])
        self.assertEqual(final, self.changes(cursor=second["continuationCursor"]))
        self.assertTrue(batch_tokens.issubset(self.service.cursors), "lost final replies remain retryable until ack")
        self.changes(after=first["revision"])
        self.assertTrue(batch_tokens.isdisjoint(self.service.cursors))
        self.assertIn(historical, self.service.cursors)
        self.assertEqual(self.changes(cursor=first["continuationCursor"])["resetReason"], "expiredCursor")
        self.assertEqual(len(self.read(other, historical)["events"]), 200)

    def test_partial_source_still_delivers_complete_new_rows(self):
        self.raw.put(rows=[raw_item(0), raw_item(1)], state="partial", reason="incomplete-tail")
        self.assertTrue(self.service.refresh_raw(A, force=True))
        batch = self.changes()
        self.assertIn(self.eid(1), {row["eventId"] for row in batch["upsertEvents"]})
        self.assertEqual(batch["coverage"]["sourceCompleteness"], "partial")
        self.assertEqual(batch["coverage"]["sources"][0]["reason"], "incomplete-tail")

    def test_unreadable_source_keeps_existing_events_and_separate_api_health(self):
        before = deepcopy(self.service.tasks[A].projection["events"])
        self.raw.put(rows=[], state="unreadable", reason="source-unreadable")
        self.assertFalse(self.service.refresh_raw(A, force=True))
        self.assertEqual(self.service.tasks[A].projection["events"], before)
        self.history.rows[A].append(item(1))
        self.assertTrue(self.service.refresh_history(A, force=True))
        batch = self.changes()
        self.assertEqual(batch["status"]["value"], "unknown")
        self.assertEqual(batch["freshness"]["state"], "stale")
        health = {row["sourceId"]: row["state"] for row in batch["freshness"]["sources"]}
        self.assertEqual(health["raw-" + A], "unavailable")
        self.assertEqual(health["history-api"], "current")
        self.assertIn(self.eid(0), self.service.tasks[A].event_hashes)

    def test_recovery_of_identical_source_clears_error_without_inventing_progress(self):
        old_progress = self.service.tasks[A].last_progress
        self.raw.fail.add(A)
        self.service.refresh_raw(A, force=True)
        failed_revision = self.service.tasks[A].revision
        self.raw.fail.remove(A)
        self.assertFalse(self.service.refresh_raw(A, force=True))
        self.assertIsNone(self.service.tasks[A].error)
        self.assertGreater(self.service.tasks[A].revision, failed_revision)
        self.assertEqual(self.service.tasks[A].last_progress, old_progress)

    def test_each_collector_retries_at_1_2_5_10_seconds_and_resets_after_success(self):
        task = self.service.tasks[A]
        self.raw.fail.add(A)
        self.history.fail.add(A)
        with mock.patch("codex_display.observation.time.monotonic", return_value=100):
            for delay in (1, 2, 5, 10, 10):
                self.service.refresh_raw(A, force=True)
                self.service.refresh_history(A, force=True)
                self.assertEqual(task.raw_retry_at, 100 + delay)
                self.assertEqual(task.api_due_at, 100 + delay)
            raw_calls, api_calls = len(self.raw.calls), len(self.history.calls)
            self.service.refresh_raw(A)
            self.service.refresh_history(A)
            self.assertEqual((len(self.raw.calls), len(self.history.calls)), (raw_calls, api_calls))
            self.raw.fail.clear()
            self.history.fail.clear()
            self.service.refresh_raw(A, force=True)
            self.service.refresh_history(A, force=True)
        self.assertEqual((task.raw_failures, task.api_failures), (0, 0))
        self.assertIsNone(task.error)
        self.assertIsNone(task.api_error)

    def test_growth_coalescing_has_a_fixed_deadline_and_idle_checks_remain_low_frequency(self):
        task = self.service.tasks[A]
        with mock.patch("codex_display.observation.time.monotonic", return_value=100):
            self.service.refresh_history(A, force=True)
            self.assertEqual(task.api_due_at, 105)
            self.raw.put(rows=[raw_item(0), raw_item(1)])
            self.service.refresh_raw(A, force=True)
            self.assertEqual(task.api_due_at, 100.75)
        with mock.patch("codex_display.observation.time.monotonic", return_value=100.5):
            self.raw.put(rows=[raw_item(0), raw_item(1), raw_item(2)])
            self.service.refresh_raw(A, force=True)
            self.assertEqual(task.api_due_at, 100.75)

    def test_api_without_readable_raw_uses_750ms_fallback(self):
        self.raw.put(B, [], state="unreadable", reason="source-missing")
        binding = self.bind(B)
        self.read(binding)
        with mock.patch("codex_display.observation.time.monotonic", return_value=100):
            self.service.refresh_history(B, force=True)
            self.assertEqual(self.service.tasks[B].api_due_at, 100.75)
        self.assertEqual(self.service._freshness(self.service.tasks[B])["outputGranularity"], "completedResults")

    def test_burst_change_pages_freeze_revision_payload_and_metadata_until_acknowledged(self):
        self.raw.put(rows=[raw_item(index) for index in range(451)])
        self.service.refresh_raw(A, force=True)
        first = self.changes()
        validate("ChangeBatch", first)
        self.assertEqual(len(first["upsertEvents"]), 200)
        revision = first["revision"]
        self.raw.put(rows=[raw_item(index) for index in range(452)])
        self.service.refresh_raw(A, force=True)
        self.assertEqual(self.changes(), first)
        merged, page = [], first
        while True:
            validate("ChangeBatch", page)
            self.assertEqual(page["revision"], revision)
            self.assertEqual(page["coverage"], first["coverage"])
            merged.extend(row["eventId"] for row in page["upsertEvents"])
            token = page["continuationCursor"]
            if token is None:
                break
            page = self.changes(cursor=token)
            self.assertEqual(page, self.changes(cursor=token))
        self.assertEqual(len(merged), 450)
        self.assertEqual(len(set(merged)), 450)
        pending = self.changes(after=revision)
        self.assertEqual([row["eventId"] for row in pending["upsertEvents"]], [self.eid(451)])
        self.assertGreater(pending["revision"], revision)

    def test_half_delivered_batch_is_not_searchable_until_last_page_and_commits_once(self):
        self.raw.put(rows=[raw_item(index) for index in range(451)])
        self.service.refresh_raw(A, force=True)
        first = self.changes()
        view = self.service.views[self.binding["viewSessionId"]]
        self.assertEqual(view.loaded, {self.eid(0)})
        self.assertEqual(view.scope_revision, self.initial["coverage"]["scopeRevision"])
        request = {"binding": self.binding, "loadedSetId": self.initial["coverage"]["loadedSetId"],
                   "scopeRevision": self.initial["coverage"]["scopeRevision"],
                   "filters": default_view()["filters"], "agentScope": {"kind": "all", "groupId": None}}
        self.assertEqual(self.service.query_loaded_events(request)["eventIds"], [self.eid(0)])
        second = self.changes(cursor=first["continuationCursor"])
        self.assertEqual(view.loaded, {self.eid(0)})
        final = self.changes(cursor=second["continuationCursor"])
        self.assertIsNone(final["continuationCursor"])
        self.assertEqual(len(view.loaded), 451)
        self.assertEqual(view.scope_revision, first["coverage"]["scopeRevision"])
        scope = view.scope_revision
        self.assertEqual(final, self.changes(cursor=second["continuationCursor"]))
        self.assertEqual(view.scope_revision, scope)
        with self.assertRaises(ServiceError):
            self.service.query_loaded_events(request)

    def test_history_scope_change_invalidates_an_unfinished_change_batch(self):
        self.raw.put(rows=[raw_item(index) for index in range(451)])
        self.service.refresh_raw(A, force=True)
        first = self.changes()
        newer_scope = self.read()
        self.assertEqual(newer_scope["coverage"]["loadedEventCount"], 201)
        continued = self.changes(cursor=first["continuationCursor"])
        self.assertTrue(continued["resetRequired"])
        self.assertEqual(continued["resetReason"], "expiredCursor")
        self.assertEqual(len(self.service.views[self.binding["viewSessionId"]].loaded), 201)

    def test_source_generation_change_invalidates_pending_continuation_and_loaded_ids(self):
        self.raw.put(rows=[raw_item(index) for index in range(251)])
        self.service.refresh_raw(A, force=True)
        pending = self.changes()
        self.assertIsNotNone(pending["continuationCursor"])
        self.raw.put(rows=[raw_item(900, generation=1)], generation=1)
        self.service.refresh_raw(A, force=True)
        reset = self.changes(cursor=pending["continuationCursor"])
        self.assertTrue(reset["resetRequired"])
        self.assertEqual(reset["resetReason"], "generationChanged")
        self.assertNotIn(pending["continuationCursor"], self.service.cursors)
        rebuilt = self.read()
        self.assertEqual({row["eventId"] for row in rebuilt["events"]}, {self.eid(900)})
        self.assertEqual(rebuilt["coverage"]["loadedEventCount"], 1)
        self.assertNotEqual(rebuilt["coverage"]["loadedSetId"], self.initial["coverage"]["loadedSetId"])

    def test_old_changes_expire_explicitly_and_foreign_observer_cursor_cannot_be_used(self):
        for _ in range(130):
            self.service._touch(self.service.tasks[A])
        self.assertEqual(self.changes()["resetReason"], "expiredCursor")
        current = self.read()
        self.initial = current
        self.raw.put(rows=[raw_item(index) for index in range(251)])
        self.service.refresh_raw(A, force=True)
        page = self.changes()
        other = self.bind(A)
        self.read(other)
        result = self.changes(cursor=page["continuationCursor"], binding=other)
        self.assertEqual(result["resetReason"], "expiredCursor")

    def test_expired_changes_drop_deleted_loaded_ids_before_fresh_rebuild(self):
        self.raw.put(rows=[raw_item(index) for index in range(251)])
        self.service.refresh_raw(A, force=True)
        self.initial = self.read()
        earlier = self.read(cursor=self.initial["coverage"]["nextCursor"])
        self.assertEqual(earlier["coverage"]["loadedEventCount"], 251)
        view = self.service.views[self.binding["viewSessionId"]]
        scope, loaded_set = view.scope_revision, view.loaded_set_id
        self.raw.put(rows=[raw_item(index) for index in range(250)])
        self.service.refresh_raw(A, force=True)
        self.assertEqual(self.service.tasks[A].generation, self.initial["generation"])
        for _ in range(129):
            self.service._touch(self.service.tasks[A])
        reset = self.changes()
        validate("ChangeBatch", reset)
        self.assertEqual(reset["resetReason"], "expiredCursor")
        self.assertEqual(reset["removeEventIds"], [])
        self.assertNotIn(self.eid(250), view.loaded)
        self.assertEqual(reset["coverage"]["loadedEventCount"], 250)
        self.assertEqual(reset["coverage"]["scopeRevision"], scope + 1)
        self.assertEqual(reset["coverage"]["loadedSetId"], loaded_set)
        self.assertEqual(self.changes()["coverage"]["scopeRevision"], scope + 1)
        rebuilt = self.read()
        events = {event["eventId"] for event in rebuilt["events"]}
        while rebuilt["coverage"]["nextCursor"]:
            rebuilt = self.read(cursor=rebuilt["coverage"]["nextCursor"])
            events.update(event["eventId"] for event in rebuilt["events"])
        self.assertEqual(len(events), 250)
        self.assertEqual(rebuilt["coverage"]["loadedEventCount"], len(events))

    def test_regular_snapshot_preserves_pending_incremental_removal(self):
        self.raw.put(rows=[raw_item(index) for index in range(3)])
        self.service.refresh_raw(A, force=True)
        self.initial = self.read()
        self.raw.put(rows=[raw_item(index) for index in range(2)])
        self.service.refresh_raw(A, force=True)
        current = self.read()
        self.assertEqual(len(current["events"]), 2)
        self.assertEqual(current["coverage"]["loadedEventCount"], 3)
        batch = self.changes()
        validate("ChangeBatch", batch)
        self.assertFalse(batch["resetRequired"])
        self.assertEqual(batch["removeEventIds"], [self.eid(2)])
        self.assertEqual(batch["coverage"]["loadedEventCount"], 2)
        self.assertEqual(len(self.service.views[self.binding["viewSessionId"]].loaded), 2)

    def test_same_operation_updates_original_id_and_revision_without_duplicate(self):
        before = self.initial["events"][0]
        self.raw.put(rows=[raw_item(0, output="synthetic newer result")])
        self.service.refresh_raw(A, force=True)
        batch = self.changes()
        self.assertEqual(len(batch["upsertEvents"]), 1)
        self.assertEqual(batch["upsertEvents"][0]["eventId"], before["eventId"])
        self.assertGreater(batch["upsertEvents"][0]["eventRevision"], before["eventRevision"])
        self.assertEqual(len(self.service.tasks[A].projection["events"]), 1)

    def test_unknown_only_raw_growth_advances_coverage_without_ui_event_or_progress(self):
        old_progress = self.service.tasks[A].last_progress
        unknown = raw_item(1)
        unknown["record"]["type"] = "futureRecord"
        unknown["raw"] = json.dumps(unknown["record"]).encode() + b"\n"
        self.raw.put(rows=[raw_item(0), unknown])
        self.service.refresh_raw(A, force=True)
        batch = self.changes()
        self.assertEqual(batch["upsertEvents"], [])
        self.assertEqual(self.service.tasks[A].last_progress, old_progress)
        self.assertGreater(batch["coverage"]["sources"][0]["consumedThrough"], self.initial["coverage"]["sources"][0]["consumedThrough"])

    def test_api_burst_follows_all_opaque_pages_to_known_boundary(self):
        self.history.rows[A] = [item(index) for index in range(651)]
        self.assertTrue(self.service.refresh_history(A, force=True))
        self.assertEqual(len(self.service.tasks[A].api_items), 651)
        self.assertEqual([cursor for identifier, cursor in self.history.calls[-4:]],
                         [None, f"history:{A}:451", f"history:{A}:251", f"history:{A}:51"])
        self.assertEqual(len(self.service.tasks[A].projection["events"]), 651)

    def test_overlapping_api_pages_deduplicate_new_ids_and_keep_latest_copy(self):
        self.history.rows[A] = [item(index) for index in range(251)]
        self.history.page_duplicates = [item(200, output="duplicate synthetic")]
        self.assertTrue(self.service.refresh_history(A, force=True))
        self.assertIsNone(self.service.tasks[A].api_error)
        self.assertEqual(len(self.service.tasks[A].api_items), 251)
        self.assertEqual(len(self.service.tasks[A].projection["events"]), 251)

    def test_unfinished_operation_is_verified_even_when_absent_from_tail(self):
        self.raw.put(B, [])
        self.history.rows[B] = [item(0, status="inProgress", turn="old-turn")]
        binding = self.bind(B)
        self.read(binding)
        self.history.rows[B] = [item(index) for index in range(1, 402)]
        self.history.unfinished_rows[(B, "old-turn")] = [item(0, output="finished", turn="old-turn")]
        self.assertTrue(self.service.refresh_history(B, force=True))
        self.assertIn((B, "old-turn", None), self.history.item_calls)
        existing = next(row for row in self.service.tasks[B].api_items if row["item"]["id"] == "item-0")
        self.assertEqual(existing["item"]["status"], "completed")

    def test_delayed_api_cannot_erase_a_newer_raw_completion(self):
        entered, release = threading.Event(), threading.Event()
        def gate(_identifier, _cursor):
            entered.set()
            release.wait(2)
        self.history.rows[A] = [item(0, output="older API", status="inProgress")]
        self.history.gate = gate
        worker, done, errors = self.start(lambda: self.service.refresh_history(A, force=True))
        try:
            self.assertTrue(entered.wait(.5))
            self.raw.put(rows=[raw_item(0, output="new raw completion")])
            self.assertTrue(self.service.refresh_raw(A, force=True))
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        task = self.service.tasks[A]
        self.assertEqual(task.projection["details"][self.eid(0)]["output"], "new raw completion")
        self.assertEqual(task.projection["events"][0]["status"]["value"], "completed")

    def test_old_api_result_is_discarded_after_raw_generation_reset(self):
        entered, release = threading.Event(), threading.Event()
        def gate(_identifier, _cursor):
            entered.set()
            release.wait(2)
        self.history.rows[A] = [item(0), item(1)]
        self.history.gate = gate
        worker, done, errors = self.start(lambda: self.service.refresh_history(A, force=True))
        try:
            self.assertTrue(entered.wait(.5))
            self.raw.put(rows=[raw_item(900, generation=1)], generation=1)
            self.service.refresh_raw(A, force=True)
            generation = self.service.tasks[A].generation
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        task = self.service.tasks[A]
        self.assertEqual(task.generation, generation)
        self.assertEqual(task.api_items, [])
        self.assertEqual(set(task.event_hashes), {self.eid(900)})

    def test_late_raw_and_api_success_or_failure_cannot_change_an_evicted_task_or_its_replacement(self):
        for source in ("raw", "api"):
            for fails in (False, True):
                with self.subTest(source=source, fails=fails):
                    entered, release = threading.Event(), threading.Event()
                    adapter = self.raw if source == "raw" else self.history
                    def gate(*_):
                        entered.set()
                        release.wait(2)
                    old = self.service.tasks[A]
                    old_state = (old.revision, old.raw.fingerprint, deepcopy(old.api_items))
                    adapter.gate = gate
                    result = []
                    refresh = self.service.refresh_raw if source == "raw" else self.service.refresh_history
                    worker, done, errors = self.start(lambda: result.append(refresh(A, force=True)))
                    try:
                        self.assertTrue(entered.wait(.5))
                        adapter.gate = None
                        with self.service.lock:
                            del self.service.tasks[A]
                        self.raw.put(rows=[raw_item(900)])
                        self.history.rows[A] = [item(900)]
                        current = self.service._load(A)
                        current_state = (current.revision, current.raw.fingerprint, deepcopy(current.api_items))
                        if fails:
                            adapter.fail.add(A)
                    finally:
                        release.set()
                        worker.join(2)
                        adapter.fail.discard(A)
                        adapter.gate = None
                    self.assertTrue(done.is_set())
                    self.assertEqual(errors, [])
                    self.assertEqual(result, [False])
                    self.assertIs(self.service.tasks[A], current)
                    self.assertEqual((current.revision, current.raw.fingerprint, current.api_items), current_state)
                    self.assertEqual((old.revision, old.raw.fingerprint, old.api_items), old_state)
                    self.assertIsNone(current.error)
                    self.assertIsNone(current.api_error)

    def test_api_failure_retains_mode_generation_and_catches_up_without_duplicates(self):
        task = self.service.tasks[A]
        generation, mode = task.generation, task.mode
        self.history.fail.add(A)
        self.service.refresh_history(A, force=True)
        self.raw.put(rows=[raw_item(0), raw_item(1)])
        self.service.refresh_raw(A, force=True)
        self.assertEqual((task.generation, task.mode), (generation, mode))
        self.history.fail.clear()
        self.history.rows[A].append(item(1))
        self.service.refresh_history(A, force=True)
        self.assertEqual((task.generation, task.mode), (generation, mode))
        self.assertEqual(set(task.event_hashes), {self.eid(0), self.eid(1)})
        self.assertIsNone(task.api_error)

    def assert_independent(self, slow_target, fast_target, install_gate):
        entered, release = threading.Event(), threading.Event()
        def gate(*_):
            entered.set()
            release.wait(2)
        install_gate(gate)
        slow, slow_done, slow_errors = self.start(slow_target)
        fast = None
        try:
            self.assertTrue(entered.wait(.5), "slow collector did not reach its I/O gate")
            fast, fast_done, fast_errors = self.start(fast_target)
            independent = fast_done.wait(.4)
        finally:
            release.set()
            slow.join(2)
            if fast:
                fast.join(2)
        self.assertTrue(independent, "one slow source held the shared projection lock")
        self.assertTrue(slow_done.is_set())
        self.assertEqual(slow_errors + fast_errors, [])

    def test_slow_api_refresh_does_not_block_raw_commit_or_cached_ui_reads(self):
        self.raw.put(rows=[raw_item(0), raw_item(1)])
        def fast():
            self.service.refresh_raw(A, force=True)
            self.assertIn(self.eid(1), {row["eventId"] for row in self.changes()["upsertEvents"]})
        self.assert_independent(lambda: self.service.refresh_history(A, force=True), fast,
                                lambda gate: setattr(self.history, "gate", gate))

    def test_slow_raw_refresh_does_not_block_api_commit(self):
        self.history.rows[A].append(item(1))
        self.assert_independent(lambda: self.service.refresh_raw(A, force=True),
                                lambda: self.service.refresh_history(A, force=True),
                                lambda gate: setattr(self.raw, "gate", gate))

    def test_loading_another_task_does_not_block_existing_task_raw_updates(self):
        binding = self.bind(B)
        self.raw.put(rows=[raw_item(0), raw_item(1)])
        self.assert_independent(lambda: self.read(binding), lambda: self.service.refresh_raw(A, force=True),
                                lambda gate: setattr(self.history, "gate", gate))

    def test_loading_old_history_does_not_block_raw_updates(self):
        self.history.rows[B] = [item(index) for index in range(450)]
        self.raw.put(B, [])
        binding = self.bind(B)
        tail = self.read(binding)
        self.raw.put(rows=[raw_item(0), raw_item(1)])
        self.assert_independent(lambda: self.read(binding, tail["coverage"]["nextCursor"]),
                                lambda: self.service.refresh_raw(A, force=True),
                                lambda gate: setattr(self.history, "gate", gate))

    def test_only_confirmed_spawn_can_authorize_background_child_discovery(self):
        task = self.service.tasks[A]
        source = task.projection["events"][0]["sources"][0]
        group = deepcopy(task.projection["groups"][0])
        group.update(groupId="main:" + B, kind="agent", threadId=B, parentGroupId="main:" + A,
                     relationshipId="synthetic-edge", creationEventId=self.eid(0))
        edge = {"relationshipId": "synthetic-edge", "type": "spawned-by", "fromThreadId": B,
                "toThreadId": A, "creationEventId": self.eid(0), "turnId": "turn-main",
                "evidence": [source], "resolution": "ambiguous"}
        for resolution, kind, expected in (("ambiguous", "spawned-by", False),
                                           ("confirmed", "communicates-with", False),
                                           ("confirmed", "spawned-by", True)):
            edge.update(resolution=resolution, type=kind)
            task.projection["groups"] = [task.projection["groups"][0], deepcopy(group)]
            task.projection["relationships"] = [deepcopy(edge)]
            task.own_projection["relationships"] = [deepcopy(edge)]
            self.assertEqual(B in self.service._active_ids(), expected, (resolution, kind))

    def test_confirmed_child_composes_its_own_events_without_adding_usage_to_parent(self):
        self.history.rows[A].append(spawn())
        self.service.refresh_history(A, force=True)
        self.assertIn(B, self.service._active_ids())
        parent_usage = deepcopy(self.service.tasks[A].projection["mainUsage"])
        self.raw.put(B, [raw_item(2, identifier=B)])
        self.service._load(B)
        with self.service.lock:
            self.service._refresh_ancestors(B)
        parent = self.service.tasks[A].projection
        self.assertEqual(parent["mainUsage"], parent_usage)
        self.assertTrue(any(row["threadId"] == B for row in parent["events"]))
        self.assertEqual(len([group for group in parent["groups"] if group["threadId"] == B]), 1)
        self.raw.fail.add(B)
        self.service.refresh_raw(B, force=True)
        parent = self.service.tasks[A].projection
        self.assertTrue(next(group for group in parent["groups"] if group["threadId"] == B)["readIssue"])
        self.assertTrue(any(row["threadId"] == B for row in parent["events"]))
        self.assertEqual(parent["status"]["value"], "unknown")

    def test_child_recovery_without_new_content_clears_parent_group_read_issue(self):
        self.history.rows[A].append(spawn())
        self.service.refresh_history(A, force=True)
        self.raw.put(B)
        self.service._load(B)
        self.service._refresh_ancestors(B)
        self.raw.fail.add(B)
        self.service.refresh_raw(B, force=True)
        self.assertTrue(next(g for g in self.service.tasks[A].projection["groups"] if g["threadId"] == B)["readIssue"])
        self.raw.fail.remove(B)
        self.service.refresh_raw(B, force=True)
        self.assertIsNone(next(g for g in self.service.tasks[A].projection["groups"] if g["threadId"] == B)["readIssue"])

    def test_child_operations_remain_unloaded_until_group_requested_and_page_independently(self):
        self.history.rows[A] = [item(index) for index in range(450)] + [spawn()]
        self.service.refresh_history(A, force=True)
        parent = self.read()
        self.assertIsNotNone(parent["coverage"]["nextCursor"])
        self.raw.put(B, [])
        self.history.rows[B] = [item(index) for index in range(450)]
        self.service._load(B)
        self.service._refresh_ancestors(B)
        changes = self.changes(after=parent["revision"])
        unloaded = next(g for g in changes["groups"] if g["threadId"] == B)
        self.assertEqual(unloaded["loadedEventCount"], 0)
        self.assertEqual(unloaded["loadedToolCallCount"], 0)
        self.assertEqual(unloaded["status"]["value"], "completed")
        self.assertIsNone(unloaded["readIssue"])
        self.assertTrue(self.service.tasks[B].projection["events"])
        self.assertFalse(any(e["threadId"] == B for e in changes["upsertEvents"]))
        request = {"binding": self.binding, "cursor": None, "anchorEventId": None, "groupId": "main:" + B}
        child = self.service.read_trajectory(request)
        validate("ThreadSnapshot", child)
        self.assertEqual(len(child["events"]), 200)
        self.assertEqual({e["threadId"] for e in child["events"]}, {B})
        group = next(g for g in child["groups"] if g["threadId"] == B)
        self.assertEqual(group["loadedEventCount"], 200)
        self.assertEqual(group["loadedToolCallCount"], 200)
        self.assertTrue(group["hasEarlier"])
        prior = self.service.read_trajectory({**request, "cursor": group["nextCursor"]})
        self.assertFalse({e["eventId"] for e in prior["events"]} & {e["eventId"] for e in child["events"]})
        prior_group = next(g for g in prior["groups"] if g["threadId"] == B)
        self.assertEqual(prior_group["loadedEventCount"], 400)
        self.assertEqual(prior_group["loadedToolCallCount"], 400)
        self.assertEqual(prior["coverage"]["nextCursor"], parent["coverage"]["nextCursor"])
        self.assertEqual(self.service.views[self.binding["viewSessionId"]].page_cursor, parent["coverage"]["nextCursor"])
        earlier_parent = self.read(cursor=parent["coverage"]["nextCursor"])
        self.assertEqual({e["threadId"] for e in earlier_parent["events"]}, {A})
        self.assertFalse({e["eventId"] for e in earlier_parent["events"]} & {e["eventId"] for e in parent["events"]})

    def test_explicit_child_read_does_not_depend_on_saving_expansion_preferences(self):
        self.history.rows[A].append(spawn())
        self.service.refresh_history(A, force=True)
        self.history.rows[B] = []
        self.raw.put(B, [raw_item(1, identifier=B), raw_item(2, identifier=B)])
        self.service._load(B)
        self.service._refresh_ancestors(B)
        group_id = "main:" + B
        state = {**default_view(), "follow": "paused", "expandedGroupIds": [group_id],
                 "agentScope": {"kind": "selected", "groupIds": [group_id]}}
        with mock.patch.object(self.service.store, "save_view", create=True,
                               side_effect=OSError("synthetic preference unavailable")) as save:
            with self.assertRaises(OSError):
                self.service.save_view_state({"binding": self.binding,
                                              "expectedPreferenceRevision": 0, "state": state})
            self.assertNotIn(group_id, self.service.views[self.binding["viewSessionId"]].requested_groups)
            result = self.service.read_trajectory({"binding": self.binding, "cursor": None,
                                                   "anchorEventId": None, "groupId": group_id})
            save.assert_called_once()
        validate("ThreadSnapshot", result)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual({e["threadId"] for e in result["events"]}, {B})
        group = next(g for g in result["groups"] if g["groupId"] == group_id)
        self.assertEqual(group["loadedEventCount"], 2)
        self.assertEqual(group["loadedToolCallCount"], 2)
        self.assertIsNone(group["readIssue"])
        self.assertIn(group_id, self.service.views[self.binding["viewSessionId"]].requested_groups)

    def test_partial_lineage_loss_preserves_previous_rows_while_other_source_advances(self):
        inherited = raw_item(10)
        inherited["source"].update(segmentId="inherited-segment", threadId=B)
        inherited["inherited"] = True
        self.raw.put(rows=[inherited, raw_item(0)])
        snapshot = self.raw.snapshots[A]
        snapshot.sources[0]["sourceId"] = "segment-" + A
        parent_source = {**snapshot.sources[0], "sourceId": "inherited-segment", "threadId": B,
                         "inheritedRange": {"start": 0, "endExclusive": 20}}
        snapshot.sources.insert(0, parent_source)
        own_segment = {"segmentId": "segment-" + A, "threadId": A, "generation": 0,
                       "device": 1, "inode": 1, "headerSha256": "synthetic-fixed-own-header",
                       "inheritedRange": None}
        parent_segment = {"segmentId": "inherited-segment", "threadId": B, "generation": 0,
                          "device": 1, "inode": 2, "headerSha256": "synthetic-parent-header",
                          "inheritedRange": {"start": 0, "endExclusive": 20}}
        snapshot.segments = [parent_segment, own_segment]
        self.service.refresh_raw(A, force=True)
        self.read()
        generation = self.service.tasks[A].generation
        self.raw.put(rows=[raw_item(0), raw_item(1)], generation=1)
        snapshot = self.raw.snapshots[A]
        snapshot.sources[0]["sourceId"] = "segment-" + A
        snapshot.sources.insert(0, {**parent_source, "state": "unreadable", "reason": "source-unreadable"})
        snapshot.segments = [own_segment]
        snapshot.issues.append("source-unreadable")
        self.service.refresh_raw(A, force=True)
        task = self.service.tasks[A]
        self.assertIn(self.eid(10), task.event_hashes, "a temporary single-source failure discarded previously readable history")
        self.assertIn(self.eid(1), task.event_hashes, "the still-readable source stopped advancing")
        self.assertEqual(task.generation, generation, "temporary missing coverage was presented as a proven source replacement")
        self.assertEqual(self.service._freshness(task)["state"], "stale")
        source_health = {source["sourceId"]: source["state"] for source in self.service._freshness(task)["sources"]}
        self.assertEqual(source_health["segment-" + A], "current")
        self.assertEqual(source_health["inherited-segment"], "unavailable")
        progress = task.last_progress
        self.service.refresh_raw(A, force=True)
        self.assertTrue(task.error, "the same unavailable range was falsely reported as recovered")
        self.assertIsNotNone(task.raw_coverage)
        self.assertEqual(task.raw_failures, 2)
        self.assertEqual(task.last_progress, progress)
        self.assertIn(self.eid(10), task.event_hashes)

    def test_changed_own_header_does_not_retain_previous_inherited_rows(self):
        inherited = raw_item(10)
        inherited["source"].update(segmentId="inherited-segment", threadId=B)
        inherited["inherited"] = True
        self.raw.put(rows=[inherited, raw_item(0)])
        snapshot = self.raw.snapshots[A]
        snapshot.sources[0]["sourceId"] = "segment-" + A
        parent_source = {**snapshot.sources[0], "sourceId": "inherited-segment", "threadId": B,
                         "inheritedRange": {"start": 0, "endExclusive": 20}}
        snapshot.sources.insert(0, parent_source)
        own = {"segmentId": "segment-" + A, "threadId": A, "generation": 0,
               "device": 1, "inode": 1, "headerSha256": "previous-header", "inheritedRange": None}
        parent = {"segmentId": "inherited-segment", "threadId": B, "generation": 0,
                  "device": 1, "inode": 2, "headerSha256": "parent-header",
                  "inheritedRange": {"start": 0, "endExclusive": 20}}
        snapshot.segments = [parent, own]
        self.service.refresh_raw(A, force=True)
        generation = self.service.tasks[A].generation
        self.raw.put(rows=[raw_item(900, generation=1)], generation=1)
        snapshot = self.raw.snapshots[A]
        snapshot.sources[0]["sourceId"] = "segment-" + A
        snapshot.sources.insert(0, {**parent_source, "state": "unreadable", "reason": "source-unreadable"})
        snapshot.segments = [{**own, "generation": 1, "headerSha256": "new-header"}]
        snapshot.issues = ["source-unreadable"]
        self.service.refresh_raw(A, force=True)
        self.assertNotIn(self.eid(10), self.service.tasks[A].event_hashes)
        self.assertEqual(set(self.service.tasks[A].event_hashes), {self.eid(900)})
        self.assertGreater(self.service.tasks[A].generation, generation)


class LazyHistoryReconnectTests(unittest.TestCase):
    def setUp(self):
        from codex_display.app import LazyHistory
        self.wrapper = LazyHistory("/synthetic/codex")
        self.addCleanup(self.wrapper.close)

    def test_failed_observer_connection_is_closed_and_next_read_constructs_a_fresh_adapter(self):
        from codex_display.adapters.history import HistoryError
        first, second = mock.Mock(), mock.Mock()
        first.read.side_effect = HistoryError("app-server-disconnected")
        second.read.return_value = {"id": A}
        with mock.patch("codex_display.app.HistoryAdapter", side_effect=[first, second]) as factory:
            self.assertIsNone(self.wrapper.adapter)
            factory.assert_not_called()
            with self.assertRaises(HistoryError):
                self.wrapper.read(A)
            first.close.assert_called_once_with()
            self.assertIsNone(self.wrapper.adapter)
            self.assertEqual(self.wrapper.read(A), {"id": A})
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(first.method_calls, [mock.call.read(A), mock.call.close()])
            self.assertEqual(second.method_calls, [mock.call.read(A)])
            self.wrapper.close()
            self.wrapper.close()
            second.close.assert_called_once_with()

    def test_only_explicit_read_methods_are_forwarded_and_task_actions_are_unavailable(self):
        adapter = mock.Mock()
        with mock.patch("codex_display.app.HistoryAdapter", return_value=adapter) as factory:
            for method in ("resume", "thread_resume", "turn_start", "send_message", "send_input", "stop", "interrupt", "write"):
                with self.subTest(method=method), self.assertRaises(AttributeError):
                    getattr(self.wrapper, method)
            factory.assert_not_called()
            for method in ("read", "read_history", "list_threads", "list_turns", "list_items"):
                getattr(self.wrapper, method)(A, cursor="synthetic-cursor")
            self.assertEqual([entry[0] for entry in adapter.method_calls],
                             ["read", "read_history", "list_threads", "list_turns", "list_items"])
            factory.assert_called_once_with("/synthetic/codex")

    def test_startup_failure_is_retryable_without_reusing_a_half_initialized_adapter(self):
        from codex_display.adapters.history import HistoryError
        adapter = mock.Mock()
        adapter.read.return_value = {"id": A}
        with mock.patch("codex_display.app.HistoryAdapter", side_effect=[HistoryError("app-server-start-failed"), adapter]) as factory:
            with self.assertRaises(HistoryError):
                self.wrapper.read(A)
            self.assertIsNone(self.wrapper.adapter)
            self.assertEqual(self.wrapper.read(A), {"id": A})
            self.assertEqual(factory.call_count, 2)

    def test_concurrent_retry_waits_for_failed_connection_cleanup_and_uses_replacement(self):
        from codex_display.adapters.history import HistoryError
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        first, second = mock.Mock(), mock.Mock()
        order, errors, results = [], [], []
        def fail(_):
            entered.set()
            release.wait(2)
            raise HistoryError("app-server-disconnected")
        first.read.side_effect = fail
        first.close.side_effect = lambda: order.append("closed-old")
        second.read.side_effect = lambda _: order.append("read-new") or {"id": B}
        def attempt(identifier):
            try:
                results.append(self.wrapper.read(identifier))
            except HistoryError as error:
                errors.append(error)
            finally:
                if identifier == B:
                    second_done.set()
        with mock.patch("codex_display.app.HistoryAdapter", side_effect=[first, second]) as factory:
            old_worker = threading.Thread(target=attempt, args=(A,), daemon=True)
            new_worker = threading.Thread(target=attempt, args=(B,), daemon=True)
            old_worker.start()
            try:
                self.assertTrue(entered.wait(.5))
                new_worker.start()
                self.assertFalse(second_done.wait(.05))
            finally:
                release.set()
                old_worker.join(2)
                if new_worker.ident:
                    new_worker.join(2)
            self.assertTrue(second_done.is_set())
            self.assertEqual(len(errors), 1)
            self.assertEqual(results, [{"id": B}])
            self.assertEqual(order, ["closed-old", "read-new"])
            self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
