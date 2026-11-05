# Preview Reader Acceptance Checks

[English](README.md) | [简体中文](README.zh-CN.md)

This directory mounts the production `TraceUI` / `Inspector` with synthetic data. It does not connect to real history, execute tools, or access working files.

## Reading behavior

- Summary retains the event overview; Preview reads loaded input, output, and errors; Source text retains the original text of the current block; Raw content shows the associated raw record; Source shows provenance evidence.
- Body format is detected automatically from content and source metadata. Reading / Source text and a single copy action share a row with the section title; format and code-language overrides are in More. Font size, wrapping, line numbers, width, and expanded reading continue to use the existing settings.
- JSON uses an index that preserves source ranges and inserts indentation only, without parsing into objects and serializing again. Numeric lexemes, duplicate fields, escaped keys, and string escapes are preserved; ordinary objects and arrays are expanded directly. The field tree, depth control, field directory, field search, and node-copy actions have been removed.
- `json-layout.ts` centralizes the long-value threshold: a value is automatically collapsed when its raw JSON exceeds **600 UTF-16 code units or 12 lines**. Objects display `{…}`, arrays display `[…]`, and strings retain their first 70 code units. The root value is never automatically collapsed. No new user setting is introduced.
- Plain-text results can be read directly when the source is confirmed as `mcpToolCall.result` and both field definitions and actual structure support this interpretation. Unknown fields, duplicate keys, non-text blocks, error results, and `structuredContent` remain fully visible as JSON; wrappers are not inferred from field names.
- Entry-level search covers all loaded input/output/error source text, including visually collapsed text and text outside the displayed window. It lists at most 100 results while showing the actual total hit count. JSON hits expand the corresponding long value and move the display window; Markdown and other formats, or hits spanning structural JSON whitespace, use source text for precise positioning.
- Each block has one copy action that copies the complete loaded original body, independently of folding, format, or display window. Synthetic tests intercept Clipboard API arguments to verify fidelity without changing the user's clipboard.
- Ordinary, complete, and unknown source states do not occupy a notice row. Reliable source truncation, saved fragments, not-yet-loaded content, and unrecorded output remain visible. Only known fixed explanation text is removed; actual errors and limitations are retained.

## State and resource boundaries

- Each backend read returns at most 65,536 Unicode characters (previously 16,384); UTF-8 byte offsets and continuation cursors remain contiguous.

- Reading state is retained only within the page session, for at most 150 entries and 600 tab positions; bodies and queries are not persisted. JSON objects retain state by field name and occurrence order for duplicate names, while arrays use indices. Array insertion or reordering has no reliable identity mapping.
- A single effective body is expanded by default. When both Input and Output exist, Call expands Input and Result expands Output. Failed entries initially expand every body, with Error first. Asynchronous body arrival, pagination, entry changes, and refreshes preserve manual choices.
- JSON parsing is limited to 8,000,000 UTF-16 code units, 200,000 nodes, and 128 levels. Incomplete, invalid, or oversized input falls back to source text, while the enclosing view retains the reading entry point.
- JSON displays at most 400 layout lines per window, stopping at a line boundary within a budget of approximately 200,000 characters. A single long-value window contains at most 100,000 characters. Explicit navigation connects previous and next windows. Search goes directly to the target window without mounting all preceding data.
- Plain source text uses 100,000-character windows; search can jump directly to the end. Copy includes the complete loaded body.
- Markdown, code, tables, and lists continue to use their own bounded displays and reading state.

## Validation

```sh
npm run build
npm test
git diff --check
node tests/fixtures/reader-preview/build.mjs
python3 -m http.server 4175 --bind 127.0.0.1 --directory build/reader-preview
```

Open `http://127.0.0.1:4175` in the same ego-browser task workspace, then run:

```js
const { runAcceptance, runNarrowAcceptance } = await import('file:///ABSOLUTE_REPO/tests/fixtures/reader-preview/acceptance.mjs');
console.log(await runAcceptance(page, '/ABSOLUTE_REPO/build/preview-unified-acceptance'));
console.log(await runNarrowAcceptance(page, '/ABSOLUTE_REPO/build/preview-unified-acceptance'));
```

Regressions for default folding, pagination, and live refresh:

```js
const { runCollapseAcceptance } = await import('file:///ABSOLUTE_REPO/tests/fixtures/reader-preview/collapse.mjs');
console.log(await runCollapseAcceptance(page, '/ABSOLUTE_REPO/build/preview-collapse-acceptance'));
```

The narrow-screen flow calibrates the browser's existing zoom and checks English and Chinese, the classic / soft / terminal styles, and light and dark themes at 390 CSS pixels. Screenshots and check results are saved to the specified build directory.
