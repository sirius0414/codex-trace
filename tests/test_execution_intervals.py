"""合成轮次边界验证：时间事实不改变状态，也不把中断后的等待扩成长执行区间。"""
from copy import deepcopy
import unittest

from codex_trace.domain.execution import execution_intervals
from codex_trace.domain.projection import project
import test_projection as fixtures

raw = fixtures.raw


def boundary(kind, at=None, ordinal=0, turn='turn-a', **payload):
    row = raw('event_msg', {'type': kind, 'turn_id': turn, **payload}, ordinal)
    if at is None:
        row['record'].pop('timestamp')
    else:
        row['record']['timestamp'] = at
    return row


def times(result, turn='turn-a'):
    return [(span['startMs'], span['endMs']) for span in result.get(turn, [])]


class ExecutionIntervalTests(unittest.TestCase):
    validate = fixtures.ProjectionTests.validate

    def test_api_seconds_preserve_ten_hour_gap_between_turns_and_do_not_change_status(self):
        api_turns = [{'id': 'first', 'startedAt': 0, 'completedAt': 5, 'durationMs': 5000, 'status': 'completed'},
                     {'id': 'second', 'startedAt': 36005, 'completedAt': 36010, 'status': 'completed'}]
        before = deepcopy(api_turns)
        result = project('thread-a', [], api_turns=api_turns)
        self.validate(result)
        self.assertEqual([times({turn['turnId']: turn['executionIntervals']}, turn['turnId']) for turn in result['turns']],
                         [[(0, 5000)], [(36005000, 36010000)]])
        self.assertEqual(sum(span['durationMs'] for turn in result['turns'] for span in turn['executionIntervals']), 10000)
        self.assertTrue(all(turn['status']['value'] == 'unknown' for turn in result['turns']))
        self.assertTrue(all(turn['executionIntervals'][0]['source'] is None for turn in result['turns']))
        self.assertTrue(all(turn['executionIntervals'][0]['originalUnit'] == 'seconds' for turn in result['turns']))
        self.assertEqual(result['statistics']['executionWallClockMs'], None)
        self.assertEqual(api_turns, before)

    def test_raw_abort_resume_same_turn_remains_two_intervals_over_api_envelope(self):
        rows = [boundary('task_started', ordinal=0, started_at=10),
                boundary('turn_aborted', ordinal=1, completed_at=20),
                boundary('turn_started', ordinal=2, started_at=36020),
                boundary('turn_complete', ordinal=3, started_at=10, completed_at=36030)]
        turns = [{'id': 'turn-a', 'startedAt': 10, 'completedAt': 36030}]
        before = deepcopy((rows, turns))
        result = project('thread-a', rows, api_turns=turns)
        self.validate(result)
        self.assertEqual(times({'turn-a': result['turns'][0]['executionIntervals']}), [(10000, 20000), (36020000, 36030000)])
        self.assertEqual(result['turns'][0]['status']['value'], 'completed')
        self.assertEqual(result['status']['value'], 'unknown')
        self.assertEqual((rows, turns), before)

    def test_raw_boundaries_override_conflicting_api_start_and_end(self):
        rows = [boundary('turn_started', ordinal=0, started_at=5), boundary('turn_complete', ordinal=1, completed_at=8)]
        result = execution_intervals('thread-a', rows, [{'id': 'turn-a', 'startedAt': 0, 'completedAt': 100}])
        self.assertEqual(times(result), [(5000, 8000)])
        self.assertEqual(result['turn-a'][0]['source']['byteStart'], 1000)

    def test_api_fills_only_missing_outer_boundaries_without_crossing_raw_abort(self):
        api_turns = [{'id': 'turn-a', 'startedAt': 0, 'completedAt': 100}]
        result = execution_intervals('thread-a', [boundary('turn_aborted', ordinal=0, completed_at=5),
            boundary('turn_started', ordinal=1, started_at=90)], api_turns)
        self.assertEqual(times(result), [(0, 5000), (90000, 100000)])
        aborted = execution_intervals('thread-a', [boundary('turn_started', ordinal=0, started_at=0),
            boundary('turn_aborted', ordinal=1)], api_turns)
        self.assertNotIn('turn-a', aborted)

    def test_api_fills_missing_start_or_completion_without_overwriting_raw_endpoint(self):
        api_turns = [{'id': 'turn-a', 'startedAt': 0, 'completedAt': 100}]
        self.assertEqual(times(execution_intervals('thread-a', [boundary('turn_started', started_at=3)], api_turns)), [(3000, 100000)])
        self.assertEqual(times(execution_intervals('thread-a', [boundary('turn_complete', completed_at=8)], api_turns)), [(0, 8000)])

    def test_open_start_stays_open_and_stale_api_end_cannot_close_resumed_episode(self):
        rows = [boundary('turn_started', ordinal=0, started_at=0),
                boundary('turn_aborted', ordinal=1, completed_at=1),
                boundary('turn_started', ordinal=2, started_at=10)]
        result = execution_intervals('thread-a', rows, [{'id': 'turn-a', 'startedAt': 0, 'completedAt': 1}])
        self.assertEqual(times(result), [(0, 1000), (10000, None)])
        self.assertEqual(result['turn-a'][1]['precision'], 'startOnly')
        self.assertIsNone(result['turn-a'][1]['durationMs'])

    def test_completion_payload_can_supply_both_boundaries_without_start_event(self):
        result = project('thread-a', [boundary('task_complete', started_at=3, completed_at=7)])
        self.validate(result)
        self.assertEqual(times({'turn-a': result['turns'][0]['executionIntervals']}), [(3000, 7000)])
        end_only = project('thread-a', [boundary('task_complete', completed_at=7, duration_ms=4000)])
        self.validate(end_only)
        self.assertNotIn('executionIntervals', end_only['turns'][0])

    def test_outer_iso_timestamp_is_fallback_and_explicit_payload_seconds_win(self):
        rows = [boundary('turn_started', '1970-01-01T00:00:01.250Z', 0),
                boundary('turn_complete', '1970-01-01T00:00:02.750Z', 1)]
        result = execution_intervals('thread-a', rows)
        self.assertEqual(times(result), [(1250, 2750)])
        self.assertEqual(result['turn-a'][0]['originalUnit'], 'milliseconds')
        rows[0]['record']['payload']['started_at'] = 0
        rows[1]['record']['payload']['completed_at'] = 1
        self.assertEqual(times(execution_intervals('thread-a', rows)), [(0, 1000)])

    def test_foreign_inherited_and_excluded_boundaries_do_not_supply_intervals(self):
        rows = [boundary('task_complete', ordinal=index, turn=f'excluded-{index}', started_at=0, completed_at=10)
                for index in range(4)]
        rows[0]['inherited'] = True
        rows[1]['projectionExcluded'] = True
        rows[2]['record']['payload']['thread_id'] = 'other'
        rows[3]['source']['threadId'] = 'other'
        self.assertEqual(execution_intervals('thread-a', rows), {})
        self.assertEqual(execution_intervals('thread-a', [], [
            {'id': 'turn-a', 'threadId': 'other', 'startedAt': 0, 'completedAt': 1},
            {'threadId': 'other', 'turn': {'id': 'turn-b', 'startedAt': 0, 'completedAt': 1}},
            {'id': 'inherited', 'inherited': True, 'startedAt': 0, 'completedAt': 1},
            {'projectionExcluded': True, 'turn': {'id': 'excluded', 'startedAt': 0, 'completedAt': 1}},
        ]), {})

    def test_invalid_and_reversed_boundaries_never_become_intervals(self):
        for value in [True, False, -1, float('nan'), float('inf'), 10**1000, '1']:
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(execution_intervals('thread-a', [boundary('turn_started', started_at=value)]), {})
                self.assertEqual(execution_intervals('thread-a', [], [{'id': 'turn-a', 'startedAt': value}]), {})
                self.assertEqual(execution_intervals('thread-a', [], [{'id': 'turn-a', 'startedAt': 0, 'completedAt': value}]), {})
        rows = [boundary('turn_started', ordinal=0, started_at=10), boundary('turn_complete', ordinal=1, completed_at=5)]
        self.assertEqual(execution_intervals('thread-a', rows), {})
        self.assertEqual(execution_intervals('thread-a', [], [{'id': 'turn-a', 'startedAt': 10, 'completedAt': 5}]), {})

    def test_missing_times_are_omitted_and_api_end_does_not_back_calculate_start(self):
        result = project('thread-a', [boundary('turn_started')], api_turns=[{'id': 'turn-a'}])
        self.validate(result)
        self.assertNotIn('executionIntervals', result['turns'][0])
        self.assertEqual(execution_intervals('thread-a', [], [{'id': 'turn-a', 'completedAt': 10, 'durationMs': 1000}]), {})
        self.assertEqual(times(execution_intervals('thread-a', [], [{'turn': {'id': 'turn-a', 'startedAt': 0}}])), [(0, None)])

    def test_duplicate_terminal_without_restart_cannot_restore_old_start_across_abort(self):
        rows = [boundary('turn_started', ordinal=0, started_at=0),
                boundary('turn_aborted', ordinal=1, completed_at=1),
                boundary('turn_complete', ordinal=2, started_at=0, completed_at=100)]
        self.assertEqual(times(execution_intervals('thread-a', rows, [{'id': 'turn-a', 'startedAt': 0, 'completedAt': 100}])), [(0, 1000)])

    def test_resumed_start_cannot_reuse_payload_start_before_an_observed_abort(self):
        rows = [boundary('turn_started', ordinal=0, started_at=0),
                boundary('turn_aborted', ordinal=1, completed_at=1),
                boundary('turn_started', '1970-01-01T00:00:10Z', 2, started_at=0),
                boundary('turn_complete', ordinal=3, started_at=0, completed_at=11)]
        self.assertEqual(times(execution_intervals('thread-a', rows)), [(0, 1000), (10000, 11000)])
        rows[2]['record'].pop('timestamp')
        self.assertEqual(times(execution_intervals('thread-a', rows)), [(0, 1000)])


if __name__ == '__main__':
    unittest.main()
