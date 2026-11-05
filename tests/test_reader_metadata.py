"""Synthetic reader provenance, exact body preservation and chunk completeness coverage."""
import json
import unittest

from codex_trace.domain.projection import project
from codex_trace.domain.readers import tool_reader
from contracts.test_contracts import validate
from test_projection import api, item, raw
import test_service as service_fixture


def part(event, name):
    return next(ref for ref in event['content'] if ref['part'] == name)


class ReaderMetadataTests(unittest.TestCase):
    def test_command_arrays_remain_arrays_and_shell_language_is_not_guessed(self):
        argv = ['python', '-c', 'print("quoted words")', 'argument with spaces']
        row = raw('event_msg', {'type': 'item_completed', 'turn_id': 't', 'item': {
            'type': 'CommandExecution', 'id': 'cmd', 'command': argv, 'cwd': '/synthetic path',
            'aggregated_output': 'log\n  indented\n', 'exit_code': 0}})
        result = project('thread-a', [row], mode='raw')
        event = result['events'][0]
        self.assertEqual(json.loads(result['details'][event['eventId']]['input']), argv)
        self.assertEqual(part(event, 'input')['reader']['format'], 'json')
        self.assertNotIn('language', part(event, 'input')['reader'])
        self.assertEqual(part(event, 'output')['reader']['format'], 'text')
        self.assertEqual(part(event, 'output')['completeness'], 'unknown')
        self.assertIn({'name': 'cwd', 'value': '/synthetic path'}, part(event, 'input')['reader']['auxiliary'])
        validate('TraceEvent', event)

    def test_json_arguments_are_not_parsed_or_reformatted_and_logs_are_independent(self):
        text = '{ "cmd" : "echo x\\n  echo y", "large": 900719925474099312345, "zero": -0, "duplicate":1,"duplicate":2 }'
        rows = [raw('turn_context', {'turn_id': 't'}),
                raw('response_item', {'type': 'function_call', 'call_id': 'a', 'name': 'exec_command', 'arguments': text}, 1),
                raw('response_item', {'type': 'function_call_output', 'call_id': 'a', 'output': 'one\n  two'}, 2)]
        result = project('thread-a', rows, mode='raw')
        event = result['events'][0]
        self.assertEqual(result['details'][event['eventId']]['input'], text)
        self.assertEqual(part(event, 'input')['reader']['format'], 'json')
        self.assertEqual(part(event, 'output')['reader']['format'], 'text')
        self.assertEqual(part(event, 'output')['completeness'], 'fragment')
        fields = part(event, 'input')['reader']['fields']
        self.assertEqual(next(f for f in fields if f['path'] == '/yield_time_ms')['unit'], 'ms')
        self.assertTrue(all(f['versionMatch'] == 'reference' and f['sourceUrl'].startswith('https://github.com/openai/codex/') for f in fields))
        validate('TraceEvent', event)

    def test_raw_structured_values_preserve_number_lexemes_duplicates_and_spacing(self):
        body = '{ "big":900719925474099312345, "precise":0.1234567890123456789012345, "zero":-0, "k":1, "k":2 }'
        record_text = '{"type":"response_item","payload":{"type":"function_call","turn_id":"t","call_id":"c","name":"unknown","arguments":' + body + '}}\n'
        row = raw('response_item', {}, 1)
        row['raw'] = record_text.encode()
        row['record'] = json.loads(record_text)
        result = project('thread-a', [row], mode='raw')
        event = result['events'][0]
        self.assertEqual(result['details'][event['eventId']]['input'], body)
        item_text = '{"type":"event_msg","payload":{"type":"item_completed","turn_id":"t","item":{"type":"McpToolCall","id":"m","tool":"read","arguments":' + body + '}}}\n'
        row['raw'], row['record'] = item_text.encode(), json.loads(item_text)
        result = project('thread-a', [row], mode='raw')
        self.assertEqual(result['details'][result['events'][0]['eventId']]['input'], body)

    def test_api_origin_discloses_decoded_projection_and_messages_use_reading_convention(self):
        result = project('thread-a', [], [api({'id': 'u', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '# heading'}]})])
        hint = part(result['events'][0], 'input')['reader']
        self.assertEqual(hint['format'], 'markdown')
        self.assertIn('API decoded projection', hint['origin'])
        self.assertIn('convention', hint['origin'])

    def test_known_output_json_is_validated_using_full_body_without_rewriting(self):
        body = '{"output":"logs", "n":900719925474099312345, "decimal":0.1234567890123456789}'
        self.assertEqual(tool_reader('exec_command', 'output', body=body)['format'], 'json')
        self.assertEqual(tool_reader('exec_command', 'output', body=body[:-1])['format'], 'text')
        self.assertEqual(tool_reader('exec_command', 'output', body='[NaN]')['format'], 'text')
        rows = [raw('turn_context', {'turn_id': 't'}),
                raw('response_item', {'type': 'function_call', 'call_id': 'a', 'name': 'exec_command', 'arguments': '{"cmd":"x"}'}, 1),
                raw('response_item', {'type': 'function_call_output', 'call_id': 'a', 'output': body}, 2)]
        result = project('thread-a', rows, mode='raw')
        event = result['events'][0]
        self.assertEqual(part(event, 'output')['reader']['format'], 'json')
        self.assertEqual(result['details'][event['eventId']]['output'], body)

    def test_unknown_tool_and_similar_mcp_name_have_no_invented_definitions(self):
        for name in ('unknown', 'server.exec_command', 'mcp__example__exec_command'):
            hint = tool_reader(name, 'input', function=True)
            self.assertEqual(hint['format'], 'json')
            self.assertNotIn('fields', hint)
            self.assertEqual(hint['adapter'], 'function')
        self.assertIsNone(tool_reader('unknown', 'input'))

    def test_mcp_protocol_descriptions_do_not_describe_arbitrary_tool_fields(self):
        result = project('thread-a', [], [api({'id': 'mcp', 'type': 'mcpToolCall', 'server': 'functions',
            'tool': 'exec_command', 'arguments': {'cmd': 'a business field'}, 'result': {
                'content': [{'type': 'text', 'text': '# readable'}, {'type': 'image', 'text': 'not text content'}],
                'structuredContent': {'cmd': 'not a shell command'}, 'isError': False}})])
        event = result['events'][0]
        self.assertNotIn('fields', part(event, 'input')['reader'])
        fields = part(event, 'output')['reader']['fields']
        self.assertEqual({f['path'] for f in fields}, {'/content', '/isError', '/structuredContent', '/content/0/text'})
        self.assertTrue(all(f['scope'] == 'protocol' and f['versionMatch'] == 'reference' for f in fields))
        validate('TraceEvent', event)

    def test_file_change_hint_requires_known_source_type(self):
        result = project('thread-a', [], [api({'id': 'p', 'type': 'fileChange', 'changes': [
            {'path': 'a.py', 'kind': {'type': 'update'}, 'diff': '@@ -1 +1 @@\n-a\n+b'},
            {'path': 'b.py', 'kind': {'type': 'add'}, 'diff': 'print(1)'}]})])
        hint = part(result['events'][0], 'input')['reader']
        self.assertEqual(hint['adapter'], 'patch')
        self.assertEqual([f['format'] for f in hint['fields']], ['diff', 'code'])
        validate('ReaderMetadata', hint)

    def test_unassociated_result_never_inherits_neighbor_tool_metadata(self):
        rows = [raw('turn_context', {'turn_id': 't'}),
                raw('response_item', {'type': 'function_call', 'call_id': 'a', 'name': 'exec_command', 'arguments': '{"cmd":"x"}'}, 1),
                raw('response_item', {'type': 'function_call_output', 'call_id': 'different', 'output': '{"output":"x"}'}, 2)]
        result = project('thread-a', rows, mode='raw')
        output = next(e for e in result['events'] if e['kind'] == 'toolResult')
        self.assertNotIn('reader', part(output, 'output'))

    def test_missing_and_partial_bodies_keep_metadata_without_claiming_completeness(self):
        result = project('thread-a', [], [api(item(aggregatedOutput=None)),
            api({'id': 'f', 'type': 'dynamicToolCall', 'tool': 'unknown', 'arguments': '{"incomplete":'})])
        command, function = result['events']
        self.assertEqual(part(command, 'output')['completeness'], 'notRecorded')
        self.assertEqual(part(function, 'input')['reader']['format'], 'json')
        self.assertEqual(result['details'][function['eventId']]['input'], '{"incomplete":')


class ReaderChunkTests(unittest.TestCase):
    setUp = service_fixture.ServiceTests.setUp
    read = service_fixture.ServiceTests.read
    # 使用现有隔离 service fixture，只新增详情分块断言；不访问真实任务目录。
    def test_reader_metadata_survives_every_chunk_without_changing_text(self):
        tail = self.read()
        identifier = tail['events'][0]['eventId']
        event = next(e for e in self.service.tasks[self.binding['threadId']].projection['events'] if e['eventId'] == identifier)
        text = '中文' * 35000 + 'needle-outside-preview'
        self.service.tasks[self.binding['threadId']].projection['details'][identifier]['input'] = text
        hint = {'format': 'json', 'origin': 'synthetic source', 'adapter': 'function'}
        event['content'] = [{'part': 'input', 'reader': hint, 'sourceTruncated': True, 'completeness': 'unknown'}]
        request = {'binding': self.binding, 'generation': tail['generation'], 'eventId': identifier, 'part': 'input', 'chunkCursor': None}
        first = self.service.read_event(request)
        self.assertIsNotNone(first['chunks'][0]['nextCursor'])
        second = self.service.read_event({**request, 'chunkCursor': first['chunks'][0]['nextCursor']})
        for response in (first, second):
            validate('EventDetail', response)
            self.assertEqual(response['chunks'][0]['reader'], hint)
            self.assertTrue(response['chunks'][0]['sourceTruncated'])
            self.assertEqual(response['chunks'][0]['completeness'], 'unknown')
        self.assertIsNone(second['chunks'][0]['nextCursor'])
        self.assertEqual(first['chunks'][0]['text'] + second['chunks'][0]['text'], text)
        first['chunks'][0]['reader']['origin'] = 'mutated response'
        self.assertEqual(event['content'][0]['reader']['origin'], 'synthetic source')
