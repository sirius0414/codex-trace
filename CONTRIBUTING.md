# Coding conventions and review guide

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

This repository uses a Python backend, a React/TypeScript frontend, and JSON Schema contracts. Keep each change focused on one purpose. Avoid combining behavior changes, bulk formatting, and comment additions in a single change.

## Comments

- When adding or changing a complex module, explain its responsibilities, upstream and downstream interactions, and main data boundaries at the module entry point. Use a module docstring in Python and a short module comment in TypeScript/JavaScript.
- For non-obvious public interfaces, state reducers, and operations with side effects, explain input constraints, output semantics, and failure/recovery behavior. Use Python docstrings and preferably JSDoc in TypeScript/JavaScript. Do not repeat parameter information already expressed by types.
- Explain design choices and invariants beside critical branches, especially invalidation of asynchronous responses, identity checks, versions and cursors, incremental merging, insufficient evidence, file publication, and idempotent retries. Reviewers should find the reasoning close to the code.
- Prefer Chinese for new explanations of application behavior; retain the original spelling of protocol fields, state names, and API names. Existing accurate English comments may remain.
- Comments explain why the code works this way and where its boundaries lie; they do not translate code line by line. Simple value access, straightforward rendering, and ordinary loops do not need filler comments. Complex branches still need explanations beyond function names.
- Update relevant comments when changing behavior. Describe only what the current code guarantees; do not present assumptions, fallback behavior, or unverified host behavior as guarantees.
- `contracts/types.ts` is generated. Change contract shapes in JSON Schema, maintain generation rules in `contracts/generate_types.py`, and run the generator to check consistency.

## Key invariants

During review, check that the implementation and nearby comments establish these invariants:

- Task data at the production entry point travels through app-only MCP `_meta`; `content` and `structuredContent` do not carry task bodies. `server.py` remains an M0 diagnostic entry point, not the production data channel.
- Asynchronous responses for a selected task must match the complete `Binding`. Matching the task ID alone cannot replace matching the source, session, and selection epoch. Before selection, open/list responses validate their own page session and request generation. Generation, revision, and cursors each have their own scope.
- Recorded content, execution state, and reading state are handled separately. Insufficient evidence remains unknown. Usage snapshots are not added directly, and communication relationships do not automatically become parent-child relationships.
- Reads of raw sources validate identity, source root, and inherited scope. Export scope is independent of UI filters, folding, and pagination. Collection from multiple sources must not be described as an atomic snapshot.
- A save timeout retains the original request identifier so its result can be checked. Only `verified` means that the saved bytes were read back and checked; `unknown` must not start another save with a new request. Releasing temporary resources does not cancel an accepted save or delete final files.

## Review entry points

| Area | Main entry points |
| --- | --- |
| MCP registration, host identity, and private results | `backend/codex_trace/app.py`, `service.py` |
| History/raw sources, evidence projection, and shared observation | `backend/codex_trace/adapters/`, `domain/`, `observation.py` |
| Frozen exports, persistent receipts, and local publication | `backend/codex_trace/raw_export.py`, `exports.py`, `export_store.py` |
| Frontend request scheduling, state merging, and recovery | `frontend/src/main.tsx`, `bridge/`, `state/` |
| List grouping, overview, and reading anchors | `frontend/src/components/model.ts`, `EventList.tsx`, `Overview.tsx` |
| Builds, offline packaging, updates, and isolated verification | `vite.config.ts`, `scripts/` |
| Request/response contracts | `contracts/README.md`, `trajectory.schema.json`, `tools.json` |

## Documentation languages

Public project documentation has an English version at the conventional filename and a Simplified Chinese version ending in `.zh-CN.md`. Update both versions together when changing instructions or policies, including language links, commands, examples, and support limitations. This requirement applies to documentation; source comments do not need duplicate translations. Keep upstream license texts unchanged.

## Verification

Run checks appropriate to the change; run the complete checks when a change spans multiple layers. Use the installed dependencies; see the [README](README.md#build-and-verification) for environment setup.

```sh
npm run build
npm test
PYTHONPATH=backend:tests .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python contracts/generate_types.py --check
git diff --check
```

For comment changes, also confirm that the comments match the actual branches and that no execution logic changes are mixed in. Tests must use synthetic tasks and temporary directories. Follow the separate README procedures for actual plugin updates and desktop entry-point acceptance checks.
