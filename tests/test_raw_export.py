"""Synthetic, byte-for-byte raw exports with controlled source races."""
from dataclasses import replace
import errno
import hashlib
import json
from codex_display.platform import filesystem as os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from jsonschema import Draft202012Validator
from codex_display.adapters.raw import RawAdapter
from codex_display.raw_export import ExportLimits, RawExportCollector, RawExportError
import test_raw as fixtures
import test_projection as projection_fixtures
from platform_fixtures import symlink

A, B, C, D = fixtures.A, fixtures.B, fixtures.C, fixtures.D
line = fixtures.line


class History:
    def __init__(self):
        self.metadata, self.items, self.reads, self.pages = {}, {}, [], []
        self.fail = set()

    def read(self, identifier):
        self.reads.append(identifier)
        if identifier not in self.metadata:
            raise RuntimeError('synthetic-unavailable')
        return self.metadata[identifier]

    def list_items(self, identifier, *, cursor=None, limit=200):
        self.pages.append((identifier, cursor))
        if identifier in self.fail:
            raise RuntimeError('synthetic-unavailable')
        start = int(cursor or '0')
        values = self.items.get(identifier, [])
        return {'data': values[start:start+limit], 'nextCursor': str(start+limit) if start+limit < len(values) else None}


def activity(child, kind='started', identifier='spawn'):
    return {'turnId': 'turn-a', 'startedAtMs': 0, 'completedAtMs': 10,
            'item': {'type':'subAgentActivity', 'id':identifier, 'agentThreadId':child, 'agentPath':'/root/synthetic', 'kind':kind}}


class RawExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root/'sessions'
        self.sessions.mkdir()
        self.private = self.root/'private'
        self.private.mkdir(mode=0o700)
        directory = os.open_directory(self.private, private=True)
        os.close(directory)
        self.raw = RawAdapter([self.sessions])
        self.addCleanup(self.raw.close)
        self.history = History()
        self.collector = RawExportCollector(self.history, self.raw)
        self.destination = self.private/'snapshot.zip'

    def write(self, identifier=A, **kwargs):
        path, header = fixtures.RawTests.write(self, identifier, **kwargs)
        self.history.metadata[identifier] = {'id':identifier, 'path':str(path)}
        return path, header

    def base(self, path, identifier, end=2):
        return fixtures.RawTests.base(self, path, identifier, end)

    def collect(self, identifier=A, collector=None, **kwargs):
        result = (collector or self.collector).collect(identifier, self.destination, **kwargs)
        manifest = result['manifest']
        Draft202012Validator({**projection_fixtures.SCHEMA, '$ref':'#/$defs/ExportManifest'}).validate(manifest)
        self.assertFalse(manifest['atomicSnapshot'])
        data = self.destination.read_bytes()
        self.assertEqual(result['bytes'], len(data))
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(json.loads(archive.read('manifest.json')), manifest)
            values = {name:archive.read(name) for name in archive.namelist() if name != 'manifest.json'}
        for source in manifest['sources']:
            if source['entryName'] is not None:
                value = values[source['entryName']]
                self.assertEqual(source['sha256'],hashlib.sha256(value).hexdigest())
                self.assertEqual(source['copiedRange']['end']-source['copiedRange']['start'],len(value))
        self.assertNotIn(str(self.sessions),json.dumps(manifest))
        return result, values

    def test_unknown_malformed_utf8_crlf_and_partial_tail_are_exact(self):
        body = line('unknown',{'future':'raw'},1,ending=b'\r\n') + b'\xffbad\n{bad\n{"half":"\xe4\xbd'
        path, _ = self.write(body=body)
        before, modified = path.read_bytes(), path.stat().st_mtime_ns
        result, entries = self.collect()
        self.assertEqual(list(entries.values()),[before])
        self.assertTrue(result['manifest']['sources'][0]['trailingPartialLine'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(path.stat().st_mtime_ns,modified)

    def test_only_unknown_body_still_exports_without_ui_events(self):
        path, _ = self.write(body=line('future',{'opaque':'saved'},1))
        result, entries = self.collect()
        self.assertEqual(list(entries.values()),[path.read_bytes()])
        self.assertTrue(result['manifest']['enumerationComplete'])

    def test_relation_discovery_pages_all_history_and_loads_nested_spawns_only(self):
        for identifier in (A,B,C,D):
            self.write(identifier)
        ordinary = [{'turnId':'turn-a','item':{'id':str(i),'type':'agentMessage','text':'synthetic'}} for i in range(201)]
        self.history.items[A] = ordinary+[activity(B),activity(D,'completed','peer-complete')]
        self.history.items[B] = [activity(C)]
        self.history.items[C] = [activity(D,'interacted','communication')]
        result, entries = self.collect()
        self.assertEqual(set(self.history.reads),{A,B,C})
        self.assertEqual({source['threadId'] for source in result['manifest']['sources']},{A,B,C})
        self.assertEqual(len(entries),3)
        self.assertGreaterEqual(sum(identifier==A for identifier,_ in self.history.pages),2)
        self.assertTrue(result['manifest']['enumerationComplete'])

    def test_explicit_raw_spawn_fallback_survives_history_api_failure(self):
        body=line('event_msg',{'type':'task_started','turn_id':'t'},1)+line('event_msg',{'type':'collab_agent_spawn_end','sender_thread_id':A,'call_id':'spawn','new_thread_id':B,'status':'running'},2)
        self.write(body=body); self.write(B)
        self.history.fail.add(A)
        result, entries = self.collect()
        self.assertEqual(len(entries),2)
        self.assertFalse(result['manifest']['enumerationComplete'])

    def test_inherited_prefix_excludes_parent_future_and_is_assigned_to_logical_child(self):
        parent, _=self.write(A,body=line(ordinal=1)+line(ordinal=2,payload={'secret':'PARENT_FUTURE'}))
        base=self.base(parent,A,2)
        child,_=self.write(B,base=base)
        result,entries=self.collect(B)
        self.assertEqual({source['threadId'] for source in result['manifest']['sources']},{B})
        self.assertEqual(len(entries),2)
        self.assertTrue(all(name.startswith(f'records/{B}/') for name in entries))
        self.assertNotIn(b'PARENT_FUTURE',b''.join(entries.values()))
        inherited=next(source for source in result['manifest']['sources'] if source['logicalInheritedRange'])
        self.assertEqual(entries[inherited['entryName']],parent.read_bytes()[:base['end_byte_offset']])
        self.assertIn(child.read_bytes(),entries.values())

    def test_shared_ancestor_ranges_have_distinct_logical_paths(self):
        parent,_=self.write(A)
        self.write(B,base=self.base(parent,A));self.write(C,base=self.base(parent,A))
        self.history.items[A]=[activity(B),activity(C,identifier='second')]
        result,entries=self.collect()
        self.assertEqual(len(entries),5)
        self.assertEqual(len(entries),len({source['entryName'] for source in result['manifest']['sources']}))

    def test_conflicting_child_metadata_excludes_child_bytes(self):
        self.write(A);self.write(B,extra={'parent_thread_id':C});self.write(C)
        self.history.items[A]=[activity(B)]
        result,entries=self.collect()
        self.assertEqual(len(entries),1)
        self.assertEqual(result['manifest']['sources'][0]['threadId'],A)
        self.assertTrue(any(edge['resolution']=='conflict' for edge in result['manifest']['relationships']))

    def test_unreadable_child_is_reported_without_hiding_root(self):
        self.write(A);self.history.items[A]=[activity(B)]
        result,entries=self.collect()
        self.assertEqual(len(entries),1)
        self.assertFalse(result['manifest']['enumerationComplete'])
        self.assertTrue(any(B in reason for reason in result['manifest']['unresolvedRanges']))

    def test_all_unreadable_fails_and_removes_only_owned_zip(self):
        with self.assertRaises(RawExportError) as context:
            self.collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'allSourcesUnreadable')
        self.assertFalse(self.destination.exists())

    def modifying_collector(self, operation, stage='copy'):
        history, raw = self.history,self.raw
        class ModifyingCollector(RawExportCollector):
            calls=0
            def _copy_source(inner,*args,**kwargs):
                if stage=='copy' and inner.calls==0:
                    operation();inner.calls+=1
                return super()._copy_source(*args,**kwargs)
            def _hash_range(inner,*args,**kwargs):
                if stage=='verification':
                    inner.calls+=1
                    if inner.calls==2:operation()
                return super()._hash_range(*args,**kwargs)
        return ModifyingCollector(history,raw)

    def test_append_after_freeze_does_not_change_frozen_bytes_or_partial_tail(self):
        path,_=self.write(body=line(ordinal=1)+b'{"half":')
        before=path.read_bytes()
        def append():
            with path.open('ab') as stream:stream.write(b'"new"}\n')
        result,entries=self.collect(collector=self.modifying_collector(append))
        self.assertEqual(list(entries.values()),[before])
        self.assertEqual(result['manifest']['sources'][0]['frozenUpperBound'],len(before))
        self.assertTrue(result['manifest']['sources'][0]['trailingPartialLine'])

    def test_rewrite_truncate_and_replacement_after_freeze_never_claim_copied(self):
        for change in ('rewrite','truncate','replace'):
            with self.subTest(change=change):
                path,header=self.write(body=line(ordinal=1,payload={'value':'unchanged'}))
                before=path.read_bytes()
                def mutate():
                    if change=='rewrite':path.write_bytes(before.replace(b'unchanged',b'CHANGED!!'))
                    elif change=='truncate':path.write_bytes(header)
                    else:
                        replacement=path.with_suffix('.replacement');replacement.write_bytes(before);replacement.replace(path)
                with self.assertRaises(RawExportError) as context:
                    self.collector=self.modifying_collector(mutate)
                    self.collector.collect(A,self.destination)
                self.assertEqual(context.exception.code,'allSourcesUnreadable')
                self.assertFalse(self.destination.exists())

    def test_source_rewrite_during_copy_verification_is_rejected(self):
        path,_=self.write(body=line(ordinal=1,payload={'value':'unchanged'}))
        before=path.read_bytes()
        collector=self.modifying_collector(lambda:path.write_bytes(before.replace(b'unchanged',b'CHANGED!!')),stage='verification')
        with self.assertRaises(RawExportError) as context:collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'allSourcesUnreadable')

    def test_byte_limit_keeps_exact_partial_prefix_and_manifest_reason(self):
        path,header=self.write(body=line(ordinal=1,payload={'value':'large'*1000}))
        limit=len(header)+13
        collector=RawExportCollector(self.history,self.raw,limits=replace(ExportLimits(),max_bytes=limit,chunk_bytes=17))
        result,entries=self.collect(collector=collector)
        self.assertEqual(list(entries.values()),[path.read_bytes()[:limit]])
        self.assertEqual(result['manifest']['sources'][0]['state'],'partial')
        self.assertEqual(result['manifest']['sources'][0]['reason'],'export-byte-limit')
        self.assertFalse(result['manifest']['enumerationComplete'])

    def test_history_task_and_source_limits_are_explicit(self):
        self.write(A);self.write(B);self.history.items[A]=[activity(B)]
        collector=RawExportCollector(self.history,self.raw,limits=replace(ExportLimits(),max_tasks=1))
        result,entries=self.collect(collector=collector)
        self.assertEqual(len(entries),1)
        self.assertTrue(any('task-enumeration-limit' in value for value in result['manifest']['unresolvedRanges']))

    def test_manager_owned_descriptor_remains_open_and_nonempty_file_is_rejected(self):
        self.write(A)
        descriptor=os.open(self.destination,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        self.addCleanup(os.close,descriptor)
        result,_=self.collect(destination_fd=descriptor)
        self.assertEqual(os.fstat(descriptor).st_size,result['bytes'])
        before=self.destination.read_bytes()
        with self.assertRaises(RawExportError) as context:
            self.collector.collect(A,self.destination,destination_fd=descriptor)
        self.assertEqual(context.exception.code,'invalid-private-destination')
        self.assertEqual(self.destination.read_bytes(),before)

    def test_preexisting_destination_and_symlink_are_never_overwritten(self):
        self.write(A)
        self.destination.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.collector.collect(A,self.destination)
        self.assertEqual(self.destination.read_bytes(),b'keep')
        self.destination.unlink()
        target=self.private/'target';target.write_bytes(b'keep')
        symlink(self, self.destination, target)
        with self.assertRaises(FileExistsError):self.collector.collect(A,self.destination)
        self.assertEqual(target.read_bytes(),b'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX chmod fixture; Windows private DACL checks use native tests')
    def test_private_directory_is_required(self):
        self.write(A);self.private.chmod(0o755)
        with self.assertRaises(RawExportError) as context:self.collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'private-directory-required')

    def test_partial_zip_write_failure_aborts_owned_archive(self):
        self.write(A,body=line(ordinal=1,payload={'data':'large'*100}))
        original=zipfile._ZipWriteFile.write
        writes=[]
        def fail_after_partial(target,data):
            if target._zinfo.filename.startswith('records/'):
                writes.append(original(target,data[:13]))
                raise OSError(errno.ENOSPC,'synthetic-full-disk')
            return original(target,data)
        with mock.patch.object(zipfile._ZipWriteFile,'write',fail_after_partial):
            with self.assertRaises(RawExportError) as context:self.collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'zip-write-failed')
        self.assertEqual(writes,[13])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.private.iterdir()),[])

    def test_spool_disk_failure_aborts_manager_archive_without_closing_descriptor(self):
        self.write(A)
        descriptor=os.open(self.destination,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        self.addCleanup(os.close,descriptor)
        with mock.patch('codex_display.raw_export.os.write',side_effect=OSError(errno.ENOSPC,'synthetic-full-disk')):
            with self.assertRaises(RawExportError) as context:
                self.collector.collect(A,self.destination,destination_fd=descriptor)
        self.assertEqual(context.exception.code,'private-copy-failed')
        self.assertEqual(os.fstat(descriptor).st_ino,self.destination.stat().st_ino)
        self.assertEqual(list(self.private.iterdir()),[self.destination])

    def test_timeout_after_zip_entry_header_is_a_whole_export_failure(self):
        self.write(A)
        original=zipfile.ZipFile.open
        collector=self.collector
        def open_entry(archive,name,*args,**kwargs):
            result=original(archive,name,*args,**kwargs)
            if isinstance(name,zipfile.ZipInfo) and name.filename.startswith('records/'):
                collector._check_time=mock.Mock(side_effect=RawExportError('export-time-limit'))
            return result
        with mock.patch.object(zipfile.ZipFile,'open',open_entry):
            with self.assertRaises(RawExportError) as context:collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'zip-time-limit')
        self.assertFalse(self.destination.exists())

    def test_compressed_own_container_copies_exact_bytes_but_inherited_container_is_excluded(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest('stdlib zstd unavailable')
        path,_=self.write(A)
        payload=path.read_bytes()
        compressed=path.with_name(path.name+'.zst')
        encoded=zstd.compress(payload)
        compressed.write_bytes(encoded)
        path.unlink()
        result,entries=self.collect()
        self.assertEqual(list(entries.values()),[encoded])
        self.assertTrue(next(iter(entries)).endswith('.bin'))
        self.assertFalse(result['manifest']['sources'][0]['trailingPartialLine'])
        self.destination.unlink()
        child,_=self.write(B,base={'thread_id':A,'end_ordinal_exclusive':2,'end_byte_offset':len(payload)})
        result,entries=self.collect(B)
        self.assertEqual(list(entries.values()),[child.read_bytes()])
        inherited=next(source for source in result['manifest']['sources'] if source['logicalInheritedRange'])
        self.assertEqual(inherited['state'],'unsafeInheritedRange')
        self.assertIsNone(inherited['entryName'])
        self.assertFalse(result['manifest']['enumerationComplete'])

    def test_projection_byte_limit_does_not_truncate_own_export(self):
        path,header=self.write(body=line(ordinal=1)+line(ordinal=2,payload={'data':'full-export'}))
        raw=RawAdapter([self.sessions],max_bytes=len(header)+10)
        self.addCleanup(raw.close)
        result,entries=self.collect(collector=RawExportCollector(self.history,raw))
        self.assertEqual(list(entries.values()),[path.read_bytes()])
        self.assertEqual(result['manifest']['sources'][0]['state'],'copied')
        self.assertTrue(any('source-byte-limit' in reason for reason in result['manifest']['unresolvedRanges']))

    def test_inherited_ordinal_conflict_never_exports_unverified_parent_range(self):
        parent,_=self.write(A,body=line(ordinal=1)+line(ordinal=3))
        base=self.base(parent,A,end=4)
        base['end_ordinal_exclusive']=3
        child,_=self.write(B,base=base)
        result,entries=self.collect(B)
        self.assertEqual(list(entries.values()),[child.read_bytes()])
        self.assertTrue(any(source['state']=='unsafeInheritedRange' for source in result['manifest']['sources']))

    def test_history_page_limit_reports_unenumerated_children(self):
        self.write(A);self.write(B)
        ordinary=[{'turnId':'t','item':{'type':'agentMessage','id':str(i)}} for i in range(200)]
        self.history.items[A]=ordinary+[activity(B)]
        collector=RawExportCollector(self.history,self.raw,limits=replace(ExportLimits(),max_history_pages=1))
        result,entries=self.collect(collector=collector)
        self.assertEqual(len(entries),1)
        self.assertNotIn(B,self.history.reads)
        self.assertIn('history-page-limit:'+A,result['manifest']['unresolvedRanges'])

    def test_source_count_limit_reports_uncopied_source(self):
        parent,_=self.write(A)
        self.write(B,base=self.base(parent,A))
        collector=RawExportCollector(self.history,self.raw,limits=replace(ExportLimits(),max_sources=1))
        result,entries=self.collect(B,collector=collector)
        self.assertEqual(len(entries),1)
        unreadable=[source for source in result['manifest']['sources'] if source['state']=='unreadable']
        self.assertEqual(len(unreadable),1)
        self.assertEqual(unreadable[0]['reason'],'export-source-limit')

    def test_copy_time_limit_never_publishes_unverified_result(self):
        self.write(A)
        collector=self.collector
        original=collector._check_time
        def timeout_after_enumeration(deadline):
            if self.history.pages:raise RawExportError('export-time-limit')
            original(deadline)
        with mock.patch.object(collector,'_check_time',timeout_after_enumeration):
            with self.assertRaises(RawExportError) as context:collector.collect(A,self.destination)
        self.assertEqual(context.exception.code,'allSourcesUnreadable')
        self.assertTrue(any('export-time-limit' in reason for reason in context.exception.manifest['unresolvedRanges']))
        self.assertFalse(self.destination.exists())


if __name__=='__main__':unittest.main()
