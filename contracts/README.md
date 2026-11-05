# Local payload contracts

[English](README.md) | [简体中文](README.zh-CN.md)

`trajectory.schema.json` is the authoritative JSON Schema (Draft 2020-12), version
`1.0.0`. Its default root validates `ThreadSnapshot`; the reusable definitions
also cover the other service payloads listed in `tools.json`. This is a proposed
application contract, not evidence that the host integration or M1 is accepted.
The M0 result determines the MCP envelope. Trajectory content and export bytes
belong only in app-visible metadata, never model-visible result fields.

Generate the frontend types with `python3 contracts/generate_types.py`. Run
`python3 contracts/generate_types.py --check` in CI to catch schema/type drift.
TypeScript cannot express every JSON Schema constraint (for example integer
bounds); payloads still require runtime validation at the service boundary.

## Contract choices

- Every selected-task response carries `Binding`: schema, source instance,
  observation session, selection epoch and thread ID. A matching thread ID alone
  is insufficient to accept a response. List/open responses precede selection.
- Missing measurements and identifiers use explicit `null`, never zero or an
  invented identifier. A recorded zero usage value is distinct from unavailable
  usage. Each snapshot includes the availability of each metric.
- The event kind enum has no unknown placeholder. Unsupported source records
  remain in the raw source catalogue and download, outside UI projections.
- Event revision and snapshot generation/revision are separate. Event sources
  use opaque segment IDs or API identities, with no frontend-supplied file path.
- Execution, reading and follow states are separate enums. Status evidence names
  its exact scope. Output completeness is independent of execution state.
- A content part distinguishes source truncation from UI chunking. Missing raw
  association is explicit in `EventDetail`, and is not filled from adjacent lines.
- `UsageSnapshot` retains ownership, scope, epoch, ordering, aggregation mode,
  cache semantics and inherited/child attribution. It does not imply a model
  request count, and the contract does not sum snapshots.
- `Turn.usage` and `TraceEvent.usage` optionally carry scoped snapshots (older
  payloads may omit them). A `modelUsage` event represents one explicit response
  identity, independent of tool/message entries. Compaction checkpoints can
  restore turn snapshots but never create new request events.
- `Turn.executionIntervals` optionally preserves recorded execution envelopes as
  `Timing[]`. Raw start/complete/abort boundaries keep resumed episodes separate;
  API `startedAt`/`completedAt` values are Unix seconds and only fill missing outer
  boundaries. An aborted episode cannot be extended to a later API completion.
  An open start retains `endMs: null`; missing starts are never calculated from a
  duration or the current clock. Without usable evidence the field is omitted.
  These intervals identify active execution windows, not measured model/tool
  phases, and do not change lifecycle status or task wall-clock statistics.
  API turn timings use `source: null` with their field names in `basis`, since an
  API turn has no item identity representable by the existing `SourceRef`.
- Group relationships retain their type and evidence. Communication and fork
  relations exist independently of the child hierarchy.
- Downloads have one discriminated request (`create/status/read/release/commit/result`) with
  no UI filters or list cursor. Each export has a frozen manifest, source ranges,
  checksums and collection times; `atomicSnapshot` is always false. Chunk cursors
  are opaque and each byte chunk has its own offset, size and checksum.
- View state contains only preferences and identifiers. Text payloads, source
  paths and authentication material cannot be added as undeclared properties.
- `ViewState.turnRange` optionally preserves a record highlight range as an
  absolute wall-clock `TimeRange`; omission or `null` clears it. The UI matches
  each record's actual timestamp or recorded interval to highlight only records
  intersecting the range. Untimed and unmatched records stay visible without a
  range highlight; turn/group headers indicate contained matches. It never
  filters or reorders the list; existing Agent and event filters independently
  control visibility. An explicit completed range selection reveals its first
  visible match through the existing reading anchor and expands only the loaded
  target path when needed, preserving detail selection. Preview, cancellation,
  clearing, viewport navigation and restoring a saved range do not request a jump.
  Positive intervals use half-open overlap; point records include the left edge
  and exclude an internal right edge, with the final axis endpoint included.
  This field is outside `Filters`: it does not change loaded-text query requests
  or search cache keys. Restoring preferences preserves the original wall-clock
  positions rather than reinterpreting them on a newly compressed active axis.
- `ViewState.timelineSpacing` remains accepted for compatibility with saved views.
  The Turn-segmented sequence overview ignores this old time-spacing preference.
  Diagram positions are layout units, not elapsed time, and are never persisted
  as `TimeRange`. Existing absolute `turnRange` values remain original-time
  highlights; the turn list can still reveal their records.

## Local ZIP delivery

The authorized destination is the current OS user's `~/Downloads/Codex Trace/`.
The service chooses the ZIP filename and resolves the destination; requests never
accept a path, directory, filename or overwrite flag. `LocalExportPath` is an
output-only absolute path. Existing POSIX paths remain valid. On Windows the
receipt uses a native local-drive path, for example
`C:\测试 用户\Downloads\Codex Trace\codex-trace-a.zip`. UNC shares,
drive-relative paths, device namespaces, alternate data streams, DOS device
names and components ending in a dot or space are excluded. Network filesystem
locking and atomic publication are outside this local export contract.
Runtime checks must enforce the exact resolved home
directory, reject symlink and reparse-point escapes, and avoid overwriting existing files; the
schema alone cannot establish filesystem containment. No upload or HTTP endpoint
is part of this contract.

`download_records` remains the single interface and the UI keeps one export
entry. The operation sequence is:

1. `create(binding, requestId)` freezes the source membership and ranges and
   returns an export handle. Repeating the key returns the same export rather
   than collecting a different snapshot. A lost create response is recovered by
   repeating the same request, never by allocating a new key.
2. `status(binding, exportId)` reports collection progress. `ready` means the
   frozen ZIP is prepared, not saved. `read(binding, exportId, cursor)` retains
   bounded, repeatable chunk access; local saving does not require routing all
   ZIP bytes through the UI.
3. `commit(binding, exportId, requestId)` requests a local save of that frozen
   ZIP. This key is distinct from the create key. A commit on an unready export
   fails with `exportNotReady`. The service records the operation before a final
   ZIP can be published and reconciles interrupted commits against their owned
   destination before attempting another write.
4. `result(binding, requestId)` queries the commit after a delayed, lost or timed
   out response, including when the caller lacks an export handle. It does not
   initiate a save. Repeating `commit` with the same key is also idempotent.
5. `release(binding, exportId)` disposes of temporary snapshot/chunk resources.
   It cannot delete an already saved ZIP or forget its commit receipt. An accepted
   commit retains its required temporary resources until it resolves, even if the
   UI releases the handle or changes selection; release is not proof of cancellation.

`requestId` binds an operation type, source instance, root thread and, for commit,
the frozen export. Reuse with a different operation or export returns
`idempotencyConflict` without another write. View session/selection epoch are
response routing guards, not permission to create another export for the same
key. Recovery under a new view binding requires authorization for that same
source and task; every response carries the requesting view's binding so stale
responses are still rejected. Receipt persistence and reconciliation must survive
MCP process restart. Expired or missing receipts must return `unknown`, not silently
perform a new commit under the same key. Receipt/tombstone cleanup is separate
from temporary export expiry; no automatic cleanup removes user-saved files.

`DownloadResult.save` describes delivery independently of snapshot `status`:

| Save state | Meaning |
| --- | --- |
| `notRequested` | This known export has no accepted commit; no request key is attached. |
| `pending` | The commit is accepted or being reconciled; do not create a replacement request. |
| `verified` | The final ZIP was written and read back at `verifiedAt`; receipt includes server filename, absolute path, byte count and SHA-256. |
| `failed` | A definitive failure with no final file published by this request; a transport timeout is never this state. |
| `unknown` | The outcome cannot be established, including a missing request record; a file may already exist. |

Only `save.status == "verified"` permits a saved-success message. Its path basename,
filename, byte count and hash must agree with the actual saved ZIP and frozen
snapshot. This receipt records verification at a point in time, not a promise
that the user cannot subsequently move or delete the file. A partial source
manifest may still produce a verified ZIP: save success never means all original
sources were readable. Unknown bytes, bad UTF-8, malformed lines and trailing
partial records remain byte-for-byte unchanged in the archive.

## Validation boundaries

Schema validation proves shape, enum membership and local value constraints.
Service/domain tests must additionally verify identity authorization, response
epoch matching, revision monotonicity, byte-range ordering, timestamp ordering,
status-evidence scope, explicit parentage, cycle rejection, usage aggregation,
and source provenance. They are not implied by passing these contract tests.
Local export service tests must also prove durable idempotency, atomic publication
without overwrites, restart/timeout reconciliation, final-file readback hashes,
receipt authorization and preservation across release. The contract fixtures do
not create files or prove those runtime properties.

`tests/fixtures/contracts/` contains entirely synthetic examples. The fixture
catalogue says which definition each example validates against. Scenario inputs
cover delayed cross-task responses, repeated/declining usage, saved output
updates and communication-versus-parentage. They are test specifications for
later implementation, not claims that those reducers already exist.

## Preview reading metadata

`ContentRef.reader` and `ContentChunk.reader` are optional, backward-compatible
`ReaderMetadata`. They describe one body part, independently of its paging,
`sourceTruncated`, and `OutputCompleteness`. A JSON format hint is not a claim
that the currently loaded chunk parses or contains every field. The service
returns the same metadata on each chunk without interpreting partial JSON.

The projection supplies format evidence for commandExecution, fileChange,
structured MCP/dynamic-tool values, and legacy function arguments. Messages
use a Markdown *reading convention*, identified as such in `origin`; this is
not a source declaration that every message is Markdown. Command argument
arrays remain JSON arrays. A known command without recorded shell information
does not acquire a guessed Bash language. Unknown tools receive structure
hints only, with no invented parameter descriptions. Confirmed call/result
identity remains the sole mechanism for sharing a tool identity with a result.

`FieldDescription` uses JSON Pointer paths and separates `scope: protocol`
from `scope: tool`. Descriptions can carry a type, unit, required flag, enum,
source URL/version, and a `versionMatch` of `exact` or `reference`. Optional
`format`/`language` hints apply only at the described path. Description text
and links are data, never executable content. Missing definitions are omitted.
Metadata is bundled and returned with the body; hovering never fetches a schema.

Current external-reference coverage (reviewed 2026-11-03):

- `exec_command` and `functions.exec_command`: ten input parameters and six
  output fields; `write_stdin`/`functions.write_stdin`: four input parameters
  and those output fields, from the official
  [shell tool definitions](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs).
- Known MCP result envelopes: `content`, `isError`, `structuredContent` and
  verified text-content blocks, from
  [MCP 2025-06-18 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
  These descriptions never describe the tool's arbitrary input arguments or
  the business fields within `structuredContent`.
- Known fileChange entries: their recorded `diff` body, from the
  [app-server schema](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartResponse.json).
  Update diffs and added/deleted file contents retain different format hints.

The historical sources read by this repository do not currently preserve tool
schemas or a negotiated MCP protocol version. All external definitions above
therefore have `versionMatch: reference`, including when a producer version
happens to be present. A producer version alone is not proof of a tool schema
match. Requiredness and enum values describe the reference, not validation of
historical invocations. Unknown fields remain readable without an explanation.

For raw tool fields, Preview takes the original JSON value substring from the
associated physical record. String values decode only their JSON string
wrapper; object/array/numeric values retain numeric lexemes, key order,
whitespace and duplicate keys. Raw remains the entire associated record.
API values arrive already decoded by the API client; `origin` explicitly says
`API decoded projection`. Their transport-byte spelling cannot be recovered
from that API projection; associated Raw records remain the evidence where
available. Reader metadata never upgrades unknown source completeness or
infers truncation from log wording.
