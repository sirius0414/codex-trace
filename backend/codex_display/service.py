"""Local observation sessions. All task access goes through read-only adapters."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from uuid import uuid4
from types import SimpleNamespace

from .probe import candidate_identity, canonical_uuid, URI
from .observation import Observation

SCHEMA_VERSION = "1.0.0"
PARSER_VERSION = "1.1.0"
PAGE_SIZE = 200
HISTORY_PAGE_BUDGET = 64
HISTORY_ITEM_LIMIT = 100_000
HISTORY_TIME_BUDGET_SECONDS = 8.0
VIEW_IDLE_SECONDS = 3600
MAX_VIEWS = 128
MAX_CACHED_TASKS = 64


def now_ms():
    return int(time.time() * 1000)


def default_view():
    return {"filters": {"text": "", "kinds": [], "toolName": None, "failedOnly": False, "timeRange": None},
            "agentScope": {"kind": "all", "groupId": None}, "overviewMode": "duration",
            "selectedEventId": None, "detailTab": "preview", "expandedTurnIds": [],
            "expandedGroupIds": [], "follow": "following", "anchor": None}


class ServiceError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(message)


@dataclass
class View:
    epoch: int = 0
    thread_id: str | None = None
    loaded: set = field(default_factory=set)
    loaded_set_id: str = field(default_factory=lambda: str(uuid4()))
    scope_revision: int = 0
    page_cursor: str | None = None
    generation: int | None = None
    group_cursors: dict = field(default_factory=dict)
    requested_groups: set = field(default_factory=set)
    batch: dict | None = None
    batch_context: dict | None = None
    last_seen: float = field(default_factory=time.monotonic)


@dataclass
class Task:
    metadata: dict
    raw: object
    api_items: list
    api_turns: list
    api_cursor: str | None
    mode: str
    projection: dict
    generation: int = 1
    revision: int = 1
    last_read: int = 0
    last_progress: int | None = None
    error: str | None = None
    api_error: str | None = None
    index_error: bool = False
    last_api: float = 0
    event_hashes: dict = field(default_factory=dict)
    own_projection: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)
    raw_observed: int | None = None
    raw_success: int | None = None
    api_observed: int | None = None
    api_success: int | None = None
    raw_failures: int = 0
    api_failures: int = 0
    raw_retry_at: float = 0
    api_due_at: float = 0
    raw_coverage: object | None = None
    source_health: dict = field(default_factory=dict)
    io_lock: object = field(default_factory=threading.RLock)
    last_access: float = field(default_factory=time.monotonic)


class TrajectoryService(Observation):
    def __init__(self, history, raw, store, projector=None, *, observe=False, exports=None, export_destination=None):
        if projector is None:
            from .domain.projection import project
            projector = project
        self.history, self.raw, self.store, self.project = history, raw, store, projector
        self.source_id = store.source_instance_id
        self.views: dict[str, View] = {}
        self.tasks: dict[str, Task] = {}
        self.cursors: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.exports = exports
        self.export_destination = export_destination
        self._last_reap = time.monotonic()
        self._init_observation(observe)

    def _forget_view(self, identifier):
        view = self.views.pop(identifier)
        self._clear_change_batch(view)
        self.cursors = {key: value for key, value in self.cursors.items()
                        if value['binding']['viewSessionId'] != identifier}

    def _protected_tasks(self):
        roots = {view.thread_id for view in self.views.values() if view.thread_id}
        protected = set(roots)
        for identifier in roots:
            task = self.tasks.get(identifier)
            if task:
                protected.update(group['threadId'] for group in task.projection['groups'])
        return protected

    def _reap(self, *, force=False):
        """Bound our own caches; persisted preferences/receipts are untouched."""
        now = time.monotonic()
        if not force and now - self._last_reap < 60:
            return
        self._last_reap = now
        for identifier, view in list(self.views.items()):
            if now - view.last_seen >= VIEW_IDLE_SECONDS:
                self._forget_view(identifier)
        protected = self._protected_tasks()
        for identifier, task in list(self.tasks.items()):
            if identifier not in protected and now - task.last_access >= 60:
                del self.tasks[identifier]
                self._discovery_failures.pop(identifier, None)

    def _reserve_task_slot(self, identifier):
        self._reap(force=True)
        protected = self._protected_tasks() | {identifier}
        while len(self.tasks) >= MAX_CACHED_TASKS:
            candidates = [(task.last_access, key) for key, task in self.tasks.items() if key not in protected]
            if not candidates:
                inactive = [(view.last_seen, key) for key, view in self.views.items()
                            if time.monotonic() - view.last_seen >= 60]
                if not inactive:
                    raise ServiceError('resourceLimit', '同时观察的任务数量已达上限；请切换或稍后重新打开。', True)
                self._forget_view(min(inactive)[1])
                protected = self._protected_tasks() | {identifier}
                continue
            _, oldest = min(candidates)
            del self.tasks[oldest]
            self._discovery_failures.pop(oldest, None)

    def open(self, metadata, entrypoint="global"):
        candidate = candidate_identity(metadata, entrypoint=entrypoint)
        identifier = candidate["candidateThreadId"]
        verified = False
        if entrypoint == "thread" and identifier:
            try:
                self.history.read(identifier)
                verified = True
            except Exception:
                pass
        with self.lock:
            self._reap(force=True)
            # Repeated host activations have no close notification. Under
            # pressure reclaim only sessions that have not read for a minute.
            if len(self.views) >= MAX_VIEWS:
                inactive = [(view.last_seen, key) for key, view in self.views.items()
                            if time.monotonic() - view.last_seen >= 60]
                if inactive:
                    self._forget_view(min(inactive)[1])
                else:
                    raise ServiceError("resourceLimit", "观察会话数量已达上限，请关闭旧页面并在一分钟后重新打开。", True)
            view_id = str(uuid4())
            view = self.views[view_id] = View()
            identity = {"state": "selectionRequired", "reason": "unverified", "explanation": "无法确定当前任务，请选择。"}
            # Only the thread entrypoint's provenance was verified on this host.
            if entrypoint == "thread" and identifier:
                if verified:
                    view.thread_id, view.epoch = identifier, 1
                    identity = {"state": "verified", "threadId": identifier, "selectionEpoch": 1}
                else:
                    identity["explanation"] = "来源任务暂时无法只读校验，请选择或重试。"
            elif not identifier:
                identity["reason"] = {"missing-identity": "missing", "invalid-identity": "invalid",
                                      "conflicting-identities": "conflict"}.get(candidate["identityReason"], "unverified")
            return {"viewSessionId": view_id, "sourceInstanceId": self.source_id, "identity": identity, "uiResourceUri": URI}

    def _view(self, view_id):
        if view_id not in self.views:
            raise ServiceError("identityRequired", "观察会话已失效，请重新打开执行轨迹。")
        self.views[view_id].last_seen = time.monotonic()
        return self.views[view_id]

    def _binding(self, binding, *, select=False):
        if binding.get("sourceInstanceId") != self.source_id or binding.get("schemaVersion") != SCHEMA_VERSION:
            raise ServiceError("identityRequired", "数据来源已变化，请重新打开。")
        view = self._view(binding["viewSessionId"])
        try:
            identifier = canonical_uuid(binding["threadId"])
        except ValueError:
            raise ServiceError("identityMismatch", "任务 ID 无效。") from None
        epoch = binding["selectionEpoch"]
        if select and epoch > view.epoch:
            self._clear_change_batch(view)
            self.cursors = {key: value for key, value in self.cursors.items()
                            if value['binding']['viewSessionId'] != binding['viewSessionId']}
            view.epoch, view.thread_id = epoch, identifier
            view.loaded.clear()
            view.loaded_set_id, view.scope_revision = str(uuid4()), 0
            view.page_cursor, view.generation, view.batch = None, None, None
            view.requested_groups.clear()
            view.group_cursors.clear()
        if view.epoch != epoch or view.thread_id != identifier:
            raise ServiceError("staleEpoch", "任务选择已变化，旧请求已拒绝。")
        return view

    def _select(self, binding):
        # Validate provenance before I/O, then recheck the epoch after the cold
        # identity read. The global projection lock never covers history I/O.
        with self.lock:
            if binding.get("sourceInstanceId") != self.source_id or binding.get("schemaVersion") != SCHEMA_VERSION:
                raise ServiceError("identityRequired", "数据来源已变化，请重新打开。")
            view = self._view(binding["viewSessionId"])
            previous_epoch = view.epoch
            selecting = binding["selectionEpoch"] > previous_epoch
            if not selecting:
                return self._binding(binding)
        try:
            identifier = canonical_uuid(binding["threadId"])
        except ValueError:
            raise ServiceError("identityMismatch", "任务 ID 无效。") from None
        self.history.read(identifier)
        with self.lock:
            view = self._view(binding["viewSessionId"])
            if view.epoch != previous_epoch:
                return self._binding(binding)
            return self._binding(binding, select=True)

    def list_threads(self, request):
        with self.lock:
            self._view(request["viewSessionId"])
        page = self.history.list_threads(cursor=request.get("cursor"), limit=50)
        with self.lock:
            self._view(request["viewSessionId"])
            query = request.get("query", "").casefold()
            rows = []
            for item in page["data"]:
                source = item.get("sourceType", item.get("source"))
                category = "subtask" if source == "subAgent" else "user" if source in ("cli", "vscode", "appServer", "exec") else "unknown"
                if request.get("sourceScope") == "user" and category != "user":
                    continue
                title = item.get("name") or item["id"][:8]
                if query and query not in title.casefold():
                    continue
                updated = item.get('updatedAt')
                updated_ms = int(updated * 1000) if type(updated) in (int, float) and 0 <= updated <= (2**53 - 1) / 1000 else None
                rows.append({"threadId": item["id"], "title": title, "source": category,
                             "updatedAt": updated_ms, "parentThreadId": None})
            return {"viewSessionId": request["viewSessionId"], "threads": rows, "nextCursor": page.get("nextCursor")}

    def _stamp_events(self, task, old=None):
        previous = {e["eventId"]: e for e in old.projection["events"]} if old else {}
        for event in task.projection["events"]:
            eid = event["eventId"]
            value = {key: val for key, val in event.items() if key != "eventRevision"}
            last = previous.get(eid)
            if last is not None and value == {key: val for key, val in last.items() if key != "eventRevision"} and task.projection["details"].get(eid) == old.projection["details"].get(eid):
                event["eventRevision"] = last["eventRevision"]
                task.event_hashes[eid] = old.event_hashes[eid]
                continue
            digest = hashlib.sha256(json.dumps([value, task.projection["details"].get(eid)], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            event["eventRevision"] = (last["eventRevision"] + (old.event_hashes.get(eid) != digest)) if last else 1
            task.event_hashes[eid] = digest

    def _load(self, identifier):
        with self.lock:
            if identifier in self.tasks:
                self.tasks[identifier].last_access = time.monotonic()
                return self.tasks[identifier]
            self._reserve_task_slot(identifier)
        metadata = self.history.read(identifier)
        raw = self.raw.read(identifier, metadata)
        api_error = None
        try:
            page = self.history.read_history(identifier, max_items=PAGE_SIZE)
            items, turns, cursor, mode = list(reversed(page["items"])), list(reversed(page["turns"])), page.get("nextCursor"), "api"
        except Exception:
            # A single source mode is fixed for this task generation, never mixed
            # in reaction to a transient later failure.
            items, turns, cursor, mode = [], [], None, "raw"
            api_error = "历史 API 不可用，当前使用可读取的原始记录。"
        projection = self.project(identifier, raw.records, items, turns, mode=mode)
        task = Task(metadata, raw, items, turns, cursor, mode, projection,
                    generation=1 + raw.generation, last_read=now_ms(), api_error=api_error)
        task.own_projection = projection
        task.raw_observed = task.raw_success = task.last_read
        self._raw_health(task, raw)
        task.api_observed = task.last_read
        task.api_success = None if api_error else task.last_read
        task.api_due_at = time.monotonic() + (0.75 if not raw.records else 5)
        task.last_progress = max((e["timing"]["startMs"] for e in projection["events"] if e["timing"]["startMs"] is not None), default=None)
        task.last_api = time.monotonic()
        with self.lock:
            if identifier in self.tasks:
                return self.tasks[identifier]
            self._reserve_task_slot(identifier)
            self.tasks[identifier] = task
            task.projection = self._compose(identifier, projection)
            self._stamp_events(task)
        self._observe_wakeup.set()
        # Persist summaries and source references; details remain independently
        # readable and are not duplicated into a large serialized cache blob.
        try:
            self.store.save_projection(identifier, task.generation, PARSER_VERSION,
                                       {key: value for key, value in projection.items() if key != "details"})
        except Exception:
            task.index_error = True
        return task

    def _cursor(self, kind, binding, task, **values):
        if len(self.cursors) >= 10000:
            raise ServiceError('resourceLimit', '当前阅读位置缓存已达上限，请重新打开此观察页面。', True)
        token = str(uuid4())
        self.cursors[token] = {"kind": kind, "binding": dict(binding), "generation": task.generation, **values}
        return token

    def _get_cursor(self, token, kind, binding, task):
        value = self.cursors.get(token)
        if not value or value["kind"] != kind or value["binding"] != binding or value["generation"] != task.generation:
            raise ServiceError("invalidCursor", "读取位置已失效，请重新加载当前范围。")
        return value

    def _coverage(self, view, task, cursor=None):
        raw = task.raw_coverage or task.raw
        sources = raw.sources
        partial = bool(raw.issues) or any(s["state"] != "readable" for s in sources)
        return {"loadedSetId": view.loaded_set_id, "scopeRevision": view.scope_revision,
                "loadedEventCount": len(view.loaded), "hasEarlier": cursor is not None, "nextCursor": cursor,
                "indexState": "failed" if task.index_error else "partial" if task.api_cursor else "complete",
                "sourceCompleteness": "partial" if partial else "unknown", "sources": sources,
                "searchTextCoverage": "fullLoadedContent", "explanation": ("本地索引暂不可保存；当前已读取内容保留。" if task.index_error else "") + "搜索仅覆盖已加载且已识别记录；原始导出独立于此范围。"}

    def _freshness(self, task):
        state = "stale" if task.error or task.api_error else "current"
        raw = task.raw_coverage or task.raw
        sources = [{"sourceId": s["sourceId"], "state": "current" if s["state"] in ("readable", "partial") and (task.raw_coverage is not None or not task.error) else "unavailable",
                "lastObservedAt": task.source_health.get(s["sourceId"], {}).get("observed", task.raw_observed),
                "lastSuccessfulReadAt": task.source_health.get(s["sourceId"], {}).get("success"),
                "lastProgressAt": task.last_progress, "reason": s["reason"] or (task.error if task.raw_coverage is None else None)} for s in raw.sources]
        sources.append({"sourceId": "history-api", "state": "stale" if task.api_error else "current",
                        "lastObservedAt": task.api_observed, "lastSuccessfulReadAt": task.api_success,
                        "lastProgressAt": task.last_progress, "reason": task.api_error})
        return {"state": state, "sources": sources,
                "lastSuccessfulReadAt": task.last_read, "lastProgressAt": task.last_progress,
                "outputGranularity": "savedFragments" if task.raw.records else "completedResults", "limitation": task.error or task.api_error or ("跟随已保存记录，部分工具输出结束后可用。" if task.raw.records else "历史记录更新，部分输出结束后可用；没有可用原始增量来源。")}

    @staticmethod
    def _timeline_origin(metadata):
        # Official Thread.createdAt is whole Unix seconds. It is a fixed axis
        # reference, not evidence of execution starting or elapsed duration.
        created = metadata.get('createdAt')
        if type(created) is int and 0 <= created <= 9007199254740:
            return {'timestampMs': created * 1000, 'basis': 'threadCreatedAt', 'precision': 'second',
                    'explanation': '以主任务创建时间为共同基准（秒级）；不是执行开始或耗时证据。'}
        return {'timestampMs': None, 'basis': 'notRecorded', 'precision': 'notRecorded',
                'explanation': '未记录可靠的主任务创建时间；使用绝对时间，不从已加载事件或子任务推断起点。'}

    def _snapshot(self, binding, view, task, events, cursor):
        projection = task.projection
        from .domain.projection import statistics
        loaded = [event for event in projection["events"] if event["eventId"] in view.loaded]
        groups = deepcopy(projection["groups"])
        for group in groups:
            own = [event for event in loaded if event["groupId"] == group["groupId"]]
            group["hasEarlier"] = bool(view.group_cursors.get(group["groupId"]))
            group["nextCursor"] = view.group_cursors.get(group["groupId"])
            group["loadedEventCount"] = len(own)
            group["loadedToolCallCount"] = len({event["callKey"] for event in own if event["callKey"]})
        snapshot = {"binding": dict(binding), "title": task.metadata.get("name") or binding["threadId"][:8],
                    "timelineOrigin": self._timeline_origin(task.metadata),
                    "generation": task.generation, "revision": task.revision, "status": projection["status"],
                    "rootGroupId": projection["groups"][0]["groupId"], "turns": projection["turns"], "events": events,
                    "groups": groups, "relationships": projection["relationships"], "mainUsage": projection["mainUsage"],
                    "statistics": statistics(loaded), "coverage": self._coverage(view, task, cursor),
                    "capabilities": {"historyPaging": task.mode == "api", "persistedItems": True, "persistedToolFragments": True,
                                     "usageRecords": bool(projection["mainUsage"]), "readOnlyLiveEvents": False,
                                     "validatedStructures": ["response_item", "event_msg.item_completed", "session_meta", "turn_context"],
                                     "verifiedAt": task.last_read}, "freshness": self._freshness(task), "updatedAt": task.last_read}
        return deepcopy(snapshot)

    def _read_earlier_history(self, identifier, task, *, anchor=None, needed=PAGE_SIZE):
        """Advance exact upstream cursors until enough *recognized* rows exist.

        Project each new page independently only to locate stable IDs; rebuild
        the complete projection once per batch. This avoids projecting and
        copying the increasingly large history after every one of 50 pages for
        a 10k-event anchor. Old duplicate items/turns cannot overwrite a newer
        version already present in the task cache. Loaded scope is changed only
        when read_trajectory actually returns rows to that observer.
        """
        generation = task.generation
        if task.mode != "api" or task.api_cursor is None:
            return False
        started = time.monotonic()
        known_items = {(row.get("turnId"), row["item"].get("id")) for row in task.api_items}
        known_events = {event["eventId"] for event in task.projection["events"]}
        known_turns = {turn.get("id") for turn in task.api_turns}
        chunks, earlier_turns, encountered = [], [], set()
        added_count = 0
        cursor = task.api_cursor
        limited = False
        failure = None
        for _ in range(HISTORY_PAGE_BUDGET):
            if cursor is None:
                break
            if time.monotonic() - started >= HISTORY_TIME_BUDGET_SECONDS or len(known_items) >= HISTORY_ITEM_LIMIT:
                limited = True
                break
            if cursor in encountered:
                failure = ServiceError("invalidCursor", "历史来源重复返回同一读取位置，已停止继续读取。")
                break
            encountered.add(cursor)
            try:
                page = self.history.read_history(identifier, max_items=PAGE_SIZE, cursor=cursor)
                next_cursor = page.get("nextCursor")
                if next_cursor is not None and (next_cursor == cursor or next_cursor in encountered):
                    raise ServiceError("invalidCursor", "历史来源重复返回同一读取位置，已停止继续读取。")
                earlier = []
                for row in reversed(page["items"]):
                    key = (row.get("turnId"), row["item"].get("id"))
                    if key not in known_items:
                        earlier.append(row)
                        known_items.add(key)
                if len(known_items) > HISTORY_ITEM_LIMIT:
                    limited = True
                    break  # This page's upstream cursor is retried, not skipped.
                turns = []
                for turn in page["turns"]:
                    if turn.get("id") not in known_turns:
                        turns.append(turn)
                        known_turns.add(turn.get("id"))
                projected = self.project(identifier, [], earlier, turns, mode="api")
                new_ids = {event["eventId"] for event in projected["events"]} - known_events
                known_events.update(new_ids)
                added_count += len(new_ids)
                chunks.append(earlier)
                earlier_turns[:0] = turns
                cursor = next_cursor
                if anchor is not None and anchor in known_events or anchor is None and added_count >= needed:
                    break
            except ServiceError as error:
                failure = error
                break
            except Exception:
                failure = ServiceError("unreadable", "较早历史暂时无法读取；已读取内容保留，可重试原位置。", True)
                break
        else:
            limited = cursor is not None
        if chunks:
            with self.lock:
                if task.generation != generation:
                    raise ServiceError("resetRequired", "来源已重建，请重新加载。", True)
                # Raw commits may run while history I/O is pending. Merge with
                # the current cache only now, under the short commit lock.
                current_keys = {(r.get("turnId"), r["item"].get("id")) for r in task.api_items}
                earlier = [row for chunk in reversed(chunks) for row in chunk
                           if (row.get("turnId"), row["item"].get("id")) not in current_keys]
                candidate_items = earlier + task.api_items
                current_turns = {turn.get("id") for turn in task.api_turns}
                candidate_turns = [turn for turn in earlier_turns if turn.get("id") not in current_turns] + task.api_turns
                turn_order = {}
                for row in candidate_items:
                    turn_order.setdefault(row.get("turnId"), len(turn_order))
                candidate_turns.sort(key=lambda turn: turn_order.get(turn.get("id"), len(turn_order)))
                projection = self.project(identifier, task.raw.records, candidate_items, candidate_turns, mode=task.mode)
                task.api_items, task.api_turns, task.api_cursor = candidate_items, candidate_turns, cursor
                self._commit_projection(identifier, task, projection, historical=True)
        if failure is not None:
            raise failure
        return limited

    def read_trajectory(self, request):
        binding = request["binding"]
        self._select(binding)
        root = self._load(binding["threadId"])
        with self.lock:
            view = self._binding(binding)
            group_id = request.get("groupId") or root.projection["groups"][0]["groupId"]
            group = next((g for g in root.projection["groups"] if g["groupId"] == group_id), None)
            if group is None:
                raise ServiceError("notFound", "分组不存在。")
            identifier = binding["threadId"] if group["kind"] == "main" else group["threadId"]
            if identifier != binding["threadId"]:
                edge = self._resolved_edges().get(group.get("relationshipId"))
                if not edge or edge["type"] != "spawned-by" or edge["resolution"] != "confirmed":
                    raise ServiceError("identityMismatch", "子任务关系未经确认。")
        task = root if identifier == binding["threadId"] else self._load(identifier)
        # Only this task's history requests serialize; raw reads and unrelated
        # task updates are never held behind cold API pagination.
        with task.io_lock:
            with self.lock:
                view = self._binding(binding)
                generation = root.generation
                all_events = [e for e in root.projection["events"] if e["threadId"] == identifier]
                before = None
                if request.get("cursor"):
                    value = self._get_cursor(request["cursor"], "page", binding, root)
                    if value.get("groupId", root.projection["groups"][0]["groupId"]) != group_id:
                        raise ServiceError("invalidCursor", "分页位置属于其他分组。")
                    before = value["before"]
                anchor_id = request.get("anchorEventId")
                anchor = next((i for i,e in enumerate(all_events) if e["eventId"] == anchor_id), None) if anchor_id else None
                end = next((i for i,e in enumerate(all_events) if e["eventId"] == before), 0) if before else len(all_events)
                needed = PAGE_SIZE - end
            limited = False
            if task.mode == "api" and task.api_cursor and (anchor_id and anchor is None or not anchor_id and needed > 0):
                limited = self._read_earlier_history(identifier, task, anchor=anchor_id, needed=max(1, needed))
            with self.lock:
                view = self._binding(binding)
                if root.generation != generation:
                    raise ServiceError("resetRequired", "来源已重建，请重新加载。", True)
                if identifier != binding["threadId"]:
                    self._refresh_ancestors(identifier, historical=True)
                    view.requested_groups.add(group_id)
                if view.generation is not None and view.generation != root.generation:
                    view.loaded.clear()
                    view.group_cursors.clear()
                    view.loaded_set_id, view.scope_revision = str(uuid4()), 0
                self._clear_change_batch(view)
                view.generation = root.generation
                all_events = [e for e in root.projection["events"] if e["threadId"] == identifier]
                end = next((i for i,e in enumerate(all_events) if e["eventId"] == before), 0) if before else len(all_events)
                if anchor_id:
                    anchor = next((i for i,e in enumerate(all_events) if e["eventId"] == anchor_id), None)
                    if anchor is None:
                        if task.api_cursor is not None:
                            raise ServiceError("resourceLimit", "旧阅读位置尚未找到，本次历史读取已达上限；可重试继续查找。", True)
                        raise ServiceError("notFound", "已读取的来源中没有原阅读位置；该记录可能已不可用。")
                    end = min(len(all_events), anchor + PAGE_SIZE // 2)
                events = all_events[max(0, end - PAGE_SIZE):end]
                before = events[0]["eventId"] if events else None
                more = max(0, end - PAGE_SIZE) > 0 or bool(task.api_cursor)
                cursor = self._cursor("page", binding, root, before=before, groupId=group_id) if more else None
                view.group_cursors[group_id] = cursor
                if identifier == binding["threadId"]:
                    view.page_cursor = cursor
                previous_size = len(view.loaded)
                view.loaded.update(e["eventId"] for e in events)
                if len(view.loaded) != previous_size:
                    view.scope_revision += 1
                snapshot = self._snapshot(binding, view, root, events, view.page_cursor)
                if limited:
                    snapshot["coverage"]["explanation"] += " 本次历史读取已达上限，可加载更早内容继续。"
                return snapshot

    def read_event(self, request):
        with self.lock:
            binding = request["binding"]
            view = self._binding(binding)
            task = self.tasks.get(binding["threadId"])
            if task is None:
                raise ServiceError("notFound", "请先加载轨迹范围。")
            if request["generation"] != task.generation:
                raise ServiceError("resetRequired", "来源已重建，请重新加载。")
            event = next((e for e in task.projection["events"] if e["eventId"] == request["eventId"]), None)
            if event is None or event["eventId"] not in view.loaded:
                raise ServiceError("notFound", "记录尚未加载或已不可用。")
            part = request["part"]
            text = task.projection["details"].get(event["eventId"], {}).get(part, "")
            offset = 0
            if request.get("chunkCursor"):
                cursor = self._get_cursor(request["chunkCursor"], "detail", binding, task)
                if cursor["event"] != event["eventId"] or cursor["part"] != part or cursor["revision"] != event["eventRevision"]:
                    raise ServiceError("invalidCursor", "详情内容已变化，请重新读取。")
                offset = cursor["offset"]
            piece = text[offset:offset + 16384]
            next_cursor = self._cursor("detail", binding, task, event=event["eventId"], part=part,
                                       revision=event["eventRevision"], offset=offset + len(piece)) if offset + len(piece) < len(text) else None
            ref = next((c for c in event["content"] if c["part"] == part), {})
            return {"binding": dict(binding), "generation": task.generation, "revision": task.revision,
                    "eventId": event["eventId"], "eventRevision": event["eventRevision"], "availability": "available",
                    "summary": event["preview"], "chunks": [{"part": part, "text": piece, "byteOffset": len(text[:offset].encode()),
                    "readableBytes": len(text.encode()), "nextCursor": next_cursor, "sourceTruncated": ref.get("sourceTruncated", False),
                    "completeness": ref.get("completeness", "unknown")}], "sources": deepcopy(event["sources"]),
                    "rawAssociation": "recorded" if any(s["kind"] == "raw" for s in event["sources"]) else "notRecorded",
                    "explanation": "原始关联未记录" if part == "raw" and not text else "当前块为已读取内容。"}

    def query_loaded_events(self, request):
        with self.lock:
            binding = request["binding"]
            view = self._binding(binding)
            task = self.tasks.get(binding["threadId"])
            if task is None:
                raise ServiceError("notFound", "请先加载轨迹范围。")
            if request["loadedSetId"] != view.loaded_set_id or request["scopeRevision"] != view.scope_revision:
                raise ServiceError("revisionConflict", "已加载范围发生变化，请重新搜索。", True)
            f, scope = request["filters"], request["agentScope"]
            found, untimed = [], 0
            for event in task.projection["events"]:
                if event["eventId"] not in view.loaded:
                    continue
                if scope["kind"] == "main" and event["threadId"] != binding["threadId"]:
                    continue
                if scope["kind"] == "group" and event["groupId"] != scope["groupId"]:
                    continue
                if f["kinds"] and event["kind"] not in f["kinds"] or f["toolName"] and event["toolName"] != f["toolName"]:
                    continue
                if f["failedOnly"] and event["status"]["value"] != "failed":
                    continue
                if f["text"]:
                    details = task.projection["details"].get(event["eventId"], {})
                    haystack = " ".join([event["preview"], event["toolName"] or "", *(v for k, v in details.items() if k not in ("raw", "source"))])
                    if f["text"].casefold() not in haystack.casefold():
                        continue
                if f["timeRange"]:
                    start, end = event["timing"]["startMs"], event["timing"]["endMs"]
                    if start is None:
                        untimed += 1
                        continue
                    timing = event["timing"]
                    if end is None and timing["precision"] == "exact" and timing["durationMs"] is not None:
                        end = start + timing["durationMs"]
                    if (end if end is not None else start) < f["timeRange"]["startMs"] or start > f["timeRange"]["endMs"]:
                        continue
                found.append(event["eventId"])
            return {"binding": dict(binding), "generation": task.generation, "revision": task.revision,
                    "loadedSetId": view.loaded_set_id, "scopeRevision": view.scope_revision, "eventIds": found,
                    "untimedExcludedCount": untimed, "searchTextCoverage": "fullLoadedContent", "explanation": "仅搜索已加载的已识别内容。"}

    def read_view_state(self, request):
        with self.lock:
            view = self._binding(request["binding"])
            saved = self.store.load_view(request["binding"]["threadId"])
            if saved["state"]:
                new_groups = set(saved["state"]["expandedGroupIds"]) - view.requested_groups
                view.requested_groups.update(new_groups)
                task = self.tasks.get(request["binding"]["threadId"])
                if new_groups and task:
                    self._touch(task)
            return {"binding": dict(request["binding"]), "preferenceRevision": saved["preferenceRevision"], "state": saved["state"] or default_view()}

    def save_view_state(self, request):
        with self.lock:
            view = self._binding(request["binding"])
            saved = self.store.save_view(request["binding"]["threadId"], request["expectedPreferenceRevision"], request["state"])
            new_groups = set(request["state"]["expandedGroupIds"]) - view.requested_groups
            if new_groups:
                view.requested_groups.update(new_groups)
                task = self.tasks.get(request["binding"]["threadId"])
                if task is not None:
                    self._touch(task)
            return {"binding": dict(request["binding"]), **saved}

    def close(self):
        self._stop_observation()
        if self.exports is not None:
            self.exports.close()
        self.history.close()
        self.store.close()

    def download_records(self, request):
        from .exports import ExportManager, ExportError
        from .raw_export import RawExportCollector
        with self.lock:
            self._binding(request['binding'])
            if self.exports is None:
                self.exports = ExportManager(self.store, RawExportCollector(self.history, self.raw), self.export_destination)
            exports = self.exports
        try:
            result = exports.handle(request)
        except ExportError as error:
            raise ServiceError(error.code, str(error), error.retryable) from None
        with self.lock:
            self._binding(request['binding'])
        return result
