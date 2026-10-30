"""A child's turn lifecycle may invalidate, but never terminate, its group."""
from copy import deepcopy
import unittest

from jsonschema import Draft202012Validator
from codex_trace.domain import compose_group_status, unknown_status
from codex_trace.domain.evidence import state
import test_projection as fixtures


CHILD, GROUP = 'child-b', 'main:child-b'
SOURCE = fixtures.raw('event_msg', {})['source']


def observed(value, at=10, *, scope='group', identifier=GROUP, confirmed=True):
    return state(value, scope, identifier, SOURCE, at, 'explicit synthetic observation', confirmed=confirmed)


def turn(value, at=20, *, child=CHILD, identifier='turn-b'):
    return {'threadId': child, 'turnId': identifier, 'order': 0,
            'status': observed(value, at, scope='turn', identifier=identifier, confirmed=value != 'running')}


class GroupStatusTests(unittest.TestCase):
    def compose(self, parent, child=None, turns=()):
        child = child or unknown_status()
        before = deepcopy((parent, child, turns))
        result = compose_group_status(parent, child, turns, GROUP)
        self.assertEqual((parent, child, turns), before)
        Draft202012Validator({**fixtures.SCHEMA, '$ref': '#/$defs/ExecutionState'}).validate(result)
        return result

    def test_unknown_child_thread_does_not_erase_parent_group_completion(self):
        parent = observed('completed')
        self.assertEqual(self.compose(parent), parent)

    def test_older_child_turn_does_not_supersede_later_group_observation(self):
        parent = observed('completed', 20)
        for value in ('running', 'completed', 'failed', 'stopped'):
            self.assertEqual(self.compose(parent, turns=[turn(value, 10)])['value'], 'completed')

    def test_strictly_later_start_invalidates_old_group_completion(self):
        result = self.compose(observed('completed', 10), turns=[turn('running', 20)])
        self.assertEqual(result['value'], 'unknown')
        self.assertEqual(result['lastKnown'], 'completed')
        self.assertIn('启动', result['explanation'])
        self.assertEqual([(e['scope'], e['scopeId'], e['status']) for e in result['evidence']],
                         [('group', GROUP, 'completed'), ('turn', 'turn-b', 'running')])

    def test_later_turn_terminal_only_invalidates_and_does_not_terminate_group(self):
        for value in ('completed', 'failed', 'stopped'):
            result = self.compose(observed('running', 10), turns=[turn(value, 20)])
            self.assertEqual(result['value'], 'unknown')
            self.assertEqual(result['lastKnown'], 'running')
            self.assertFalse(any(e['scope'] == 'group' and e['status'] == value for e in result['evidence']))

    def test_turn_completion_without_group_evidence_is_not_a_group_completion(self):
        result = self.compose(unknown_status(), turns=[turn('completed')])
        self.assertEqual(result['value'], 'unknown')
        self.assertIsNone(result['lastKnown'])

    def test_missing_and_equal_times_do_not_establish_newer_turn(self):
        for parent_time, child_time in ((None, 20), (10, None), (10, 10)):
            with self.subTest(parent=parent_time, child=child_time):
                result = self.compose(observed('completed', parent_time), turns=[turn('running', child_time)])
                self.assertEqual(result['value'], 'completed')

    def test_real_zero_timestamp_is_comparable(self):
        result = self.compose(observed('completed', 0), turns=[turn('running', 1)])
        self.assertEqual(result['value'], 'unknown')
        self.assertEqual(result['evidence'][0]['observedAt'], 0)

    def test_newer_reliable_child_thread_state_maps_to_its_group(self):
        child = observed('failed', 20, scope='thread', identifier=CHILD)
        result = self.compose(observed('completed', 10), child, [turn('completed', 15)])
        self.assertEqual(result['value'], 'failed')
        self.assertEqual(result['lastKnown'], 'failed')
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual((result['evidence'][0]['scope'], result['evidence'][0]['scopeId']), ('group', GROUP))

    def test_older_child_thread_state_cannot_replace_later_parent_observation(self):
        child = observed('completed', 5, scope='thread', identifier=CHILD)
        result = self.compose(observed('running', 10), child)
        self.assertEqual(result['value'], 'running')

    def test_conflicting_thread_evidence_without_order_is_unknown(self):
        child = observed('failed', None, scope='thread', identifier=CHILD)
        result = self.compose(observed('completed', 10), child)
        self.assertEqual(result['value'], 'unknown')
        self.assertEqual(result['lastKnown'], 'completed')
        self.assertTrue(all(e['scope'] == 'group' and e['scopeId'] == GROUP for e in result['evidence']))

    def test_other_thread_and_wrong_scope_cannot_affect_child(self):
        foreign = observed('failed', 30, scope='thread', identifier='other')
        result = self.compose(observed('completed', 10), foreign, [turn('running', 40, child='other')])
        self.assertEqual(result['value'], 'completed')
        wrong = observed('failed', 30, scope='turn', identifier='turn-b')
        self.assertEqual(self.compose(observed('completed', 10), wrong)['value'], 'completed')

    def test_unconfirmed_parent_state_does_not_become_reliable_when_child_loads(self):
        parent = observed('completed')
        parent['evidence'][0]['basis'] = 'unconfirmed'
        result = self.compose(parent)
        self.assertEqual(result['value'], 'unknown')
        self.assertIsNone(result['lastKnown'])

    def test_latest_turn_observation_is_selected_by_evidence_not_list_order(self):
        result = self.compose(observed('completed', 10), turns=[turn('running', 30), turn('completed', 20, identifier='older')])
        self.assertEqual(result['value'], 'unknown')
        self.assertEqual(result['evidence'][-1]['status'], 'running')
        self.assertEqual(result['evidence'][-1]['observedAt'], 30)

    def test_composition_is_repeatable(self):
        parent, turns = observed('completed', 10), [turn('running', 20)]
        first = self.compose(parent, turns=turns)
        self.assertEqual(first, self.compose(first, turns=turns))


if __name__ == '__main__':
    unittest.main()
