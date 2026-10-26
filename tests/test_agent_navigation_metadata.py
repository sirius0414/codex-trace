"""协作跳转只采用明确 Thread ID，且不改变已有父子关系证据。"""
import unittest

from codex_display.domain import project
import test_projection as fixtures


class AgentNavigationMetadataTests(unittest.TestCase):
    validate = fixtures.ProjectionTests.validate

    def test_api_activity_targets_survive_without_creating_child_relationships(self):
        for activity in ('started', 'interacted', 'interrupted', 'completed'):
            with self.subTest(activity=activity):
                result = project('thread-a', [], [fixtures.api({
                    'id': 'activity', 'type': 'subAgentActivity', 'kind': activity,
                    'agentThreadId': 'child-b', 'agentPath': '/root/display-name',
                })])
                self.validate(result)
                self.assertEqual(result['events'][0]['relatedThreadIds'], ['child-b'])
                self.assertEqual(len(result['relationships']), int(activity == 'started'))
                self.assertEqual(len(result['groups']), 2 if activity == 'started' else 1)

    def test_raw_activity_item_and_public_activity_event_share_target_metadata(self):
        records = [
            fixtures.raw('event_msg', {
                'type': 'item_completed', 'thread_id': 'thread-a', 'turn_id': 'turn-a',
                'item': {'id': 'activity', 'type': 'SubAgentActivity', 'kind': 'interacted',
                         'agent_thread_id': 'child-b', 'agent_path': '/root/display-name'},
            }),
            fixtures.raw('event_msg', {
                'type': 'sub_agent_activity', 'thread_id': 'thread-a', 'turn_id': 'turn-a',
                'event_id': 'activity', 'kind': 'interacted', 'agent_thread_id': 'child-b',
                'agent_path': '/root/display-name',
            }),
        ]
        for row in records:
            with self.subTest(raw_type=row['record']['payload']['type']):
                result = project('thread-a', [row], mode='raw')
                self.validate(result)
                self.assertEqual(result['events'][0]['relatedThreadIds'], ['child-b'])
                self.assertEqual(result['relationships'], [])

    def test_legacy_collaboration_preserves_unique_explicit_participants_only(self):
        result = project('thread-a', [], [fixtures.api({
            'id': 'send', 'type': 'collabAgentToolCall', 'tool': 'sendMessage',
            'senderThreadId': 'thread-a',
            'receiverThreadIds': ['child-b', 'thread-a', 'child-b', '', None, 7, 'peer-c'],
            'prompt': 'Mentioned thread-z and /root/unverified are only message text.',
        })])
        self.validate(result)
        self.assertEqual(result['events'][0]['relatedThreadIds'], ['child-b', 'peer-c'])
        self.assertEqual(len(result['groups']), 1)

    def test_exact_raw_item_enriches_incomplete_api_navigation_metadata(self):
        row = fixtures.raw('event_msg', {
            'type': 'item_completed', 'thread_id': 'thread-a', 'turn_id': 'turn-a',
            'item': {'id': 'send', 'type': 'CollabAgentToolCall', 'tool': 'sendMessage',
                     'sender_thread_id': 'thread-a', 'receiver_thread_ids': ['child-b']},
        })
        result = project('thread-a', [row], [fixtures.api({
            'id': 'send', 'type': 'collabAgentToolCall', 'tool': 'sendMessage',
            'senderThreadId': 'thread-a', 'receiverThreadIds': [],
        })])
        self.validate(result)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['events'][0]['relatedThreadIds'], ['child-b'])
        self.assertEqual(len(result['events'][0]['sources']), 2)

    def test_paths_prompt_and_arbitrary_tool_arguments_do_not_become_targets(self):
        result = project('thread-a', [], [fixtures.api({
            'id': 'send', 'type': 'collabAgentToolCall', 'tool': 'sendMessage',
            'senderThreadId': 'thread-a', 'receiverThreadIds': '/root/unverified',
            'prompt': 'thread-z', 'agentPath': '/root/unverified',
        }), fixtures.api({
            'id': 'tool', 'type': 'dynamicToolCall', 'tool': 'sendMessage',
            'arguments': {'target': 'thread-z'}, 'agentThreadId': 'thread-z',
        })])
        self.validate(result)
        self.assertTrue(all('relatedThreadIds' not in event for event in result['events']))


if __name__ == '__main__':
    unittest.main()
