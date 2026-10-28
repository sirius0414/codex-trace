"""Synthetic projection fixtures, checked against public output contracts."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator
from codex_display.domain.projection import project, statistics

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'contracts/trajectory.schema.json').read_text())


def raw(kind, payload, ordinal=0, **extra):
    record = {'type': kind, 'timestamp': '1970-01-01T00:00:00Z', 'payload': payload}
    encoded = json.dumps(record).encode() + b'\r\n'
    return {'record': record, 'raw': encoded, 'ordinal': ordinal,
            'source': {'kind': 'raw', 'threadId': 'thread-a', 'segmentId': 'seg', 'generation': 0,
                       'byteStart': ordinal*1000, 'byteEnd': ordinal*1000 + len(encoded), 'rawType': kind,
                       'producerVersion': '0.163.0', 'association': 'physicalRecord'}, **extra}


def api(item, turn='turn-a', start=0, end=10):
    return {'turnId': turn, 'startedAtMs': start, 'completedAtMs': end, 'item': item}


def item(id='item-1', **extra):
    return {'id': id, 'type': 'commandExecution', 'command': 'echo hello', 'status': 'completed',
            'aggregatedOutput': 'hello\n', 'exitCode': 0, 'durationMs': 10, **extra}


class ProjectionTests(unittest.TestCase):
    def validate(self, result):
        for key, definition in [('events','TraceEvent'), ('turns','Turn'), ('groups','Group'), ('relationships','Relationship'), ('mainUsage','UsageSnapshot')]:
            for row in result[key]:
                Draft202012Validator({**SCHEMA, '$ref': '#/$defs/' + definition}).validate(row)
        for key, definition in [('status','ExecutionState'), ('statistics','Statistics')]:
            Draft202012Validator({**SCHEMA, '$ref': '#/$defs/' + definition}).validate(result[key])

    def test_known_api_variants_and_zero_time_contract(self):
        rows = [api({'id':'u','type':'userMessage','content':[{'type':'text','text':'user\nmessage'}]}),
                api({'id':'a','type':'agentMessage','text':'assistant'}),
                api({'id':'r','type':'reasoning','summary':['readable summary'],'content':['not a summary']}),
                api(item()), api({'id':'compact','type':'contextCompaction'}),
                api({'id':'mcp','type':'mcpToolCall','server':'local','tool':'read','arguments':{},'result':{'content':[]},'status':'completed'})]
        result = project('thread-a', [], rows)
        self.validate(result)
        self.assertEqual(len(result['events']), 6)
        self.assertEqual(result['events'][0]['timing']['startMs'], 0)
        self.assertNotIn('not a summary', json.dumps(result))
        self.assertEqual(result['statistics']['toolCalls']['value'], 2)
        self.assertIsNone(result['statistics']['modelRequests']['value'])
        self.assertEqual(result['status']['value'], 'unknown')
        self.assertEqual(result['events'][0]['preview'], 'user message')

    def test_unknown_records_never_project_or_leak_to_details(self):
        result = project('thread-a', [raw('unknown', {'secret':'UNKNOWN'}), raw('event_msg', {'type':'future','text':'UNKNOWN'})], [api({'id':'x','type':'future','text':'UNKNOWN'})])
        self.assertEqual(result['events'], [])
        self.assertNotIn('UNKNOWN', json.dumps(result))
        self.validate(result)

    def test_api_raw_exact_identity_is_one_event(self):
        r = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','started_at_ms':0,'completed_at_ms':10,
                             'item': {'id':'item-1','type':'CommandExecution','command':['echo','hello'], 'status':'completed','aggregated_output':'hello\n'}}, 3)
        result = project('thread-a', [r], [api(item())])
        self.validate(result)
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(len(event['sources']), 2)
        self.assertEqual(result['details'][event['eventId']]['raw'], r['raw'].decode())
        self.assertEqual(event['eventId'], project('thread-a', [r], mode='raw')['events'][0]['eventId'])

    def test_newer_raw_completion_is_not_erased_by_stale_api(self):
        r = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','started_at_ms':0,'completed_at_ms':20,
                             'item': {'id':'item-1','type':'CommandExecution','command':['echo','hello'], 'status':'completed','aggregated_output':'new complete'}}, 3)
        result = project('thread-a', [r], [api(item(status='inProgress',aggregatedOutput=None),end=None)])
        event = result['events'][0]
        self.validate(result)
        self.assertEqual(event['status']['value'],'completed')
        self.assertEqual(result['details'][event['eventId']]['output'],'new complete')
        self.assertEqual(len(event['sources']),2)

    def test_same_text_different_id_is_not_merged(self):
        r = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','item': {'id':'other','type':'CommandExecution','command':['echo','hello'],'status':'completed'}}, 3)
        result = project('thread-a', [r], [api(item())])
        self.assertEqual(len(result['events']), 2)
        api_event = next(e for e in result['events'] if e['sources'][0]['kind']=='api')
        self.assertNotIn('raw', result['details'][api_event['eventId']])

    def test_same_item_id_in_different_turn_or_thread_is_distinct(self):
        a = project('thread-a', [], [api(item()), api(item(), turn='turn-b')])
        b = project('thread-b', [], [api(item())])
        self.assertEqual(len({e['eventId'] for e in a['events']}), 2)
        self.assertNotEqual(a['events'][0]['eventId'], b['events'][0]['eventId'])

    def test_context_and_environment_keep_explicit_source(self):
        rows = [raw('session_meta', {'id':'thread-a','base_instructions': {'text':'recorded system'}, 'cwd':'/example'}),
                raw('turn_context', {'turn_id':'turn-a','cwd':'/example','model':'example'}, 1),
                raw('response_item', {'type':'message','role':'developer','content':[{'type':'input_text','text':'<skills_instructions>recorded instruction</skills_instructions>'}]},2)]
        result = project('thread-a', rows)
        self.validate(result)
        self.assertEqual([e['kind'] for e in result['events']], ['systemContext','environment','environment','developerContext'])
        for event in result['events']:
            self.assertTrue(result['details'][event['eventId']]['raw'])
        self.assertIsNone(result['events'][0]['turnId'])
        self.assertEqual(result['events'][-1]['turnId'], 'turn-a')

    def test_custom_tool_fragment_remains_outer_layer_and_stable(self):
        rows = [raw('turn_context', {'turn_id':'turn-a'},0),
                raw('response_item', {'type':'custom_tool_call','call_id':'call-a','name':'functions.exec','input':'echo hello'},1)]
        initial = project('thread-a', rows, [api(item())])
        rows.append(raw('response_item', {'type':'custom_tool_call_output','call_id':'call-a','output':'hello'},2))
        final = project('thread-a', rows, [api(item())])
        self.validate(final)
        self.assertEqual(len(final['events']), 2)
        outer = next(e for e in final['events'] if e['toolName']=='functions.exec')
        self.assertEqual(outer['eventId'], next(e for e in initial['events'] if e['toolName']=='functions.exec')['eventId'])
        self.assertEqual(final['details'][outer['eventId']]['output'], 'hello')
        self.assertEqual(next(c for c in outer['content'] if c['part']=='output')['completeness'], 'fragment')
        self.assertEqual(outer['status']['value'], 'unknown')
        self.assertEqual(final['statistics']['toolCalls']['value'], 2)
        for event in final['events']:
            parts=final['details'][event['eventId']]
            for content in event['content']:
                self.assertEqual(content['readableBytes'],len(parts[content['part']].encode('utf-8')))

    def test_attached_sources_and_replaced_output_keep_exact_content_sizes(self):
        row = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','started_at_ms':0,'completed_at_ms':20,
                               'item':{'id':'item-1','type':'CommandExecution','command':['echo','hello'],'status':'completed','aggregated_output':'多字节结果\n' * 100}},3)
        result = project('thread-a',[row],[api(item(status='inProgress',aggregatedOutput='partial'),end=None)])
        self.validate(result)
        event=result['events'][0]
        for content in event['content']:
            text=result['details'][event['eventId']][content['part']]
            self.assertEqual(content['readableBytes'],len(text.encode('utf-8')))
        before=deepcopy(row)
        event['sources'][0]['association']='physicalRecord'
        event['sources'][1]['byteStart']=42
        self.assertEqual(row,before)

    def test_call_id_pair_records_elapsed_return_time_without_finishing_background_work(self):
        rows = [raw('turn_context', {'turn_id': 'turn-a'}),
                raw('response_item', {'type': 'custom_tool_call', 'call_id': 'c', 'name': 'exec', 'input': 'start'}, 1),
                raw('response_item', {'type': 'custom_tool_call_output', 'call_id': 'c', 'output': 'Script running with cell ID 4'}, 2)]
        rows[1]['record']['timestamp'] = '1970-01-01T00:00:01.125Z'
        rows[2]['record']['timestamp'] = '1970-01-01T00:00:32.250Z'
        before = deepcopy(rows)
        result = project('thread-a', rows)
        self.validate(result)
        event = result['events'][0]
        self.assertEqual((event['timing']['startMs'], event['timing']['endMs'], event['timing']['durationMs']), (1125, 32250, 31125))
        self.assertEqual(event['status']['value'], 'unknown')
        self.assertEqual(next(c for c in event['content'] if c['part'] == 'output')['completeness'], 'fragment')
        self.assertEqual(len(event['sources']), 2)
        self.assertEqual(rows, before)
        rows[2]['record']['timestamp'] = '1970-01-01T00:00:00Z'
        self.assertIsNone(project('thread-a', rows)['events'][0]['timing']['endMs'])

    def test_ambiguous_or_cross_turn_call_ids_cannot_supply_elapsed_time(self):
        rows = [raw('turn_context', {'turn_id': 'turn-a'}),
                raw('response_item', {'type': 'custom_tool_call', 'call_id': 'c', 'name': 'exec'}, 1),
                raw('response_item', {'type': 'custom_tool_call', 'call_id': 'c', 'name': 'exec'}, 2),
                raw('response_item', {'type': 'custom_tool_call_output', 'call_id': 'c', 'output': 'result'}, 3)]
        rows[-1]['record']['timestamp'] = '1970-01-01T00:00:10Z'
        self.assertTrue(all(e['timing']['endMs'] is None for e in project('thread-a', rows)['events']))
        rows[2] = raw('turn_context', {'turn_id': 'turn-b'}, 2)
        self.assertTrue(all(e['timing']['endMs'] is None for e in project('thread-a', rows)['events']))

    def test_image_extension_keeps_execution_boundary_separate_from_outer_exec_and_wait(self):
        rows = [raw('turn_context', {'turn_id': 'turn-a'}),
                raw('response_item', {'type': 'custom_tool_call', 'call_id': 'outer', 'name': 'exec'}, 1),
                raw('response_item', {'type': 'custom_tool_call_output', 'call_id': 'outer', 'output': 'Script running with cell ID 4'}, 2),
                raw('response_item', {'type': 'function_call', 'call_id': 'wait-call', 'name': 'wait', 'arguments': '{"cell_id":"4"}'}, 3),
                raw('event_msg', {'type': 'item_completed', 'turn_id': 'turn-a', 'started_at_ms': 10, 'completed_at_ms': 34510,
                     'item': {'id': 'image', 'type': 'Extension', 'kind': 'image_gen.generation', 'status': 'completed',
                              'revisedPrompt': 'rocket', 'result': 'LARGE_IMAGE_BASE64', 'savedPath': '/example/rocket.png'}}, 4),
                raw('response_item', {'type': 'function_call_output', 'call_id': 'wait-call', 'output': 'image returned'}, 5)]
        for row, at in zip([rows[1], rows[2], rows[3], rows[5]], ['00.000', '31.000', '33.000', '34.600']):
            row['record']['timestamp'] = f'1970-01-01T00:00:{at}Z'
        result = project('thread-a', rows)
        self.validate(result)
        tools = {e['toolName']: e for e in result['events']}
        self.assertEqual(set(tools), {'exec', 'image_gen.generation', 'wait'})
        self.assertEqual(tools['exec']['timing']['durationMs'], 31000)
        self.assertEqual(tools['image_gen.generation']['timing']['durationMs'], 34500)
        self.assertEqual(tools['image_gen.generation']['status']['value'], 'completed')
        self.assertEqual(tools['wait']['timing']['durationMs'], 1600)
        self.assertNotIn('LARGE_IMAGE_BASE64', tools['image_gen.generation']['preview'])
        self.assertEqual(result['details'][tools['image_gen.generation']['eventId']]['output'], '/example/rocket.png')

    def test_reasoning_without_public_summary_keeps_only_recorded_activity_timing(self):
        result = project('thread-a', [raw('event_msg', {'type': 'item_completed', 'turn_id': 'turn-a',
            'started_at_ms': 100, 'completed_at_ms': 800,
            'item': {'id': 'r', 'type': 'Reasoning', 'summary_text': [], 'raw_content': ['PRIVATE_REASONING']}})])
        self.validate(result)
        event = result['events'][0]
        self.assertEqual(event['kind'], 'reasoningSummary')
        self.assertEqual(event['timing']['durationMs'], 700)
        self.assertNotIn('PRIVATE_REASONING', event['preview'])
        self.assertNotIn('PRIVATE_REASONING', result['details'][event['eventId']]['output'])

    def test_missing_scope_or_ambiguous_call_does_not_link_result(self):
        rows = [raw('response_item', {'type':'custom_tool_call','call_id':'c','name':'t','input':'x'},1),
                raw('response_item', {'type':'custom_tool_call_output','call_id':'c','output':'y'},2)]
        result = project('thread-a', rows)
        self.assertEqual([e['kind'] for e in result['events']], ['toolCall','toolResult'])
        rows.insert(0,raw('turn_context', {'turn_id':'t'},0))
        rows.insert(2,raw('response_item', {'type':'custom_tool_call','call_id':'c','name':'t','input':'z'},3))
        result = project('thread-a', rows)
        self.assertEqual(sum(e['kind']=='toolResult' for e in result['events']), 1)

    def test_api_mode_does_not_duplicate_legacy_messages_or_function_calls(self):
        rows = [raw('response_item', {'type':'message','role':'user','content':[{'type':'input_text','text':'same'}]},0),
                raw('response_item', {'type':'function_call','call_id':'f','name':'cmd','arguments':'same'},1)]
        result = project('thread-a', rows, [api({'id':'u','type':'userMessage','content':[{'type':'text','text':'same'}]})])
        self.assertEqual(len(result['events']), 1)

    def test_legacy_raw_mode_and_modern_raw_mode_do_not_mix(self):
        legacy = raw('response_item', {'type':'message','role':'user','content':[{'type':'input_text','text':'same'}]},0)
        result = project('thread-a', [legacy], mode='raw')
        self.assertEqual(len(result['events']), 1)
        modern = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'t','item': {'type':'UserMessage','id':'u','content':[{'type':'text','text':'same'}]}},1)
        result = project('thread-a', [legacy, modern], mode='raw')
        self.assertEqual(len(result['events']), 1)
        self.validate(result)

    def test_projection_exclusion_and_cross_thread_records(self):
        row = raw('event_msg', {'type':'item_completed','thread_id':'other','turn_id':'t','item': {'type':'AgentMessage','id':'a','content':[{'type':'Text','text':'other'}]}})
        self.assertEqual(project('thread-a', [row], mode='raw')['events'], [])
        row['inherited']=True
        self.assertEqual(len(project('thread-a', [row], mode='raw')['events']), 1)
        row['projectionExcluded']=True
        self.assertEqual(project('thread-a', [row], mode='raw')['events'], [])

    def test_cold_turn_status_and_final_message_never_finish_task(self):
        for state in ('interrupted','completed','failed','inProgress'):
            result = project('thread-a', [], [api({'id':'a','type':'agentMessage','text':'final','phase':'final'})], [{'id':'t','status':state}])
            self.assertEqual(result['status']['value'], 'unknown')
            self.assertEqual(result['turns'][0]['status']['value'], 'unknown')

    def test_parallel_statistics_union_and_zero_duration(self):
        result = project('thread-a', [], [api(item('a',durationMs=10),start=0,end=10), api(item('b',durationMs=10),start=5,end=15), api(item('c',durationMs=0),start=0,end=0)])
        self.assertEqual(result['statistics']['toolUnionMs'],15)
        self.assertEqual(result['statistics']['operationCumulativeMs'],20)
        self.assertEqual(result['statistics']['displayedRangeMs'],15)
        self.assertIsNone(result['statistics']['executionWallClockMs'])

    def test_rust_duration_nanoseconds_round_to_integer_ms_preserving_zero(self):
        for nanos, expected in ((9625, 0), (1600000, 2), (0, 0)):
            row = raw('event_msg', {'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a',
                       'item': {'id':'duration','type':'CommandExecution','command':['true'],
                                'status':'completed','duration':{'secs':0,'nanos':nanos}}})
            result = project('thread-a', [row], mode='raw')
            self.validate(result)
            timing = result['events'][0]['timing']
            self.assertIs(type(timing['durationMs']), int)
            self.assertEqual(timing['durationMs'], expected)
            self.assertEqual(timing['precision'], 'durationOnly')
            self.assertIn('整数毫秒', timing['basis'])

    def test_no_mutation_and_repeatability(self):
        rows=[raw('turn_context', {'turn_id':'turn-a','cwd':'/x'})]
        items=[api(item())]
        before=deepcopy((rows,items))
        a=project('thread-a', rows, items)
        self.assertEqual(a,project('thread-a',rows,items))
        self.assertEqual((rows,items),before)

if __name__=='__main__':
    unittest.main()
