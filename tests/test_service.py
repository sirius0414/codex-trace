"""Observation isolation, pagination, full loaded search and detail bounds."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from codex_display.service import TrajectoryService, ServiceError, default_view
from contracts.test_contracts import validate

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
FIXTURE = Path(__file__).parent / "fixtures/contracts/snapshot-running.json"


class History:
    def __init__(self): self.reads = []
    def read(self, identifier):
        self.reads.append(identifier)
        return {"id": identifier, "name": "合成任务", "path": "/synthetic-only"}
    def read_history(self, identifier, **_): return {"items": [], "turns": [], "nextCursor": None}
    def list_threads(self, **_):
        return {"data": [{"id": A, "name": "A", "source": "appServer"}, {"id": B, "name": "Internal", "source": "unknown"}], "nextCursor": None}
    def close(self): pass


class Store:
    source_instance_id = "synthetic-source"
    def __init__(self): self.views = {}
    def save_projection(self, *_): pass
    def load_view(self, identifier): return deepcopy(self.views.get(identifier, {"preferenceRevision": 0, "state": None}))
    def save_view(self, identifier, revision, state):
        old = self.load_view(identifier)
        if old["preferenceRevision"] != revision: return {"saved": False, **old}
        self.views[identifier] = {"preferenceRevision": revision + 1, "state": deepcopy(state)}
        return {"saved": True, **self.views[identifier]}
    def close(self): pass


class Raw:
    def read(self, *_): return SimpleNamespace(records=[], sources=[], issues=[], generation=1, fingerprint="synthetic", segments=[])


def project(identifier, *_args, **_kwargs):
    fixture = json.loads(FIXTURE.read_text())
    exemplar = fixture["events"][0]
    events, details = [], {}
    for index in range(450):
        event = deepcopy(exemplar)
        event.update(eventId=f"{identifier}-{index}", threadId=identifier, preview="safe preview")
        event["order"]["ordinal"] = index
        event["timing"]["startMs"] = index * 1000
        event["timing"]["endMs"] = None
        events.append(event)
        details[event["eventId"]] = {"input": "中文" * 10000 + "needle-outside-preview", "raw": "known raw", "source": "source", "summary": "summary"}
    return {**{key: fixture[key] for key in ("turns", "groups", "relationships", "mainUsage", "statistics", "status")}, "events": events, "details": details}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.service = TrajectoryService(self.history, Raw(), Store(), project)
        self.opened = self.service.open({"threadId": A}, "thread")
        self.binding = {"schemaVersion": "1.0.0", "sourceInstanceId": "synthetic-source", "viewSessionId": self.opened["viewSessionId"], "selectionEpoch": 1, "threadId": A}
    def read(self, cursor=None, anchor=None):
        return self.service.read_trajectory({"binding": self.binding, "cursor": cursor, "anchorEventId": anchor})
    def test_global_candidate_is_not_auto_authorized(self):
        opened = self.service.open({"threadId": B}, "global")
        self.assertEqual(opened["identity"]["state"], "selectionRequired")
        self.assertNotIn(B, self.history.reads)
    def test_latest_earlier_pages_are_stable_and_no_duplicates(self):
        tail = self.read()
        validate("ThreadSnapshot", tail)
        self.assertEqual(len(tail["events"]), 200)
        previous = self.read(tail["coverage"]["nextCursor"])
        self.assertEqual(previous["events"], self.read(tail["coverage"]["nextCursor"])["events"])
        self.assertFalse({e["eventId"] for e in tail["events"]} & {e["eventId"] for e in previous["events"]})
        first = self.read(previous["coverage"]["nextCursor"])
        self.assertEqual(len(first["events"]), 50)
        self.assertFalse(first["coverage"]["hasEarlier"])
        self.assertEqual(first["coverage"]["loadedEventCount"], 450)
    def test_old_selection_and_foreign_cursor_cannot_read_other_task(self):
        tail = self.read()
        old = dict(self.binding)
        self.binding = {**self.binding, "selectionEpoch": 2, "threadId": B}
        self.read()
        with self.assertRaises(ServiceError): self.service.read_trajectory({"binding": old, "cursor": None, "anchorEventId": None})
        with self.assertRaises(ServiceError): self.read(tail["coverage"]["nextCursor"])
    def test_search_only_loaded_full_text_excludes_unloaded(self):
        tail = self.read()
        filters = default_view()["filters"]
        filters["text"] = "needle-outside-preview"
        request = {"binding": self.binding, "loadedSetId": tail["coverage"]["loadedSetId"], "scopeRevision": tail["coverage"]["scopeRevision"], "filters": filters, "agentScope": {"kind": "all", "groupId": None}}
        result = self.service.query_loaded_events(request)
        validate("QueryResult", result)
        self.assertEqual(len(result["eventIds"]), 200)
        self.read(tail["coverage"]["nextCursor"])
        with self.assertRaises(ServiceError): self.service.query_loaded_events(request)
    def test_detail_chunks_preserve_utf8_and_binding(self):
        tail = self.read()
        request = {"binding": self.binding, "generation": tail["generation"], "eventId": tail["events"][0]["eventId"], "part": "input", "chunkCursor": None}
        first = self.service.read_event(request)
        validate("EventDetail", first)
        second = self.service.read_event({**request, "chunkCursor": first["chunks"][0]["nextCursor"]})
        self.assertEqual(second["chunks"][0]["byteOffset"], len(first["chunks"][0]["text"].encode()))
        self.assertEqual(first["chunks"][0]["text"] + second["chunks"][0]["text"], "中文" * 10000 + "needle-outside-preview")
    def test_view_preference_compare_and_swap(self):
        self.read()
        initial = self.service.read_view_state({"binding": self.binding})
        validate("ReadViewStateResult", initial)
        request = {"binding": self.binding, "expectedPreferenceRevision": 0, "state": default_view()}
        self.assertTrue(self.service.save_view_state(request)["saved"])
        self.assertFalse(self.service.save_view_state(request)["saved"])


class PagedHistory(History):
    """Only exact opaque cursor tokens work; offsets are private fixture state."""
    def __init__(self, count=10000, *, unknown=None):
        super().__init__()
        self.calls = []
        self.count = count
        self.unknown = set(unknown or ())
        self.tokens = {"fixture-cursor-" + str(end * 7919 + 37): end for end in range(0, count + 1, 200)}
        self.reverse = {value: key for key, value in self.tokens.items()}
        self.fail_cursor = None
        self.repeat = False
        self.duplicates = []
    def item(self, index):
        return {"turnId": "turn-" + str(index // 200),
                "item": {"id": "item-" + str(index), "type": "futureOpaqueItem" if index in self.unknown else "agentMessage",
                         "text": "synthetic message " + str(index)},
                "startedAtMs": index * 1000, "completedAtMs": index * 1000}
    def read_history(self, identifier, *, cursor=None, max_items=200):
        self.calls.append((identifier, cursor))
        if cursor == self.fail_cursor and cursor is not None:
            raise OSError("synthetic history unavailable")
        self.asserted_page_size = max_items
        end = self.count if cursor is None else self.tokens[cursor]
        start = max(0, end - max_items)
        items = [self.item(i) for i in reversed(range(start, end))]
        turns = [{"id": "turn-" + str(i // 200), "status": "completed"} for i in range(start, end, 200)]
        next_cursor = self.reverse[start] if start else None
        if self.repeat and cursor is not None:
            next_cursor = cursor
        return {"items": items + self.duplicates, "turns": turns, "nextCursor": next_cursor}


class AnchorPagingTests(unittest.TestCase):
    def configure(self, count=10000, unknown=None):
        from codex_display.domain.projection import project as official_project
        self.history = PagedHistory(count, unknown=unknown)
        self.projected_counts = []
        def counted(identifier, raw, items, turns, **kwargs):
            self.projected_counts.append(len(items))
            return official_project(identifier, raw, items, turns, **kwargs)
        self.service = TrajectoryService(self.history, Raw(), Store(), counted)
        opened = self.service.open({"threadId": A}, "thread")
        self.binding = {"schemaVersion": "1.0.0", "sourceInstanceId": "synthetic-source", "viewSessionId": opened["viewSessionId"], "selectionEpoch": 1, "threadId": A}
        self.official_project = official_project
    def event_id(self, index, identifier=A):
        return self.official_project(identifier, [], [self.history.item(index)], [], mode="api")["events"][0]["eventId"]
    def read(self, cursor=None, anchor=None, binding=None):
        return self.service.read_trajectory({"binding": binding or self.binding, "cursor": cursor, "anchorEventId": anchor})

    def test_old_10k_anchor_follows_real_cursors_once_and_is_repeatable(self):
        self.configure()
        tail = self.read()
        tail_ids = {e["eventId"] for e in tail["events"]}
        anchor = self.event_id(50)
        result = self.read(anchor=anchor)
        self.assertIn(anchor, {e["eventId"] for e in result["events"]})
        self.assertEqual(len(self.history.calls), 50)
        self.assertEqual([c for _, c in self.history.calls[1:]], [self.history.reverse[end] for end in range(9800, 0, -200)])
        self.assertLessEqual(sum(self.projected_counts), 20000)
        self.assertEqual(len(self.service.tasks[A].api_items), 10000)
        self.assertEqual(len(self.service.tasks[A].api_turns), 50)
        self.assertEqual(result["generation"], tail["generation"])
        self.assertEqual(result["coverage"]["loadedEventCount"], len(tail_ids | {e["eventId"] for e in result["events"]}))
        self.assertLess(result["coverage"]["loadedEventCount"], 10000)
        repeat = self.read(anchor=anchor)
        self.assertEqual(repeat["events"], result["events"])
        self.assertEqual(repeat["coverage"]["scopeRevision"], result["coverage"]["scopeRevision"])
        self.assertEqual(len(self.history.calls), 50)
        validate("ThreadSnapshot", result)

    def test_anchor_skips_unknown_only_pages_without_stopping(self):
        self.configure(1000, unknown=range(200, 800))
        self.read()
        result = self.read(anchor=self.event_id(30))
        self.assertIn(self.event_id(30), {e["eventId"] for e in result["events"]})
        self.assertEqual(len(self.history.calls), 5)
        self.assertFalse(result["coverage"]["hasEarlier"])
        self.assertFalse(any("futureOpaqueItem" in str(event) for event in result["events"]))

    def test_earlier_page_skips_unknown_only_pages_and_reuses_frontend_cursor(self):
        self.configure(1000, unknown=range(200, 800))
        tail = self.read()
        cursor = tail["coverage"]["nextCursor"]
        earlier = self.read(cursor)
        self.assertEqual(len(earlier["events"]), 200)
        self.assertEqual(len(self.history.calls), 5)
        self.assertFalse({e["eventId"] for e in earlier["events"]} & {e["eventId"] for e in tail["events"]})
        self.assertEqual(self.read(cursor)["events"], earlier["events"])
        self.assertEqual(len(self.history.calls), 5)

    def test_initial_unknown_tail_does_not_hide_earlier_known_events(self):
        self.configure(600, unknown=range(200, 600))
        result = self.read()
        self.assertEqual(len(result["events"]), 200)
        self.assertEqual(len(self.history.calls), 3)
        self.assertFalse(result["coverage"]["hasEarlier"])

    def test_anchor_not_found_only_after_source_exhausted(self):
        self.configure(600)
        self.read()
        with self.assertRaises(ServiceError) as failure:
            self.read(anchor="no-such-record")
        self.assertEqual(failure.exception.code, "notFound")
        self.assertIsNone(self.service.tasks[A].api_cursor)
        self.assertEqual(len(self.history.calls), 3)

    def test_limit_keeps_real_cursor_and_retry_continues_without_scope_leak(self):
        self.configure(1000)
        tail = self.read()
        anchor = self.event_id(10)
        with mock.patch("codex_display.service.HISTORY_PAGE_BUDGET", 1):
            with self.assertRaises(ServiceError) as failure:
                self.read(anchor=anchor)
        self.assertEqual(failure.exception.code, "resourceLimit")
        self.assertTrue(failure.exception.retryable)
        self.assertEqual(self.service.tasks[A].api_cursor, self.history.reverse[600])
        self.assertEqual(len(self.service.views[self.binding["viewSessionId"]].loaded), 200)
        result = self.read(anchor=anchor)
        self.assertIn(anchor, {e["eventId"] for e in result["events"]})
        self.assertEqual(len(self.history.calls), 5)
        self.assertEqual(result["coverage"]["loadedSetId"], tail["coverage"]["loadedSetId"])

    def test_unknown_limited_page_returns_continuation_not_no_more_history(self):
        self.configure(1000, unknown=range(200, 1000))
        with mock.patch("codex_display.service.HISTORY_PAGE_BUDGET", 1):
            result = self.read()
        self.assertEqual(result["events"], [])
        self.assertTrue(result["coverage"]["hasEarlier"])
        self.assertIn("上限", result["coverage"]["explanation"])
        continued = self.read(result["coverage"]["nextCursor"])
        self.assertEqual(len(continued["events"]), 200)
        self.assertFalse(continued["coverage"]["hasEarlier"])

    def test_history_failure_preserves_cached_progress_and_original_scope(self):
        self.configure(1000)
        tail = self.read()
        self.history.fail_cursor = self.history.reverse[600]
        with self.assertRaises(ServiceError) as failure:
            self.read(anchor=self.event_id(10))
        self.assertEqual(failure.exception.code, "unreadable")
        self.assertEqual(self.service.tasks[A].api_cursor, self.history.reverse[600])
        self.assertEqual(len(self.service.views[self.binding["viewSessionId"]].loaded), 200)
        self.assertEqual(len(self.service.tasks[A].api_items), 400)
        self.history.fail_cursor = None
        result = self.read(anchor=self.event_id(10))
        self.assertIn(self.event_id(10), {e["eventId"] for e in result["events"]})
        self.assertEqual(result["generation"], tail["generation"])

    def test_item_limit_does_not_skip_the_page_that_exceeds_budget(self):
        self.configure(1000)
        self.read()
        with mock.patch("codex_display.service.HISTORY_ITEM_LIMIT", 500):
            with self.assertRaises(ServiceError) as failure:
                self.read(anchor=self.event_id(10))
        self.assertEqual(failure.exception.code, "resourceLimit")
        self.assertEqual(len(self.service.tasks[A].api_items), 400)
        self.assertEqual(self.service.tasks[A].api_cursor, self.history.reverse[600])
        result = self.read(anchor=self.event_id(10))
        self.assertIn(self.event_id(10), {e["eventId"] for e in result["events"]})
        self.assertEqual(len(self.service.tasks[A].api_items), 1000)
        self.assertEqual([t["id"] for t in self.service.tasks[A].api_turns], ["turn-" + str(i) for i in range(5)])

    def test_time_limit_preserves_cursor_for_explicit_retry(self):
        self.configure(600)
        self.read()
        cursor = self.service.tasks[A].api_cursor
        with mock.patch("codex_display.service.HISTORY_TIME_BUDGET_SECONDS", 0):
            with self.assertRaises(ServiceError) as failure:
                self.read(anchor=self.event_id(10))
        self.assertEqual(failure.exception.code, "resourceLimit")
        self.assertEqual(self.service.tasks[A].api_cursor, cursor)
        self.assertEqual(len(self.history.calls), 1)
        result = self.read(anchor=self.event_id(10))
        self.assertIn(self.event_id(10), {e["eventId"] for e in result["events"]})

    def test_repeated_upstream_cursor_is_rejected_without_duplicate_items(self):
        self.configure(600)
        self.read()
        self.history.repeat = True
        with self.assertRaises(ServiceError) as failure:
            self.read(anchor=self.event_id(10))
        self.assertEqual(failure.exception.code, "invalidCursor")
        self.assertEqual(len(self.service.tasks[A].api_items), 200)

    def test_older_duplicate_cannot_overwrite_newer_item_or_turn(self):
        self.configure(600)
        tail = self.read()
        new_id = self.event_id(599)
        newer = next(e for e in tail["events"] if e["eventId"] == new_id)
        self.history.duplicates = [{"turnId": "turn-2", "item": {"id": "item-599", "type": "agentMessage", "text": "old page must not overwrite"}, "startedAtMs": None, "completedAtMs": None}]
        self.read(anchor=self.event_id(10))
        current = next(e for e in self.service.tasks[A].projection["events"] if e["eventId"] == new_id)
        self.assertEqual(current["preview"], newer["preview"])
        self.assertGreaterEqual(current["eventRevision"], newer["eventRevision"])
        self.assertEqual(len(self.service.tasks[A].api_items), 600)

    def test_anchor_cache_does_not_authorize_other_observer_or_task(self):
        self.configure(600)
        self.read(anchor=self.event_id(10))
        opened = self.service.open({"threadId": A}, "thread")
        other = {**self.binding, "viewSessionId": opened["viewSessionId"]}
        other_tail = self.read(binding=other)
        self.assertEqual(other_tail["coverage"]["loadedEventCount"], 200)
        with self.assertRaises(ServiceError) as failure:
            self.service.read_event({"binding": other, "generation": other_tail["generation"], "eventId": self.event_id(10), "part": "output", "chunkCursor": None})
        self.assertEqual(failure.exception.code, "notFound")
        new_binding = {**self.binding, "threadId": B, "selectionEpoch": 2}
        with self.assertRaises(ServiceError) as failure:
            self.read(anchor=self.event_id(10), binding=new_binding)
        self.assertEqual(failure.exception.code, "notFound")
        self.assertTrue(all(e["threadId"] == B for e in self.service.tasks[B].projection["events"]))

    def test_exact_duration_extends_filter_but_start_only_does_not(self):
        self.configure(200)
        result = self.read()
        events = self.service.tasks[A].projection["events"]
        for event in events:
            event["timing"].update(startMs=None, endMs=None, durationMs=None, precision="orderOnly")
        events[0]["timing"].update(startMs=100, endMs=None, durationMs=500, precision="exact")
        events[1]["timing"].update(startMs=100, endMs=None, durationMs=500, precision="startOnly")
        filters = default_view()["filters"]
        filters["timeRange"] = {"startMs": 400, "endMs": 450}
        found = self.service.query_loaded_events({"binding": self.binding, "loadedSetId": result["coverage"]["loadedSetId"], "scopeRevision": result["coverage"]["scopeRevision"], "filters": filters, "agentScope": {"kind": "all", "groupId": None}})
        self.assertEqual(found["eventIds"], [events[0]["eventId"]])
        self.assertEqual(found["untimedExcludedCount"], 198)


if __name__ == "__main__": unittest.main()
