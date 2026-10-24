"""轮次与单次模型用量的证据回归：合成来源，不读取真实会话。"""
from copy import deepcopy
import unittest

from codex_display.domain import project
import test_projection as fixtures
from test_domain_evidence import usage

raw, api, item = fixtures.raw, fixtures.api, fixtures.item


class ScopedUsageTests(unittest.TestCase):
    validate = fixtures.ProjectionTests.validate

    def test_turn_latest_and_request_dedup_are_independent_of_thread_totals(self):
        first = usage(1, 100, response='one', output=10, cached=50)
        later = usage(2, 220, response='two', output=30, cached=70)
        later['record']['payload']['usage'] = {'input_tokens': 120, 'output_tokens': 20, 'cached_input_tokens': 20}
        duplicate = deepcopy(later)
        duplicate['ordinal'] = 3
        result = project('thread-a', [first, later, duplicate], mode='raw')
        self.validate(result)
        self.assertEqual(result['turns'][0]['usage'][0]['input']['value'], 220)
        self.assertEqual(result['mainUsage'][0]['input']['value'], 220)
        self.assertEqual([event['usage'][0]['input']['value'] for event in result['events']], [100, 120])
        self.assertEqual(len({event['eventId'] for event in result['events']}), 2)
        self.assertEqual(result['statistics']['toolCalls']['value'], 0)
        self.assertIsNone(result['statistics']['modelRequests']['value'])
        self.assertTrue(all(event['modelRequestKey'] for event in result['events']))

    def test_turns_and_sessions_remain_separate_even_when_response_id_repeats(self):
        rows = [usage(0, 100, response='shared', epoch='one'), usage(1, 8, response='shared', epoch='two'),
                usage(2, 50, epoch='two', turn_id='turn-b')]
        result = project('thread-a', rows, mode='raw')
        self.validate(result)
        self.assertEqual([turn['turnId'] for turn in result['turns']], ['turn-a', 'turn-b'])
        self.assertEqual([value['input']['value'] for value in result['turns'][0]['usage']], [100, 8])
        self.assertEqual(result['turns'][1]['usage'][0]['input']['value'], 50)
        self.assertEqual(len(result['events']), 3)
        self.assertEqual(len({event['modelRequestKey'] for event in result['events']}), 3)

    def test_usage_only_earlier_turn_keeps_source_order_around_api_turn(self):
        rows = [usage(0, 30, turn_id='older'), raw('event_msg', {'type': 'task_started', 'turn_id': 'newer'}, 1)]
        result = project('thread-a', rows, api_turns=[{'id': 'newer'}])
        self.assertEqual([turn['turnId'] for turn in result['turns']], ['older', 'newer'])

    def test_checkpoint_restores_turn_without_recreating_request(self):
        record = usage(0, 70)
        checkpoint = raw('compacted', {'latest_token_usage_record': record['record']['payload']}, 1)
        result = project('thread-a', [checkpoint], mode='raw')
        self.validate(result)
        self.assertEqual(result['turns'][0]['usage'][0]['input']['value'], 70)
        self.assertFalse(any(event['kind'] == 'modelUsage' for event in result['events']))
        cleared = raw('compacted', {'latest_token_usage_record': None}, 2)
        result = project('thread-a', [record, checkpoint, cleared], mode='raw')
        self.assertEqual(len([event for event in result['events'] if event['kind'] == 'modelUsage']), 1)
        self.assertEqual(result['turns'][0]['usage'][0]['input']['value'], 70)
        self.assertEqual(result['mainUsage'], [])

    def test_inherited_foreign_and_unscoped_usage_are_never_attached_to_active_turn(self):
        rows = [raw('event_msg', {'type': 'task_started', 'turn_id': 'active'}, 0),
                usage(1, 999, owner='child'), usage(2, 888, turn_id=None),
                usage(3, 777), usage(4, 666)]
        rows[3]['inherited'] = True
        rows[4]['projectionExcluded'] = True
        result = project('thread-a', rows, mode='raw')
        self.validate(result)
        self.assertEqual(result['events'], [])
        self.assertEqual([(turn['turnId'], turn['usage']) for turn in result['turns']], [('active', [])])

    def test_missing_and_zero_metrics_remain_distinct_and_no_snapshot_sum_occurs(self):
        later = usage(1, 5, output=0)
        later['record']['payload']['turn_token_usage'] = {'input_tokens': 5, 'output_tokens': 0}
        result = project('thread-a', [usage(0, 100, output=20), later])
        value = result['turns'][0]['usage'][0]
        self.assertEqual(value['input']['value'], 5)
        self.assertEqual(value['output'], {'availability': 'recorded', 'value': 0})
        self.assertEqual(value['cachedInput'], {'availability': 'notRecorded', 'value': None})
        self.assertIn('计数下降', value['coverage'])

    def test_token_count_observation_and_nearby_tool_do_not_gain_independent_usage(self):
        rows = [raw('turn_context', {'turn_id': 'turn-a'}, 0),
                raw('event_msg', {'type': 'token_count', 'info': {
                    'last_token_usage': {'input_tokens': 10}, 'total_token_usage': {'input_tokens': 900}}}, 1)]
        result = project('thread-a', rows, [api(item())])
        self.validate(result)
        self.assertFalse(any(event['kind'] == 'modelUsage' for event in result['events']))
        self.assertTrue(all(not event.get('usage') for event in result['events']))
        self.assertEqual(result['turns'][0]['usage'], [])
        # 即使准确用量存在，也不能根据条目邻近给工具绑定一个模型请求。
        result = project('thread-a', [usage(2, 10)], [api(item())])
        self.assertEqual(next(event for event in result['events'] if event['kind'] == 'toolCall').get('usage'), None)
        self.assertEqual(len([event for event in result['events'] if event['kind'] == 'modelUsage']), 1)


if __name__ == '__main__':
    unittest.main()
