"""Evidence semantics regression cases: no real messages or execution side effects."""
from copy import deepcopy
import unittest
from codex_trace.domain import project, resolve_relationships
import test_projection as fixtures

raw, api = fixtures.raw, fixtures.api


def usage(order, input_tokens=0, *, owner='thread-a', epoch='session-a', output=0, cached=0, response=None, **extra):
    values = {'input_tokens': input_tokens, 'output_tokens': output, 'cached_input_tokens': cached}
    payload = {'thread_id': owner, 'turn_id':'turn-a', 'session_id':epoch, 'root_turn_id':'turn-a',
               'response_id':response or f'response-{order}', 'usage':values, 'turn_token_usage':values,
               'thread_token_usage':values, **extra}
    return raw('token_usage_record', payload, order)


def activity(id='spawn', child='child-b', kind='started', **extra):
    return {'id':id, 'type':'subAgentActivity', 'agentThreadId':child, 'agentPath':'/root/child', 'kind':kind, **extra}


class DomainEvidenceTests(unittest.TestCase):
    # Reuse the contract validator, not the M1 tests.
    validate = fixtures.ProjectionTests.validate
    def test_usage_cumulative_is_latest_not_sum_or_max(self):
        result = project('thread-a', [usage(0,100), usage(1,150), usage(2,150), usage(3,80)])
        self.validate(result)
        self.assertEqual(len(result['mainUsage']),1)
        value = result['mainUsage'][0]
        self.assertEqual(value['input']['value'],80)
        self.assertIn('计数下降',value['coverage'])
        self.assertEqual(value['attribution'],'unseparated')
        self.assertEqual(value['cacheSemantics'],'includedInInput')
        self.assertEqual(result['statistics']['modelRequests']['value'],None)

    def test_usage_missing_metric_does_not_reuse_old_value_and_real_zero_is_recorded(self):
        later=usage(1,200)
        later['record']['payload']['thread_token_usage']={'input_tokens':200,'cached_input_tokens':0}
        result=project('thread-a',[usage(0,100,output=10),later])
        self.validate(result)
        value=result['mainUsage'][0]
        self.assertEqual(value['output'],{'availability':'notRecorded','value':None})
        self.assertEqual(value['cachedInput'],{'availability':'recorded','value':0})

    def test_usage_main_family_excludes_other_family_children_and_ancestor_owner(self):
        rows=[usage(0,100), usage(1,900,owner='child-b'),
              raw('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':9999}}},2)]
        result=project('thread-a',rows)
        self.assertEqual(result['mainUsage'][0]['input']['value'],100)
        self.assertEqual(result['mainUsage'][0]['source']['rawType'],'token_usage_record')

    def test_inherited_baseline_is_not_claimed_self_only(self):
        rows=[raw('session_meta',{'id':'thread-a','forked_from_id':'parent-p'}),usage(1,250)]
        result=project('thread-a',rows)
        self.assertEqual(result['mainUsage'][0]['attribution'],'includesInherited')
        self.assertIn('继承',result['mainUsage'][0]['coverage'])
        self.assertEqual(result['relationships'][0]['type'],'forked-from')
        self.assertEqual(len(result['groups']),1)

    def test_spawn_inherited_context_alone_does_not_prove_inherited_usage(self):
        context=raw('response_item',{'type':'message','role':'developer','content':[]},0,inherited=True,projectionExcluded=True)
        result=project('thread-a',[context,usage(1,10)])
        self.assertEqual(result['mainUsage'][0]['attribution'],'unseparated')

    def test_usage_epochs_remain_separate_and_are_not_implicitly_summed(self):
        result=project('thread-a',[usage(0,100,epoch='one'),usage(1,30,epoch='two')])
        self.validate(result)
        self.assertEqual([v['input']['value'] for v in result['mainUsage']],[100,30])
        self.assertTrue(all('彼此未求和' in v['coverage'] for v in result['mainUsage']))

    def test_compaction_checkpoint_replaces_usage_and_null_clears_reachable_baseline(self):
        before = usage(0,100)
        checkpoint = raw('compacted',{'message':'summary','latest_token_usage_record':usage(1,70)['record']['payload']},1)
        result = project('thread-a',[before,checkpoint])
        self.validate(result)
        self.assertEqual(len(result['mainUsage']),1)
        self.assertEqual(result['mainUsage'][0]['input']['value'],70)
        self.assertEqual(result['mainUsage'][0]['source']['rawType'],'compacted')
        self.assertIn('checkpoint',result['mainUsage'][0]['coverage'])
        cleared = raw('compacted',{'message':'summary','latest_token_usage_record':None},2)
        self.assertEqual(project('thread-a',[before,checkpoint,cleared])['mainUsage'],[])
        # Following usage starts from the saved reset; it is neither an old max
        # nor a sum of the pre-checkpoint baseline and the new cumulative record.
        result = project('thread-a',[before,cleared,usage(3,8)])
        self.assertEqual(result['mainUsage'][0]['input']['value'],8)

    def test_compaction_does_not_invent_usage_or_read_another_owner_checkpoint(self):
        unrelated = raw('compacted',{'latest_token_usage_record':usage(0,900,owner='other')['record']['payload']})
        self.assertEqual(project('thread-a',[unrelated])['mainUsage'],[])
        legacy = raw('compacted',{'message':'summary'},1)
        self.assertEqual(project('thread-a',[usage(0,50),legacy])['mainUsage'][0]['input']['value'],50)

    def test_per_request_fallback_deduplicates_explicit_request_identity(self):
        one=usage(0,12,response='same',thread_token_usage=None)
        two=usage(1,15,response='same',thread_token_usage=None)
        result=project('thread-a',[one,two])
        self.validate(result)
        self.assertEqual(len(result['mainUsage']),1)
        self.assertEqual(result['mainUsage'][0]['scope'],'request')
        self.assertEqual(result['mainUsage'][0]['input']['value'],15)
        self.assertEqual(result['mainUsage'][0]['attribution'],'selfOnly')
        self.assertIsNone(result['statistics']['modelRequests']['value'])

    def test_token_count_fallback_is_recent_observation_not_request_count(self):
        rows=[raw('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':x,'output_tokens':0}}},i) for i,x in enumerate((100,150,150))]
        result=project('thread-a',rows)
        self.validate(result)
        self.assertEqual(len(result['events']),0)
        self.assertEqual(result['mainUsage'][0]['input']['value'],150)
        self.assertEqual(result['mainUsage'][0]['mode'],'observation')

    def test_unknown_usage_fields_and_invalid_metric_values_are_not_inferred(self):
        rows=[raw('token_usage_record',{'thread_id':'thread-a','future_usage':{'input_tokens':999}},0),
              usage(1,True,output=-1,cached=1.25)]
        result=project('thread-a',rows)
        self.assertEqual(len(result['mainUsage']),1)
        for key in ('input','output','cachedInput'):
            self.assertEqual(result['mainUsage'][0][key]['availability'],'notRecorded')

    def test_turn_terminal_does_not_terminate_thread(self):
        rows=[raw('event_msg',{'type':'task_started','turn_id':'turn-a'},0),
              raw('event_msg',{'type':'task_complete','turn_id':'turn-a'},1)]
        result=project('thread-a',rows,api_turns=[{'id':'turn-a','status':'interrupted'}])
        self.validate(result)
        self.assertEqual(result['turns'][0]['status']['value'],'completed')
        self.assertEqual(result['status']['value'],'unknown')
        self.assertIn('最近轮次已完成',result['status']['explanation'])
        self.assertEqual(result['turns'][0]['status']['evidence'][0]['scope'],'turn')

    def test_lifecycle_missing_time_is_null_and_missing_turn_scope_is_not_guessed(self):
        row=raw('event_msg',{'type':'task_complete','turn_id':'turn-a'})
        row['record'].pop('timestamp')
        result=project('thread-a',[row,raw('event_msg',{'type':'turn_aborted'},1)])
        self.validate(result)
        self.assertEqual(result['turns'][0]['status']['value'],'completed')
        self.assertIsNone(result['turns'][0]['status']['evidence'][0]['observedAt'])
        self.assertEqual(len(result['turns']),1)

    def test_new_turn_after_terminal_remains_unknown_with_last_known_start(self):
        rows=[raw('event_msg',{'type':'task_complete','turn_id':'old'},0),
              raw('event_msg',{'type':'turn_started','turn_id':'new'},1)]
        result=project('thread-a',rows)
        latest=next(t for t in result['turns'] if t['turnId']=='new')
        self.assertEqual(latest['status']['value'],'unknown')
        self.assertEqual(latest['status']['lastKnown'],'running')
        self.assertEqual(result['status']['value'],'unknown')
        self.assertIn('启动',result['status']['explanation'])

    def test_older_lifecycle_turn_is_inserted_before_recent_api_turn(self):
        rows=[raw('event_msg',{'type':'task_complete','turn_id':'old'},0),
              raw('event_msg',{'type':'turn_started','turn_id':'new'},1)]
        result=project('thread-a',rows,api_turns=[{'id':'new'}])
        self.assertEqual([turn['turnId'] for turn in result['turns']],['old','new'])
        self.assertIn('启动',result['status']['explanation'])

    def test_raw_turn_gaps_merge_around_exact_official_anchors(self):
        ids=['earlier','a','gap','b','after']
        rows=[raw('event_msg',{'type':'task_started','turn_id':value},index) for index,value in enumerate(ids)]
        result=project('thread-a',rows,api_turns=[{'id':'a'},{'id':'b'}])
        self.assertEqual([turn['turnId'] for turn in result['turns']],ids)

    def test_error_and_abort_affect_only_explicit_turn(self):
        for payload,status in (({'type':'turn_complete','turn_id':'x','error':{'message':'failure'}},'failed'),
                               ({'type':'turn_aborted','turn_id':'x','reason':'interrupted'},'stopped')):
            result=project('thread-a',[raw('event_msg',payload)])
            self.assertEqual(result['turns'][0]['status']['value'],status)
            self.assertEqual(result['status']['value'],'unknown')

    def test_negative_tool_exit_only_fails_that_event(self):
        result=project('thread-a',[],[api(fixtures.item(exitCode=-1))])
        self.validate(result)
        self.assertEqual(result['events'][0]['status']['value'],'failed')
        self.assertEqual(result['status']['value'],'unknown')

    def test_terminal_operation_does_not_prove_output_completeness(self):
        for status,completeness in (('completed','unknown'),('failed','unknown'),('inProgress','fragment')):
            result=project('thread-a',[],[api(fixtures.item(status=status))])
            content=next(part for part in result['events'][0]['content'] if part['part']=='output')
            self.assertEqual(content['completeness'],completeness)
            self.assertFalse(content['sourceTruncated'])

    def test_end_only_time_is_not_invented_as_start_and_status_keeps_evidence(self):
        result=project('thread-a',[],[api(fixtures.item(durationMs=None),start=None,end=0)])
        self.validate(result)
        event=result['events'][0]
        self.assertEqual(event['timing']['precision'],'orderOnly')
        self.assertIsNone(event['timing']['startMs'])
        self.assertIsNone(event['timing']['endMs'])
        self.assertEqual(event['status']['evidence'][0]['observedAt'],0)

    def test_unknown_structured_type_and_status_do_not_break_projection(self):
        rows=[raw('event_msg',{'type':'item_completed','turn_id':'turn-a','item':{'id':'bad','type':['future']}})]
        result=project('thread-a',rows,[api(fixtures.item(status={'future':True},exitCode=0))])
        self.validate(result)
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['events'][0]['status']['value'],'unknown')

    def test_v2_started_creates_explicit_child_and_preserves_creation_event(self):
        result=project('thread-a',[],[api(activity())])
        self.validate(result)
        edge=result['relationships'][0]; group=result['groups'][1]
        self.assertEqual((edge['fromThreadId'],edge['toThreadId']),('child-b','thread-a'))
        self.assertEqual(edge['type'],'spawned-by')
        self.assertEqual(group['groupId'],'main:child-b')
        self.assertEqual(group['parentGroupId'],'main:thread-a')
        self.assertEqual(group['creationEventId'],result['events'][0]['eventId'])
        self.assertEqual(group['loadedEventCount'],0)
        self.assertEqual(group['status']['lastKnown'],'running')
        self.assertEqual(group['status']['value'],'unknown')

    def test_v2_completed_in_initiating_thread_never_creates_a_child(self):
        for kind in ('interacted','completed','interrupted'):
            result=project('thread-a',[],[api(activity(kind=kind))])
            self.assertEqual(len(result['events']),1)
            self.assertEqual(result['relationships'],[])
            self.assertEqual(len(result['groups']),1)

    def test_v2_child_terminal_observation_does_not_finish_root(self):
        result=project('thread-a',[],[api(activity(),end=10),api(activity(id='done',kind='completed'),end=20)])
        self.validate(result)
        self.assertEqual(result['groups'][1]['status']['value'],'completed')
        self.assertEqual(result['status']['value'],'unknown')

    def test_child_observations_follow_official_order_without_wall_clock(self):
        rows=[api(activity(),start=None,end=None),
              api(activity(id='done',kind='completed'),start=None,end=None),
              api(activity(id='next',kind='interacted'),start=None,end=None)]
        completed=project('thread-a',[],rows[:2])
        self.validate(completed)
        self.assertEqual(completed['groups'][1]['status']['value'],'completed')
        self.assertIsNone(completed['groups'][1]['status']['evidence'][0]['observedAt'])
        # A later interaction doesn't prove running, but makes an earlier round's
        # completion insufficient to claim the child is still complete.
        self.assertEqual(project('thread-a',[],rows)['groups'][1]['status']['value'],'unknown')

    def test_old_raw_spawn_does_not_replace_later_child_completion(self):
        row=raw('event_msg',{'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','completed_at_ms':999,
                            'item':{'type':'SubAgentActivity','id':'spawn','kind':'started','agent_thread_id':'child-b'}},1)
        result=project('thread-a',[row],[api(activity(),end=10),api(activity(id='done',kind='completed'),end=20)])
        # Clocks are not a substitute for the authoritative item sequence.
        self.assertEqual(result['groups'][1]['status']['value'],'completed')

    def test_api_raw_v2_identity_has_one_group_and_event(self):
        row=raw('event_msg',{'type':'item_completed','thread_id':'thread-a','turn_id':'turn-a','completed_at_ms':10,
                            'item':{'type':'SubAgentActivity','id':'spawn','kind':'started','agent_thread_id':'child-b','agent_path':'/root/child'}},1)
        result=project('thread-a',[row],[api(activity())])
        self.validate(result)
        self.assertEqual(len(result['events']),1)
        self.assertEqual(len(result['relationships']),1)
        self.assertEqual(len(result['groups']),2)
        self.assertEqual(len(result['relationships'][0]['evidence']),2)

    def test_legacy_spawn_and_ordinary_communication_are_distinct(self):
        base={'id':'spawn','type':'collabAgentToolCall','tool':'spawnAgent','status':'completed','senderThreadId':'thread-a',
              'receiverThreadIds':['child-b'],'agentsStates':{'child-b':{'completed':'done'}}}
        result=project('thread-a',[],[api(base),api({**base,'id':'send','tool':'sendMessage','receiverThreadIds':['peer-c']})])
        self.validate(result)
        self.assertEqual({e['type'] for e in result['relationships']},{'spawned-by','communicates-with'})
        self.assertEqual([g['threadId'] for g in result['groups']],['thread-a','child-b'])
        self.assertEqual(result['groups'][1]['status']['value'],'completed')

    def test_legacy_raw_spawn_end_has_explicit_child(self):
        rows=[raw('event_msg',{'type':'task_started','turn_id':'turn-a'},0),
              raw('event_msg',{'type':'collab_agent_spawn_end','sender_thread_id':'thread-a','call_id':'spawn',
                               'new_thread_id':'child-b','new_agent_nickname':'worker','status':'running','completed_at_ms':10},1)]
        result=project('thread-a',rows,mode='raw')
        self.validate(result)
        self.assertEqual(result['groups'][1]['name'],'worker')
        self.assertEqual(result['groups'][1]['creationEventId'],result['events'][0]['eventId'])

    def test_nested_child_edges_compose_without_changing_event_group_identity(self):
        parent=project('thread-a',[],[api(activity())])
        child=project('child-b',[],[api(activity(id='grandchild',child='child-c'))])
        edges=resolve_relationships(parent['relationships']+child['relationships'])
        self.assertEqual(len(edges),2)
        self.assertTrue(all(e['resolution']=='confirmed' for e in edges))
        self.assertEqual(child['events'][0]['groupId'],parent['groups'][1]['groupId'])
        self.assertEqual(child['groups'][1]['parentGroupId'],parent['groups'][1]['groupId'])

    def test_conflicting_parents_cycles_and_multiple_creation_records_are_not_reparented(self):
        a=project('thread-a',[],[api(activity())])['relationships'][0]
        b=deepcopy(a);b['toThreadId']='another-parent'
        self.assertTrue(all(e['resolution']=='conflict' for e in resolve_relationships([a,b])))
        c=deepcopy(a);c['fromThreadId'],c['toThreadId']=a['toThreadId'],a['fromThreadId']
        self.assertTrue(all(e['resolution']=='cycle' for e in resolve_relationships([a,c])))
        duplicate=deepcopy(a);duplicate['creationEventId']='other-creation'
        result=resolve_relationships([a,duplicate])
        self.assertEqual(result[0]['resolution'],'ambiguous')
        self.assertIsNone(result[0]['creationEventId'])

    def test_parent_metadata_adds_evidence_to_existing_spawn_without_duplicate_group(self):
        parent=project('thread-a',[],[api(activity())])
        child=project('child-b',[raw('session_meta',{'id':'child-b','parent_thread_id':'thread-a'})])
        edges=resolve_relationships(parent['relationships']+child['relationships'])
        self.assertEqual(len(edges),1)
        self.assertEqual(edges[0]['creationEventId'],parent['events'][0]['eventId'])
        self.assertEqual(len(edges[0]['evidence']),2)

    def test_inherited_activity_never_spawns_parent_siblings_under_child(self):
        row=raw('event_msg',{'type':'item_completed','thread_id':'parent','turn_id':'turn-a','item':
            {'type':'SubAgentActivity','id':'spawn','kind':'started','agent_thread_id':'sibling','agent_path':'/root/sibling'}},1,inherited=True)
        result=project('thread-a',[row],[api(activity(child='sibling'))])
        self.assertEqual(result['relationships'],[])
        self.assertEqual(len(result['groups']),1)

    def test_unknown_activity_and_agent_path_do_not_create_relations(self):
        result=project('thread-a',[raw('future',{'type':'spawn_agent','agent_thread_id':'other'})],
                       [api(activity(kind='future')),api({'id':'unknown','type':'future','agentPath':'/root/other'})])
        self.assertEqual(result['events'],[])
        self.assertEqual(result['relationships'],[])

    def test_raw_compaction_summary_retained_without_invented_context(self):
        result=project('thread-a',[raw('compacted',{'message':'Saved summary'})],mode='raw')
        self.validate(result)
        self.assertEqual(result['events'][0]['kind'],'compaction')
        self.assertEqual(result['details'][result['events'][0]['eventId']]['output'],'Saved summary')
        self.assertEqual(result['events'][0]['timing']['durationMs'],None)
        self.assertEqual(result['mainUsage'],[])


if __name__=='__main__':
    unittest.main()
