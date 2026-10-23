"""Shared read-only collectors and revision-bound, UI-private change batches.

Raw and API I/O run on separate workers: a slow cold history request must not
become the fast path for already persisted records. No worker runs task actions.
"""
from __future__ import annotations

from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

RETRY_SECONDS = (1, 2, 5, 10)
CHANGE_PAGE_SIZE = 200
CHANGE_RETENTION = 128
MAX_RELATED_TASKS = 64
ACTIVE_WINDOW_SECONDS = 60


def _now():
    return int(time.time() * 1000)


def _key(row):
    return row.get("turnId"), row.get("item", {}).get("id")


class Observation:
    def _init_observation(self, enabled):
        self._observe_stop = threading.Event()
        self._observe_wakeup = threading.Event()
        self._observe_threads = []
        self._discovery_failures = {}
        if enabled:
            for target in (self._raw_loop, self._api_loop):
                worker = threading.Thread(target=target, name="codex-display-observer", daemon=True)
                self._observe_threads.append(worker)
                worker.start()

    def _active_ids(self):
        now = time.monotonic()
        with self.lock:
            self._reap()
            roots = {view.thread_id for view in self.views.values()
                     if view.thread_id and now - view.last_seen < ACTIVE_WINDOW_SECONDS}
            result = set(roots)
            edges = self._resolved_edges()
            for identifier in roots:
                task = self.tasks.get(identifier)
                if task:
                    for group in task.projection["groups"]:
                        edge = edges.get(group.get("relationshipId"))
                        if edge and edge["type"] == "spawned-by" and edge["resolution"] == "confirmed" and not (group.get("readIssue") or "").startswith("关联存在"):
                            result.add(group["threadId"])
            return sorted(roots) + sorted(result - roots)[:max(0, MAX_RELATED_TASKS - len(roots))]

    def _resolved_edges(self):
        from .domain.relationships import resolve_relationships
        edges = [edge for task in self.tasks.values() for edge in (task.own_projection or task.projection)["relationships"]]
        return {edge["relationshipId"]: edge for edge in resolve_relationships(edges)}

    def _raw_loop(self):
        while not self._observe_stop.is_set():
            for identifier in self._active_ids():
                if self._observe_stop.is_set():
                    break
                try:
                    self.refresh_raw(identifier)
                except Exception:
                    # Fail closed without emitting source bodies or stopping other tasks.
                    self._collector_error(identifier, "raw")
            try:
                if hasattr(self.raw, "wait_for_changes"):
                    self.raw.wait_for_changes(timeout=.25)
                else:
                    self._observe_stop.wait(.25)
            except Exception:
                self._observe_stop.wait(.25)

    def _api_loop(self):
        while not self._observe_stop.is_set():
            identifiers = self._active_ids()
            for identifier in identifiers:
                if self._observe_stop.is_set():
                    break
                with self.lock:
                    task = self.tasks.get(identifier)
                    due = task is not None and time.monotonic() >= task.api_due_at
                try:
                    if task is None:
                        previous = self._discovery_failures.get(identifier, (0, 0))
                        if time.monotonic() < previous[1]:
                            continue
                        # Identity validation and source loading are done outside
                        # the global cache lock, including unavailable children.
                        self._load(identifier)
                        self._discovery_failures.pop(identifier, None)
                        with self.lock:
                            self._refresh_ancestors(identifier)
                    elif due:
                        self.refresh_history(identifier)
                except Exception:
                    if task is None:
                        count = self._discovery_failures.get(identifier, (0, 0))[0] + 1
                        self._discovery_failures[identifier] = (count, time.monotonic() + RETRY_SECONDS[min(count - 1, 3)])
                    else:
                        self._collector_error(identifier, "api")
            self._observe_wakeup.wait(.1)
            self._observe_wakeup.clear()

    def _stop_observation(self):
        self._observe_stop.set()
        self._observe_wakeup.set()
        # Closing this observer's own app-server releases an in-flight request;
        # it never controls the task or the user's app-server process.
        self.history.close()
        for worker in self._observe_threads:
            worker.join(timeout=15)
        if hasattr(self.raw, "close"):
            self.raw.close()

    def _touch(self, task, *, added=(), changed=(), removed=()):
        task.revision += 1
        task.changes.append({"revision": task.revision, "added": set(added),
                             "changed": set(changed), "removed": set(removed)})
        del task.changes[:-CHANGE_RETENTION]

    def _collector_error(self, identifier, source, *, expected_task=None):
        with self.lock:
            task = self.tasks.get(identifier)
            if task is None or expected_task is not None and task is not expected_task:
                return
            if source == "raw":
                first = task.error is None
                task.raw_failures += 1
                task.raw_retry_at = time.monotonic() + RETRY_SECONDS[min(task.raw_failures - 1, 3)]
                task.raw_observed = _now()
                task.error = "原始来源暂不可读；保留已有记录，正在退避重试。"
            else:
                first = task.api_error is None
                task.api_failures += 1
                task.api_due_at = time.monotonic() + RETRY_SECONDS[min(task.api_failures - 1, 3)]
                task.api_observed = _now()
                task.api_error = "历史 API 校验中断；已有记录与原始来源采集保留，正在退避重试。"
            if first:
                self._touch(task)
                self._refresh_ancestors(identifier)

    def _raw_health(self, task, raw):
        observed = _now()
        for source in raw.sources:
            health = task.source_health.setdefault(source["sourceId"], {"observed": None, "success": None})
            health["observed"] = observed
            if source["state"] in ("readable", "partial"):
                health["success"] = observed

    def _compose(self, identifier, own):
        # Projection values are immutable after construction except the event
        # revision and composed group metadata. Copy only those owned shells;
        # large detail strings and untouched evidence can be shared safely.
        result = {**own, "events": [dict(event) for event in own["events"]],
                  "turns": list(own["turns"]), "details": dict(own["details"]),
                  "groups": deepcopy(own["groups"]), "relationships": deepcopy(own["relationships"])}
        groups = {group["groupId"]: group for group in result["groups"]}
        relations = {edge["relationshipId"]: edge for edge in result["relationships"]}
        resolved = self._resolved_edges()
        relations.update({key: value for key, value in resolved.items() if key in relations})
        included = {identifier}
        queue = [(group, 1) for group in list(groups.values()) if group["kind"] != "main"]
        while queue:
            group, depth = queue.pop(0)
            edge = relations.get(group["relationshipId"])
            child_id = group["threadId"]
            if not edge or edge["type"] != "spawned-by" or edge["resolution"] != "confirmed":
                group["readIssue"] = "父子关系尚未确认，未读取关联任务。"
                continue
            if child_id in included or depth > 16 or len(included) >= MAX_RELATED_TASKS:
                group["readIssue"] = "关联存在重复、环或超过本次读取范围；未继续展开。"
                continue
            included.add(child_id)
            child = self.tasks.get(child_id)
            if child is None:
                group["readIssue"] = "子任务记录尚未读取，正在只读校验。"
                continue
            projection = child.own_projection or child.projection
            from .domain.lifecycle import compose_group_status
            group["status"] = compose_group_status(group["status"], projection["status"], projection["turns"], group["groupId"])
            group["usage"] = deepcopy(projection["mainUsage"])
            group["readIssue"] = child.error or child.api_error
            result["events"].extend(dict(event) for event in projection["events"])
            result["turns"].extend(projection["turns"])
            result["details"].update(projection["details"])
            for relation in projection["relationships"]:
                relations.setdefault(relation["relationshipId"], deepcopy(resolved.get(relation["relationshipId"], relation)))
            for nested in projection["groups"]:
                if nested["kind"] == "main":
                    continue
                if nested["groupId"] not in groups:
                    groups[nested["groupId"]] = deepcopy(nested)
                    queue.append((groups[nested["groupId"]], depth + 1))
        result["groups"] = list(groups.values())
        result["relationships"] = list(relations.values())
        return result

    def _commit_projection(self, identifier, task, own, *, historical=False, reset=False, propagate=True):
        old = SimpleNamespace(projection=task.projection, event_hashes=dict(task.event_hashes))
        task.own_projection = own
        task.projection = self._compose(identifier, own)
        task.event_hashes = {}
        self._stamp_events(task, old)
        previous, current = set(old.event_hashes), set(task.event_hashes)
        changed = {key for key in previous & current if old.event_hashes[key] != task.event_hashes[key]}
        keys = ("groups", "turns", "relationships", "mainUsage", "status")
        metadata_changed = any(old.projection.get(key) != task.projection.get(key) for key in keys)
        if reset:
            task.generation += 1
            task.changes.clear()
        if reset or previous != current or changed or metadata_changed:
            self._touch(task, added=() if historical else current - previous, changed=changed, removed=previous - current)
            if not historical and (changed or current - previous or old.projection.get("mainUsage") != own.get("mainUsage")):
                task.last_progress = _now()
        if propagate:
            self._refresh_ancestors(identifier, reset=reset, historical=historical)

    def _refresh_ancestors(self, identifier, *, reset=False, historical=False):
        for parent_id, parent in list(self.tasks.items()):
            if parent_id != identifier and any(group["threadId"] == identifier for group in parent.projection["groups"]):
                self._commit_projection(parent_id, parent, parent.own_projection or parent.projection,
                                        historical=historical, reset=reset, propagate=False)

    def refresh_raw(self, identifier, *, force=False):
        with self.lock:
            task = self.tasks.get(identifier)
            if task is None or not force and time.monotonic() < task.raw_retry_at:
                return False
            metadata = dict(task.metadata)
        try:
            raw = self.raw.read(identifier, metadata)
        except Exception:
            self._collector_error(identifier, "raw", expected_task=task)
            return False
        with self.lock:
            if self.tasks.get(identifier) is not task or task.metadata != metadata:
                return False  # A newer source binding superseded this read.
            task.raw_observed = _now()
            self._raw_health(task, raw)
            readable = any(source["state"] in ("readable", "partial") for source in raw.sources)
            if not readable and raw.sources:
                task.raw_coverage = raw
                self._collector_error(identifier, "raw")
                return False
            changed = raw.fingerprint != task.raw.fingerprint
            recovered = task.error is not None
            unavailable = any(s["state"] in ("missing", "unreadable") for s in raw.sources)
            if not changed and unavailable:
                task.raw_coverage = raw
                self._collector_error(identifier, "raw")
                return False
            task.error = None
            task.raw_coverage = None
            task.raw_success = task.last_read = _now()
            if not changed:
                task.raw_failures, task.raw_retry_at = 0, 0
                if recovered:
                    self._touch(task)
                    self._refresh_ancestors(identifier)
                return False
            reset = raw.generation != task.raw.generation
            old_segments = {s["segmentId"]: s for s in task.raw.segments}
            new_segments = {s["segmentId"]: s for s in raw.segments}
            version_keys = ("device", "inode", "generation", "headerSha256", "inheritedRange")
            own_segments = [s for s in raw.segments if s.get("inheritedRange") is None]
            stable_own = bool(own_segments) and all(s["segmentId"] in old_segments and all(
                s.get(key) == old_segments[s["segmentId"]].get(key) for key in version_keys) for s in own_segments)
            stable_present = all(key not in old_segments or all(s.get(field) == old_segments[key].get(field)
                                 for field in version_keys) for key, s in new_segments.items())
            failed_inheritance = any(s["state"] in ("missing", "unreadable") and s.get("inheritedRange") is not None for s in raw.sources)
            if stable_own and stable_present:
                missing = {key for key, s in old_segments.items() if key not in new_segments and s.get("inheritedRange") is not None}
                if missing and failed_inheritance:
                    # The unchanged owning header proves the same inheritance
                    # boundary. Keep cached rows only for the unavailable range;
                    # export always recaptures independent, currently readable bytes.
                    task.raw_coverage = deepcopy(raw)
                    raw = deepcopy(raw)
                    raw.records[:0] = [row for row in task.raw.records if row["source"]["segmentId"] in missing]
                    raw.segments[:0] = [old_segments[key] for key in missing]
                    task.error = "部分继承来源暂不可读；保留该范围已读记录，其余来源继续更新。"
                    task.raw_failures += 1
                    task.raw_retry_at = time.monotonic() + RETRY_SECONDS[min(task.raw_failures - 1, 3)]
                    reset = False
                elif not missing and all(key in old_segments for key in new_segments):
                    # Reappearance after a temporary lineage lookup failure is
                    # not itself a physical rewrite.
                    reset = False
            if task.error is None:
                task.raw_failures, task.raw_retry_at = 0, 0
            task.raw = raw
            if reset:
                task.api_items, task.api_turns, task.api_cursor = [], [], None
            own = self.project(identifier, raw.records, task.api_items, task.api_turns, mode=task.mode)
            self._commit_projection(identifier, task, own, reset=reset)
            # The first growth starts a bounded coalescing window. Continuous
            # appends cannot push this deadline indefinitely into the future.
            task.api_due_at = min(task.api_due_at, time.monotonic() + .75)
            self._observe_wakeup.set()
            return True

    def _latest_history(self, identifier, known, metadata):
        pages, turns, seen, cursor = [], {}, set(), None
        started = time.monotonic()
        for _ in range(64):
            page = self.history.read_history(identifier, max_items=200, cursor=cursor)
            rows = page["items"]
            pages.append(rows)
            for turn in page["turns"]:
                turns.setdefault(turn.get("id"), turn)
            metadata = page.get("thread", metadata)
            next_cursor = page.get("nextCursor")
            if any(_key(row) in known for row in rows) or next_cursor is None:
                break
            if next_cursor in seen or next_cursor == cursor or time.monotonic() - started > 8:
                raise RuntimeError("history-catchup-limit")
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise RuntimeError("history-catchup-limit")
        return [row for page in reversed(pages) for row in reversed(page)], list(reversed(turns.values())), next_cursor, metadata

    def refresh_history(self, identifier, *, force=False):
        with self.lock:
            task = self.tasks.get(identifier)
            if task is None or not force and time.monotonic() < task.api_due_at:
                return False
        if not task.io_lock.acquire(blocking=False):
            return False
        try:
            with self.lock:
                if self.tasks.get(identifier) is not task:
                    return False
                generation = task.generation
                old_items = list(task.api_items)
                metadata = dict(task.metadata)
            rows, turns, next_cursor, metadata = self._latest_history(identifier, {_key(row) for row in old_items}, metadata)
            # Explicit unfinished history items remain under verification after
            # leaving the latest page. This does not turn cold status into fact.
            unfinished = {row.get("turnId") for row in old_items
                          if row.get("item", {}).get("status") in ("inProgress", "running")}
            if hasattr(self.history, "list_items"):
                if len(unfinished) > 64:
                    raise RuntimeError("unfinished-verification-limit")
                for turn_id in unfinished:
                    cursor = None
                    for _ in range(16):
                        page = self.history.list_items(identifier, turn_id=turn_id, cursor=cursor, limit=200)
                        rows.extend(reversed(page["data"]))
                        cursor = page.get("nextCursor")
                        if cursor is None:
                            break
                    if cursor is not None:
                        raise RuntimeError("unfinished-verification-limit")
            replacement = None
            if metadata.get("path") != task.metadata.get("path"):
                replacement = self.raw.read(identifier, metadata)
                if not any(s["state"] in ("readable", "partial") for s in replacement.sources):
                    raise RuntimeError("replacement-source-unreadable")
            with self.lock:
                if self.tasks.get(identifier) is not task:
                    return False
                if task.generation != generation:
                    task.api_due_at = time.monotonic()
                    return False
                recovered = task.api_error is not None
                origin_changed = self._timeline_origin(task.metadata) != self._timeline_origin(metadata)
                reset = task.mode != "api"
                # A changed physical source must be checked before mixing its
                # new history with raw bytes from the previous source.
                if replacement is not None:
                    task.raw = replacement
                    task.api_items, task.api_turns, task.api_cursor = [], [], None
                    reset = True
                task.metadata = metadata
                was_empty = not task.api_items
                candidate = {_key(row): row for row in task.api_items}
                for row in rows:
                    candidate[_key(row)] = row
                turn_map = {turn.get("id"): turn for turn in task.api_turns}
                turn_map.update({turn.get("id"): turn for turn in turns})
                previous_items, previous_turns = task.api_items, task.api_turns
                task.api_items, task.api_turns = list(candidate.values()), list(turn_map.values())
                turn_order = {}
                for row in task.api_items:
                    turn_order.setdefault(row.get("turnId"), len(turn_order) + 1)
                task.api_turns.sort(key=lambda turn: turn_order.get(turn.get("id"), 0))
                if was_empty:
                    task.api_cursor = next_cursor
                task.mode = "api"
                task.api_error, task.api_failures = None, 0
                task.api_observed = task.api_success = task.last_read = _now()
                task.last_api = time.monotonic()
                task.api_due_at = task.last_api + (5 if task.raw.records else .75)
                if not reset and previous_items == task.api_items and previous_turns == task.api_turns:
                    if recovered or origin_changed:
                        self._touch(task)
                        self._refresh_ancestors(identifier)
                    return recovered or origin_changed
                own = self.project(identifier, task.raw.records, task.api_items, task.api_turns, mode=task.mode)
                before = task.revision
                self._commit_projection(identifier, task, own, reset=reset)
                if (recovered or origin_changed) and task.revision == before:
                    self._touch(task)
                    self._refresh_ancestors(identifier)
                return task.revision != before
        except Exception:
            self._collector_error(identifier, "api", expected_task=task)
            return False
        finally:
            task.io_lock.release()

    def _change_result(self, binding, view, task, after, *, reset=None):
        snapshot = self._snapshot(binding, view, task, [], view.page_cursor)
        return {"binding": dict(binding), "generation": task.generation, "baseRevision": after,
                "revision": task.revision, "resetRequired": reset is not None, "resetReason": reset,
                "continuationCursor": None, "upsertEvents": [], "removeEventIds": [],
                **{key: snapshot[key] for key in ("turns", "groups", "relationships", "status", "timelineOrigin", "mainUsage", "statistics", "coverage", "freshness")}}

    def _clear_change_batch(self, view):
        """Retire only this observer's batch after ack, reset or selection.

        Historical/detail cursors have independent lifetimes. The context
        identity check also makes this safe when a caller repeats cleanup.
        Callers hold the service lock while changing observer state.
        """
        context = view.batch_context
        if context:
            for token in context.get("cursorIds", ()):
                value = self.cursors.get(token)
                if value and value.get("context") is context:
                    del self.cursors[token]
        view.batch, view.batch_context = None, None

    def read_changes(self, request):
        from .service import ServiceError
        binding = request["binding"]
        with self.lock:
            self._binding(binding)
        task = self._load(binding["threadId"])
        with self.lock:
            view = self._binding(binding)
            after = request["afterRevision"]
            if request["generation"] != task.generation:
                self._clear_change_batch(view)
                return self._change_result(binding, view, task, after, reset="generationChanged")
            token = request.get("continuationCursor")
            if token:
                value = self.cursors.get(token)
                if not value or value["kind"] != "changes" or value["binding"] != binding or value["generation"] != task.generation or value["after"] != after:
                    return self._change_result(binding, view, task, after, reset="expiredCursor")
                context = value.get("context")
                if context is not view.batch_context:
                    return self._change_result(binding, view, task, after, reset="expiredCursor")
                if context and not context["committed"] and view.scope_revision != context["scope"]:
                    self._clear_change_batch(view)
                    return self._change_result(binding, view, task, after, reset="expiredCursor")
                if value["result"]["continuationCursor"] is None and context:
                    self._commit_batch(view, context)
                return deepcopy(value["result"])
            if view.batch and view.batch["baseRevision"] == after:
                if not view.batch_context["committed"] and view.scope_revision != view.batch_context["scope"]:
                    self._clear_change_batch(view)
                    return self._change_result(binding, view, task, after, reset="expiredCursor")
                return deepcopy(view.batch)
            # The next base revision acknowledges the previous frozen batch.
            # Single-page and empty reads need no continuation storage at all.
            self._clear_change_batch(view)
            if after > task.revision or task.changes and after < task.changes[0]["revision"] - 1:
                return self._change_result(binding, view, task, after, reset="expiredCursor")
            changed, added, removed = set(), set(), set()
            for change in task.changes:
                if change["revision"] > after:
                    changed.update(change["changed"])
                    added.update(change["added"])
                    removed.update(change["removed"])
            events = task.projection["events"]
            requested = {group["threadId"] for group in task.projection["groups"] if group["groupId"] in view.requested_groups}
            loaded_threads = {event["threadId"] for event in events if event["eventId"] in view.loaded}
            selected = {event["eventId"] for event in events if event["eventId"] in view.loaded and event["eventId"] in changed
                        or event["eventId"] in added and (event["threadId"] == binding["threadId"] or event["threadId"] in loaded_threads)}
            staged = deepcopy(view)
            for child_id in requested - {binding["threadId"]}:
                child_events = [e for e in events if e["threadId"] == child_id]
                selected.update(event["eventId"] for event in child_events[-200:] if event["eventId"] not in view.loaded)
                group_id = next(g["groupId"] for g in task.projection["groups"] if g["threadId"] == child_id)
                child = self.tasks.get(child_id)
                if child and group_id not in view.group_cursors and child_events:
                    staged.group_cursors[group_id] = self._cursor("page", binding, task, groupId=group_id,
                        before=child_events[-200:][0]["eventId"]) if len(child_events) > 200 or child.api_cursor else None
            upserts = [event for event in events if event["eventId"] in selected]
            removals = sorted((view.loaded & removed) - {event["eventId"] for event in events})
            staged.loaded.difference_update(removals)
            staged.loaded.update(selected)
            if staged.loaded != view.loaded:
                staged.scope_revision += 1
            context = {"scope": view.scope_revision, "added": selected, "removed": removals,
                       "groups": staged.group_cursors, "committed": False, "cursorIds": set()}
            result = self._change_result(binding, staged, task, after)
            result["removeEventIds"] = removals
            pages = [upserts[i:i + CHANGE_PAGE_SIZE] for i in range(0, len(upserts), CHANGE_PAGE_SIZE)] or [[]]
            next_token = None
            for page in reversed(pages[1:]):
                chunk = {**result, "upsertEvents": deepcopy(page), "continuationCursor": next_token}
                next_token = self._cursor("changes", binding, task, after=after, result=chunk, context=context)
                context["cursorIds"].add(next_token)
            first = {**result, "upsertEvents": deepcopy(pages[0]), "continuationCursor": next_token}
            # Scope becomes searchable only after the final page was delivered.
            # A lost response can retry the immutable final page without a second commit.
            if first["continuationCursor"] is None:
                self._commit_batch(view, context)
            view.batch = first if task.revision > after or selected else None
            view.batch_context = context if view.batch else None
            return deepcopy(first)

    def _commit_batch(self, view, context):
        if context["committed"]:
            return
        previous = set(view.loaded)
        view.loaded.difference_update(context["removed"])
        view.loaded.update(context["added"])
        view.group_cursors.update(context["groups"])
        if previous != view.loaded:
            view.scope_revision += 1
        context["committed"] = True
