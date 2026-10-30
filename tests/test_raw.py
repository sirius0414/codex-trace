import hashlib
import json
from codex_trace.platform import filesystem as os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_trace.adapters.raw import RawAdapter, RawError
from platform_fixtures import symlink

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
C = '33333333-3333-4333-8333-333333333333'
D = '44444444-4444-4444-8444-444444444444'


def line(kind='response_item', payload=None, ordinal=None, ending=b'\n'):
    result = {'type': kind, 'payload': payload if payload is not None else {'type': 'message', 'role': 'user', 'content': []}}
    if ordinal is not None:
        result['ordinal'] = ordinal
    return json.dumps(result, ensure_ascii=False).encode() + ending


class RawTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.sessions = self.root / 'sessions'
        self.archived = self.root / 'archived_sessions'
        self.sessions.mkdir()
        self.archived.mkdir()
        self.adapter = RawAdapter([self.sessions, self.archived])
        self.addCleanup(self.adapter.close)

    def write(self, identifier=A, *, rollout=None, base=None, body=None, mode='paginated', extra=None, root=None):
        rollout = rollout or identifier
        name = 'rollout-2026-10-22T12-00-00-' + identifier + ('_' + rollout if rollout != identifier else '') + '.jsonl'
        path = (root or self.sessions) / name
        payload = {'id': identifier, 'history_mode': mode, 'cli_version': 'synthetic'}
        if base is not None:
            payload['history_base'] = base
        payload.update(extra or {})
        initial = base.get('end_ordinal_exclusive', 0) if isinstance(base, dict) else 0
        header = line('session_meta', payload, initial if mode == 'paginated' else None)
        if body is None:
            body = line(ordinal=initial + 1)
        path.write_bytes(header + body)
        return path, header

    def read(self, path, identifier=A, adapter=None):
        return (adapter or self.adapter).read(identifier, {'id': identifier, 'path': str(path)})

    def base(self, path, identifier, end=2):
        rows = path.read_bytes().splitlines(keepends=True)
        bound = sum(len(row) for row in rows if json.loads(row)['ordinal'] < end)
        return {'thread_id': identifier, 'end_ordinal_exclusive': end, 'end_byte_offset': bound}

    def test_complete_known_records_have_exact_refs_and_no_writes(self):
        path, _ = self.write(body=line(ordinal=1, ending=b'\r\n'))
        before = path.read_bytes()
        modified = path.stat().st_mtime_ns
        snapshot = self.read(path)
        self.assertEqual(len(snapshot.records), 2)
        self.assertEqual(snapshot.issues, [])
        self.assertEqual(snapshot.sources[0]['state'], 'readable')
        self.assertEqual(snapshot.segments[0]['byteEnd'], len(before))
        for record in snapshot.records:
            source = record['source']
            self.assertEqual(record['raw'], before[source['byteStart']:source['byteEnd']])
        self.assertTrue(snapshot.records[-1]['raw'].endswith(b'\r\n'))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertEqual(snapshot.fingerprint, self.read(path).fingerprint)

    def test_unknown_bad_utf8_bad_json_and_partial_tail_are_not_records(self):
        body = line('future_type', {'private': 'unknown'}, 1) + b'\xffbad\n' + b'{bad\n' + line(ordinal=4) + b'{"half":"\xe4\xbd'
        path, header = self.write(body=body)
        snapshot = self.read(path)
        self.assertEqual([r['ordinal'] for r in snapshot.records], [0, 4])
        self.assertEqual(snapshot.segments[0]['byteEnd'], len(header + body))
        self.assertEqual(snapshot.segments[0]['completeThrough'], len(header + body) - 11)
        self.assertEqual(snapshot.segments[0]['partialTailBytes'], 11)
        self.assertIn('malformed-record', snapshot.issues)
        self.assertIn('incomplete-tail', snapshot.issues)
        self.assertNotIn('unknown', json.dumps(snapshot.sources))

    def test_complete_lines_only_use_lf_not_bare_cr_as_boundaries(self):
        path, _ = self.write(body=b'bad\rrecord\n' + line(ordinal=2))
        snapshot = self.read(path)
        self.assertEqual([r['ordinal'] for r in snapshot.records], [0, 2])
        self.assertEqual(snapshot.records[1]['raw'], line(ordinal=2))

    def test_inherited_parent_prefix_excludes_parent_followups(self):
        parent, _ = self.write(A, body=line(ordinal=1) + line(ordinal=2, payload={'secret': 'PARENT_AFTER_FORK'}))
        child, _ = self.write(B, base=self.base(parent, A, 2))
        snapshot = self.read(child, B)
        self.assertEqual([r['ordinal'] for r in snapshot.records], [0, 1, 2, 3])
        self.assertNotIn(b'PARENT_AFTER_FORK', b''.join(r['raw'] for r in snapshot.records))
        inherited = snapshot.segments[0]
        self.assertLess(inherited['byteEnd'], parent.stat().st_size)
        self.assertEqual(inherited['inheritedRange'], {'start': 0, 'endExclusive': 2})
        self.assertEqual([r['inherited'] for r in snapshot.records], [True, True, False, False])

    def test_multilevel_chain_is_ordered_and_does_not_follow_fork_id_without_base(self):
        parent, _ = self.write(A)
        middle, _ = self.write(B, base=self.base(parent, A))
        child, _ = self.write(C, base=self.base(middle, B, 4))
        snapshot = self.read(child, C)
        self.assertEqual([r['ordinal'] for r in snapshot.records], list(range(6)))
        self.assertEqual([s['threadId'] for s in snapshot.segments], [A, B, C])
        standalone, _ = self.write(D, extra={'forked_from_id': A})
        self.assertEqual(len(self.read(standalone, D).segments), 1)

    def test_reverted_rollout_id_differs_from_header_owner(self):
        parent, _ = self.write(A, rollout=D)
        child, _ = self.write(B, base=self.base(parent, D))
        snapshot = self.read(child, B)
        self.assertEqual(snapshot.issues, [])
        self.assertEqual(snapshot.segments[0]['threadId'], A)
        self.assertEqual(snapshot.segments[0]['rolloutId'], D)

    def test_archived_parent_and_selected_archive_move_keep_segment_identity(self):
        parent, _ = self.write(A, root=self.archived)
        child, _ = self.write(B, base=self.base(parent, A))
        first = self.read(child, B)
        child.rename(self.archived / child.name)
        second = self.read(child, B)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual([s['segmentId'] for s in first.segments], [s['segmentId'] for s in second.segments])
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_missing_parent_does_not_hide_current_valid_source(self):
        child, _ = self.write(B, base={'thread_id': A, 'end_ordinal_exclusive': 2, 'end_byte_offset': 200})
        snapshot = self.read(child, B)
        self.assertIn('source-missing', snapshot.issues)
        self.assertEqual(len(snapshot.records), 2)
        self.assertTrue(any(s['state'] == 'missing' for s in snapshot.sources))

    def test_unsupported_base_keeps_current_source_without_guessing(self):
        child, _ = self.write(B, base={'unknown': 'PRIVATE_DO_NOT_REPORT'})
        snapshot = self.read(child, B)
        self.assertIn('unsupported-history-base', snapshot.issues)
        self.assertEqual(len(snapshot.records), 2)
        self.assertEqual(len(snapshot.sources), 1)
        self.assertNotIn('PRIVATE_DO_NOT_REPORT', json.dumps(snapshot.sources))

    def test_cycle_is_bounded_and_valid_selected_segment_survives(self):
        path, header = self.write(A, base={'thread_id': A, 'end_ordinal_exclusive': 1, 'end_byte_offset': 10_000})
        # A self pointer cannot prove any prefix: neither arbitrary parent bytes
        # nor repeated current records may be projected.
        snapshot = self.read(path)
        self.assertEqual(len(snapshot.records), 2)
        self.assertIn("inheritance-cycle", snapshot.issues)
        self.assertEqual(len(snapshot.segments), 1)

    def test_cutoff_inside_line_or_after_file_never_exposes_parent(self):
        parent, _ = self.write(A)
        base = self.base(parent, A)
        for bound in (base['end_byte_offset'] - 1, base['end_byte_offset'] + 10_000):
            child, _ = self.write(B, base={**base, 'end_byte_offset': bound})
            snapshot = self.read(child, B)
            self.assertEqual({r['source']['threadId'] for r in snapshot.records}, {B})
            self.assertIn('inheritance-byte-boundary-invalid', snapshot.issues)

    def test_cutoff_ordinal_conflict_disables_entire_inherited_export_range(self):
        parent, _ = self.write(A, body=line(ordinal=1) + line(ordinal=2))
        base = self.base(parent, A, 3)
        child, _ = self.write(B, base={**base, 'end_ordinal_exclusive': 2})
        snapshot = self.read(child, B)
        self.assertEqual({r['source']['threadId'] for r in snapshot.records}, {B})
        self.assertFalse(snapshot.segments[0]['exportable'])
        self.assertIn('inheritance-ordinal-boundary-invalid', snapshot.issues)

    def test_subagent_inherited_model_context_is_excluded_from_self_projection(self):
        parent, _ = self.write(A)
        child, _ = self.write(B, base=self.base(parent, A), extra={'subagent_history_start_ordinal': 3})
        snapshot = self.read(child, B)
        self.assertEqual([r['projectionExcluded'] for r in snapshot.records], [True, True, True, False])

    def test_append_retains_generation_rewrite_truncate_replace_increment(self):
        path, header = self.write()
        original = self.read(path)
        with path.open('ab') as stream:
            stream.write(line(ordinal=2))
        append = self.read(path)
        self.assertEqual(original.generation, append.generation)
        self.assertNotEqual(original.fingerprint, append.fingerprint)
        current = path.read_bytes()
        path.write_bytes(current.replace(b'"role": "user"', b'"role": "tool"'))
        rewrite = self.read(path)
        self.assertGreater(rewrite.generation, append.generation)
        path.write_bytes(header + line(ordinal=1))
        truncated = self.read(path)
        self.assertGreater(truncated.generation, rewrite.generation)
        replacement = path.with_suffix('.temp')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        self.assertGreater(self.read(path).generation, truncated.generation)

    def test_reading_different_inherited_bounds_does_not_invalidate_physical_generation(self):
        parent, _ = self.write(A, body=line(ordinal=1) + line(ordinal=2))
        own = self.read(parent)
        child, _ = self.write(B, base=self.base(parent, A))
        inherited = self.read(child, B)
        self.assertEqual(own.segments[0]['generation'], inherited.segments[0]['generation'])
        self.assertEqual(self.read(parent).segments[0]['generation'], own.segments[0]['generation'])

    def test_legacy_without_ordinals_uses_physical_order(self):
        path, _ = self.write(mode='legacy', body=line() + line('future_type') + line())
        self.assertEqual([r['ordinal'] for r in self.read(path).records], [0, 1, 3])

    def test_identity_filename_and_path_validation(self):
        path, _ = self.write(A)
        self.assertIn('source-identity-mismatch', self.read(path, B).issues)
        outside = self.root / path.name
        outside.write_bytes(path.read_bytes())
        self.assertIn('outside-session-roots', self.read(outside).issues)
        other = self.sessions / path.name.replace(A, B)
        other.write_bytes(path.read_bytes())
        self.assertIn('filename-owner-mismatch', self.read(other).issues)
        with self.assertRaises(RawError):
            self.adapter.read('bad-id', {})
        with self.assertRaises(RawError):
            self.adapter.read(A, {'id': B, 'path': str(path)})

    def test_file_and_directory_symlinks_rejected_without_reads(self):
        target, _ = self.write(A)
        linked_file = self.sessions / 'link.jsonl'
        symlink(self, linked_file, target)
        self.assertIn('symlink-or-noncanonical-source', self.read(linked_file).issues)
        linked = self.sessions / 'linked'
        symlink(self, linked, self.sessions, target_is_directory=True)
        self.assertIn('symlink-or-noncanonical-source', self.read(linked / target.name).issues)
        self.assertIn('not-regular-source', self.read(self.sessions).issues)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO fixture; Windows special handles have separate native tests')
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.sessions / 'fifo'
        os.mkfifo(fifo)
        self.assertIn('not-regular-source', self.read(fifo).issues)

    def test_duplicate_source_locations_are_ambiguous_not_latest(self):
        parent, _ = self.write(A)
        (self.archived / parent.name).write_bytes(parent.read_bytes())
        child, _ = self.write(B, base=self.base(parent, A))
        snapshot = self.read(child, B)
        self.assertIn('ambiguous-source', snapshot.issues)
        self.assertEqual({r['source']['threadId'] for r in snapshot.records}, {B})

    def test_resources_are_bounded_but_source_range_is_preserved(self):
        path, header = self.write(body=line(ordinal=1) + line(ordinal=2))
        adapter = RawAdapter([self.sessions], max_bytes=len(header) + 10)
        snapshot = self.read(path, adapter=adapter)
        self.assertEqual(len(snapshot.records), 1)
        self.assertIn('source-byte-limit', snapshot.issues)
        self.assertEqual(snapshot.segments[0]['byteEnd'], path.stat().st_size)
        self.assertEqual(snapshot.segments[0]['partialTailBytes'], 0)
        limited = self.read(path, adapter=RawAdapter([self.sessions], max_records=1))
        self.assertEqual(len(limited.records), 1)
        self.assertIn('record-count-limit', limited.issues)

    def test_compressed_selected_container_identity_is_checked_but_not_projected(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest('stdlib zstd unavailable')
        path, _ = self.write(A)
        payload = path.read_bytes()
        compressed = path.with_name(path.name + '.zst')
        compressed.write_bytes(zstd.compress(payload))
        path.unlink()
        snapshot = self.read(path)
        self.assertEqual(snapshot.records, [])
        self.assertEqual(snapshot.sources[0]['format'], 'compressed')
        self.assertEqual(snapshot.sources[0]['state'], 'unsupported')
        self.assertTrue(snapshot.segments[0]['exportable'])
        self.assertEqual(snapshot.segments[0]['byteEnd'], compressed.stat().st_size)
        self.assertFalse(path.exists())
        child, _ = self.write(B, base={'thread_id': A, 'end_ordinal_exclusive': 2, 'end_byte_offset': len(payload)})
        inherited = self.read(child, B)
        self.assertFalse(inherited.segments[0]['exportable'])
        self.assertIn('compressed-inherited-range-unavailable', inherited.issues)

    def test_unchanged_bytes_ignore_mtime_only_change(self):
        path, _ = self.write()
        before = self.read(path)
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        after = self.read(path)
        self.assertEqual(before.generation, after.generation)
        self.assertEqual(before.fingerprint, after.fingerprint)

    def test_unknown_history_mode_is_covered_but_not_projected(self):
        path, _ = self.write(mode='future-layout')
        snapshot = self.read(path)
        self.assertEqual(snapshot.records, [])
        self.assertIn('unsupported-history-mode', snapshot.issues)
        self.assertTrue(snapshot.segments[0]['exportable'])

    def test_parent_swap_after_canonical_check_is_not_followed(self):
        folder = self.sessions / 'nested'
        folder.mkdir()
        path, _ = self.write(root=folder)
        outside = self.root / 'outside'
        outside.mkdir()
        self.write(root=outside)
        original = Path.resolve
        switched = False

        def swap(current, *args, **kwargs):
            nonlocal switched
            resolved = original(current, *args, **kwargs)
            if current == path and not switched:
                switched = True
                folder.rename(self.sessions / 'saved-folder')
                symlink(self, folder, outside, target_is_directory=True)
            return resolved
        with mock.patch.object(Path, 'resolve', swap):
            snapshot = self.read(path)
        self.assertEqual(snapshot.records, [])
        self.assertTrue(snapshot.issues)

    def test_source_rewrite_during_read_does_not_emit_mixed_records(self):
        path, _ = self.write()
        original = os.pread
        changed = False

        def rewrite(descriptor, count, offset):
            nonlocal changed
            value = original(descriptor, count, offset)
            if not changed:
                changed = True
                path.write_bytes(path.read_bytes().replace(b'"role": "user"', b'"role": "tool"'))
            return value
        with mock.patch('codex_trace.adapters.raw.os.pread', rewrite):
            snapshot = self.read(path)
        self.assertEqual(snapshot.records, [])
        self.assertIn('source-changed-during-read', snapshot.issues)

    def test_stable_polls_do_no_body_io_or_decoding(self):
        path, _ = self.write(body=b"".join(line(ordinal=i) for i in range(1, 101)))
        first = self.read(path)
        metrics = dict(self.adapter.metrics)
        for _ in range(4):
            current = self.read(path)
            self.assertEqual(first.fingerprint, current.fingerprint)
            self.assertEqual(first.records, current.records)
        for counter in ("readBytes", "verifiedBytes", "parsedBytes", "parsedLines", "rebuilds"):
            self.assertEqual(metrics[counter], self.adapter.metrics[counter], counter)
        self.assertEqual(self.adapter.metrics["cacheHits"] - metrics["cacheHits"], 4)

    def test_append_decodes_only_new_complete_records_without_duplicates(self):
        path, _ = self.write(body=b"".join(line(ordinal=i) for i in range(1, 101)))
        first = self.read(path)
        metrics = dict(self.adapter.metrics)
        added = line(ordinal=101) + line("future_type", {}, 102) + b"bad\n" + line(ordinal=104)
        with path.open("ab") as stream:
            stream.write(added)
        result = self.read(path)
        self.assertEqual(result.generation, first.generation)
        self.assertEqual([r["ordinal"] for r in result.records], list(range(101)) + [101, 104])
        self.assertEqual(self.adapter.metrics["parsedLines"] - metrics["parsedLines"], 4)
        self.assertEqual(self.adapter.metrics["parsedBytes"] - metrics["parsedBytes"], len(added))
        self.assertEqual(self.adapter.metrics["readBytes"] - metrics["readBytes"], len(added))
        self.assertGreater(self.adapter.metrics["verifiedBytes"], metrics["verifiedBytes"])
        again = self.read(path)
        self.assertEqual(again.records, result.records)
        self.assertEqual(first.records[-1]["ordinal"], 100)
        self.assertIn("malformed-record", result.issues)

    def test_half_utf8_and_half_json_complete_in_place(self):
        path, _ = self.write()
        first = self.read(path)
        added = line(ordinal=2, payload={"type": "message", "text": "中文"})
        split = added.index("中".encode()) + 1
        metrics = dict(self.adapter.metrics)
        with path.open("ab") as stream:
            stream.write(added[:split])
        partial = self.read(path)
        self.assertEqual(len(partial.records), len(first.records))
        self.assertEqual(self.adapter.metrics["parsedBytes"], metrics["parsedBytes"])
        self.assertIn("incomplete-tail", partial.issues)
        with path.open("ab") as stream:
            stream.write(added[split:])
        complete = self.read(path)
        self.assertEqual(complete.records[-1]["record"]["payload"]["text"], "中文")
        self.assertEqual(complete.generation, first.generation)
        self.assertNotIn("incomplete-tail", complete.issues)
        self.assertEqual(self.adapter.metrics["parsedBytes"] - metrics["parsedBytes"], len(added))
        self.assertEqual(self.adapter.metrics["parsedLines"] - metrics["parsedLines"], 1)

    def test_rewrite_with_restored_mtime_is_detected_via_ctime(self):
        path, _ = self.write()
        first = self.read(path)
        info = path.stat()
        path.write_bytes(path.read_bytes().replace(b'"role": "user"', b'"role": "tool"'))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(path.stat().st_ctime_ns, info.st_ctime_ns)
        changed = self.read(path)
        self.assertGreater(changed.generation, first.generation)
        self.assertEqual(changed.records[-1]["record"]["payload"]["role"], "tool")

    def test_rewrite_and_growth_does_not_masquerade_as_append(self):
        path, _ = self.write()
        first = self.read(path)
        before = dict(self.adapter.metrics)
        path.write_bytes(path.read_bytes().replace(b'"role": "user"', b'"role": "tool"') + line(ordinal=2))
        changed = self.read(path)
        self.assertGreater(changed.generation, first.generation)
        self.assertGreater(self.adapter.metrics["rebuilds"], before["rebuilds"])
        self.assertEqual(changed.records[1]["record"]["payload"]["role"], "tool")

    def test_inherited_append_outside_fork_range_is_not_read_or_projected(self):
        parent, _ = self.write(A)
        child, _ = self.write(B, base=self.base(parent, A))
        first = self.read(child, B)
        metrics = dict(self.adapter.metrics)
        with parent.open("ab") as stream:
            stream.write(line(ordinal=2, payload={"private": "PARENT_LATER_CONTENT"}))
        later = self.read(child, B)
        self.assertEqual(first.records, later.records)
        self.assertEqual(first.generation, later.generation)
        self.assertEqual(first.fingerprint, later.fingerprint)
        self.assertEqual(self.adapter.metrics["parsedBytes"], metrics["parsedBytes"])
        self.assertEqual(self.adapter.metrics["readBytes"], metrics["readBytes"])
        self.assertNotIn(b"PARENT_LATER_CONTENT", b"".join(r["raw"] for r in later.records))

    def test_inherited_rewrite_invalidates_even_after_alternate_range_reads(self):
        parent, _ = self.write(A, body=line(ordinal=1) + line(ordinal=2))
        child, _ = self.write(B, base=self.base(parent, A))
        own = self.read(parent)
        self.read(child, B)
        data = parent.read_bytes()
        last = data.rfind(b'"role": "user"')
        parent.write_bytes(data[:last] + data[last:].replace(b'"role": "user"', b'"role": "tool"'))
        self.read(child, B)
        changed = self.read(parent)
        self.assertGreater(changed.generation, own.generation)
        self.assertEqual(changed.records[-1]["record"]["payload"]["role"], "tool")

    def test_changed_chain_and_cutoff_never_reuse_stale_parent_rows(self):
        parent, _ = self.write(A, body=line(ordinal=1) + line(ordinal=2))
        child, _ = self.write(B, base=self.base(parent, A, 2))
        first = self.read(child, B)
        self.write(B, base=self.base(parent, A, 3))
        second = self.read(child, B)
        self.assertGreater(second.generation, first.generation)
        self.assertEqual([r["ordinal"] for r in second.records], [0, 1, 2, 3, 4])
        bad_base = self.base(parent, A, 3)
        self.write(B, base={**bad_base, "end_ordinal_exclusive": 2})
        conflicted = self.read(child, B)
        self.assertFalse(conflicted.segments[0]["exportable"])
        self.assertEqual({r["source"]["threadId"] for r in conflicted.records}, {B})

    def test_10k_under_20mb_fixture_warm_poll_and_one_append_are_incremental(self):
        text = "x" * 1800
        body = b"".join(line(ordinal=i, payload={"type": "message", "text": text}) for i in range(1, 10001))
        path, _ = self.write(body=body)
        self.assertLess(path.stat().st_size, 20 * 1024 * 1024)
        first = self.read(path)
        self.assertEqual(len(first.records), 10001)
        before = dict(self.adapter.metrics)
        self.read(path)
        self.assertEqual(self.adapter.metrics["readBytes"], before["readBytes"])
        self.assertEqual(self.adapter.metrics["verifiedBytes"], before["verifiedBytes"])
        added = line(ordinal=10001)
        with path.open("ab") as stream:
            stream.write(added)
        second = self.read(path)
        self.assertEqual(len(second.records), 10002)
        self.assertEqual(self.adapter.metrics["readBytes"] - before["readBytes"], len(added))
        self.assertEqual(self.adapter.metrics["parsedBytes"] - before["parsedBytes"], len(added))
        self.assertEqual(self.adapter.metrics["parsedLines"] - before["parsedLines"], 1)
        self.assertEqual(second.generation, first.generation)

    def test_cached_record_limit_is_explicit_and_does_not_reparse_old_lines(self):
        path, _ = self.write()
        limited = RawAdapter([self.sessions], max_records=2)
        self.addCleanup(limited.close)
        first = self.read(path, adapter=limited)
        old = dict(limited.metrics)
        with path.open("ab") as stream:
            stream.write(line(ordinal=2))
        second = self.read(path, adapter=limited)
        self.assertEqual(second.records, first.records)
        self.assertIn("record-count-limit", second.issues)
        self.assertEqual(limited.metrics["parsedLines"] - old["parsedLines"], 1)
        self.assertEqual(second.segments[0]["byteEnd"], path.stat().st_size)

    def test_extreme_bad_line_count_is_bounded_without_mislabeling_tail(self):
        path, header = self.write(body=b"bad\n" * 10010)
        limited = RawAdapter([self.sessions], max_records=2)
        self.addCleanup(limited.close)
        snapshot = self.read(path, adapter=limited)
        self.assertIn("line-count-limit", snapshot.issues)
        self.assertNotIn("incomplete-tail", snapshot.issues)
        self.assertEqual(limited.metrics["parsedLines"], 10000)
        self.assertEqual(snapshot.segments[0]["byteEnd"], path.stat().st_size)
        self.assertEqual(snapshot.segments[0]["partialTailBytes"], 0)
        self.assertTrue(snapshot.segments[0]["exportable"])
        before = dict(limited.metrics)
        self.read(path, adapter=limited)
        self.assertEqual(limited.metrics["parsedLines"], before["parsedLines"])

    def test_sources_and_refs_match_contract(self):
        import jsonschema
        schema = json.loads((Path(__file__).resolve().parents[1] / 'contracts/trajectory.schema.json').read_text())
        path, _ = self.write()
        snapshot = self.read(path)
        for definition, items in [('SourceCoverage', snapshot.sources), ('SourceRef', [r['source'] for r in snapshot.records])]:
            validator = jsonschema.Draft202012Validator({'$ref': '#/$defs/' + definition, '$defs': schema['$defs']})
            for item in items:
                validator.validate(item)


if __name__ == '__main__':
    unittest.main()
