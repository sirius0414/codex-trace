"""Real metadata shapes and bounded long-lived observation sessions."""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from codex_display.service import TrajectoryService, ServiceError
from codex_display.adapters.history import _metadata
from test_service import History, Store, Raw, A, B
from contracts.test_contracts import validate

C = '33333333-3333-4333-8333-333333333333'

class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.service = TrajectoryService(self.history, Raw(), Store())
        self.addCleanup(self.service.close)
    def open(self, thread=A):
        opened = self.service.open({'threadId':thread}, 'thread')
        return {'schemaVersion':'1.0.0','sourceInstanceId':'synthetic-source',
                'viewSessionId':opened['viewSessionId'],'selectionEpoch':1,'threadId':thread}
    def read(self, binding):
        return self.service.read_trajectory({'binding':binding,'cursor':None,'anchorEventId':None})
    def test_real_history_metadata_keeps_user_threads_and_classifies_subagents(self):
        rows = [_metadata({'id':A,'name':'User','source':'appServer','updatedAt':1790000000.125}),
                _metadata({'id':B,'name':'Child','source':{'subAgent':{'thread_spawn':{'parent_thread_id':A}}}}),
                _metadata({'id':C,'name':'Unknown','source':'newSource'})]
        self.history.list_threads = lambda **kw: {'data':rows,'nextCursor':None}
        binding = self.open()
        request = {'viewSessionId':binding['viewSessionId'],'query':'','sourceScope':'user','cursor':None}
        result = self.service.list_threads(request)
        validate('ListThreadsResult', result)
        self.assertEqual([row['threadId'] for row in result['threads']], [A])
        self.assertEqual(result['threads'][0]['updatedAt'], 1790000000125)
        all_rows = self.service.list_threads({**request,'sourceScope':'all'})['threads']
        self.assertEqual([r['source'] for r in all_rows], ['user','subtask','unknown'])
    def test_open_reclaims_expired_views_and_their_cursors(self):
        old = self.open(); self.read(old)
        task = self.service.tasks[A]
        cursor = self.service._cursor('page',old,task,before='synthetic')
        self.service.views[old['viewSessionId']].last_seen = time.monotonic() - 3601
        self.open(B)
        self.assertNotIn(old['viewSessionId'], self.service.views)
        self.assertNotIn(cursor, self.service.cursors)
        with self.assertRaises(ServiceError): self.service.read_view_state({'binding':old})
    def test_pressure_reclaims_inactive_session_without_invalidating_active_one(self):
        with mock.patch('codex_display.service.MAX_VIEWS',2):
            old = self.open(); active = self.open(B)
            self.service.views[old['viewSessionId']].last_seen -= 61
            self.open(C)
            self.assertNotIn(old['viewSessionId'], self.service.views)
            self.assertIn(active['viewSessionId'], self.service.views)
            with self.assertRaises(ServiceError) as caught: self.open()
            self.assertEqual(caught.exception.code,'resourceLimit')
    def test_selection_releases_old_epoch_cursors(self):
        binding = self.open(); self.read(binding)
        cursor = self.service._cursor('detail',binding,self.service.tasks[A],offset=1)
        self.read({**binding,'threadId':B,'selectionEpoch':2})
        self.assertNotIn(cursor,self.service.cursors)
    def test_task_lru_reclaims_previous_selections_and_keeps_current(self):
        with mock.patch('codex_display.service.MAX_CACHED_TASKS',2):
            binding = self.open(); self.read(binding)
            self.read({**binding,'threadId':B,'selectionEpoch':2})
            self.read({**binding,'threadId':C,'selectionEpoch':3})
            self.assertEqual(set(self.service.tasks),{B,C})
            self.read({**binding,'threadId':A,'selectionEpoch':4})
            self.assertEqual(set(self.service.tasks),{A,C})
    def test_active_tasks_are_not_silently_evicted_at_limit(self):
        with mock.patch('codex_display.service.MAX_CACHED_TASKS',2):
            self.read(self.open(A)); self.read(self.open(B))
            with self.assertRaises(ServiceError) as caught: self.read(self.open(C))
            self.assertEqual(caught.exception.code,'resourceLimit')
            self.assertEqual(set(self.service.tasks),{A,B})
    def test_task_pressure_reclaims_idle_view_before_global_view_limit(self):
        with mock.patch('codex_display.service.MAX_CACHED_TASKS',2):
            old = self.open(A); self.read(old)
            active = self.open(B); self.read(active)
            cursor = self.service._cursor('page',old,self.service.tasks[A],before='synthetic')
            self.service.views[old['viewSessionId']].last_seen -= 61
            self.read(self.open(C))
            self.assertEqual(set(self.service.tasks),{B,C})
            self.assertNotIn(old['viewSessionId'],self.service.views)
            self.assertNotIn(cursor,self.service.cursors)
            self.assertIn(active['viewSessionId'],self.service.views)
    def test_timeline_origin_uses_only_valid_official_creation_seconds(self):
        for value in (0, 1790000000, 9007199254740):
            with self.subTest(value=value):
                self.history.read = lambda identifier: {'id':identifier,'createdAt':value}
                self.service.tasks.clear()
                result = self.read(self.open())
                validate('ThreadSnapshot',result)
                self.assertEqual(result['timelineOrigin']['timestampMs'],value*1000)
                self.assertEqual(result['timelineOrigin']['basis'],'threadCreatedAt')
        for value in (None, True, 123.25, '123', -1, 9007199254741):
            with self.subTest(value=value):
                self.history.read = lambda identifier: {'id':identifier,'createdAt':value}
                self.service.tasks.clear()
                result = self.read(self.open())
                validate('ThreadSnapshot',result)
                self.assertIsNone(result['timelineOrigin']['timestampMs'])
                self.assertEqual(result['timelineOrigin']['basis'],'notRecorded')
    def test_idle_unreferenced_task_reaped_without_touching_current(self):
        binding = self.open(); self.read(binding)
        self.read({**binding,'threadId':B,'selectionEpoch':2})
        self.service.tasks[A].last_access -= 61
        with self.service.lock: self.service._reap(force=True)
        self.assertEqual(set(self.service.tasks),{B})
    def test_cursor_cap_does_not_destroy_valid_history_positions(self):
        binding = self.open(); self.read(binding)
        task = self.service.tasks[A]
        original = self.service._cursor('page',binding,task,before='oldest')
        value = self.service.cursors[original]
        self.service.cursors.update({str(i):value for i in range(9999)})
        with self.assertRaises(ServiceError) as caught:
            self.service._cursor('page',binding,task,before='next')
        self.assertEqual(caught.exception.code,'resourceLimit')
        self.assertEqual(self.service._get_cursor(original,'page',binding,task)['before'],'oldest')

if __name__=='__main__':unittest.main()
