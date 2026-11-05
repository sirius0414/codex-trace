import test from 'node:test';
import assert from 'node:assert/strict';
import { codeTokenText, detectCodeLanguage, frontmatterEnd, highlightedLines, knownLanguage, markdownDocument, safeReaderUrl } from '../../frontend/src/components/reader/reader-model.ts';

const fixture = '# Context\n\nRead **carefully**, `code` and [source](https://example.org).\n\n> Quote\n\n- First\n- Second\n\n| Key | Value |\n| --- | --- |\n| input | text |\n\n## Commands\n\n```bash\n  printf "%s\\n" "<unsafe>"\n\techo done\n```\n\n<system_context>\nKeep this wrapper and all inner text.\n</system_context>\n\n## Finish\n\nDone.\n';
test('Markdown retains headings, tables, links, lists, quotes and literal context wrappers', () => {
  const document = markdownDocument(fixture);
  assert.equal(document.blocks.map(block => block.token.raw).join(''), fixture);
  assert.deepEqual(document.headings.map(block => block.token.text), ['Context', 'Commands', 'Finish']);
  for (const type of ['table', 'code', 'html', 'list', 'blockquote', 'paragraph']) assert.ok(document.blocks.some(block => block.token.type === type), type);
  const html = document.blocks.find(block => block.token.type === 'html');
  assert.match(html.token.raw, /<system_context>\nKeep this wrapper and all inner text.\n<\/system_context>/);
  assert.equal(document.headings[0].sectionEnd, document.blocks.length);
  assert.equal(document.blocks[document.headings[1].sectionEnd].token.text, 'Finish');
});
test('Markdown append preserves existing heading identities and section boundaries remain hierarchical', () => {
  const before = markdownDocument('# A\n\nbody\n\n## B\n\nchild\n');
  const after = markdownDocument('# A\n\nbody\n\n## B\n\nchild\n\n# C\n\nnext');
  assert.deepEqual(after.headings.slice(0, 2).map(block => block.id), before.headings.map(block => block.id));
  assert.equal(after.blocks[after.headings[0].sectionEnd].token.text, 'C');
});
test('fenced code copying preserves original tabs, spaces, line endings and excludes fences', () => {
  const body = '  printf "%s\\n" "<unsafe>"\n\techo done\n';
  assert.equal(codeTokenText(markdownDocument(fixture).blocks.find(block => block.token.type === 'code').token), body);
  for (const source of ['```bash\r\n  echo hello\r\n```', '~~~python\n\tprint("x")\n~~~', ' ```sh\n  echo x\n ```']) {
    const token = markdownDocument(source).blocks.find(block => block.token.type === 'code').token;
    assert.equal(codeTokenText(token), source.slice(source.indexOf('\n') + 1, source.lastIndexOf('\n') + 1));
  }
  assert.equal(codeTokenText(markdownDocument('```sh\n  still loading').blocks[0].token), '  still loading');
});
test('syntax highlighting never changes source characters or accepts raw HTML nodes', () => {
  const source = '\tconst html = "<script>alert(1)</script> & \\"word\\"";\n// </span><img src=x>\n';
  const lines = highlightedLines(source, 'typescript');
  assert.equal(lines.map(line => line.map(piece => piece.text).join('')).join('\n'), source);
  assert.ok(lines.flat().some(piece => piece.className.includes('hljs')));
  assert.equal(knownLanguage('Bash'), 'bash');
  assert.equal(knownLanguage('not-a-language'), undefined);
  assert.equal(highlightedLines(source, 'not-a-language').flat().map(piece => piece.text).join('\n'), source);
});
test('inherited object names are unknown explicit languages and preserve plain source', () => {
  const source = '\tconst html = "<tag> & value";\r\nconsole.log(html);\n';
  const labels = [...Object.getOwnPropertyNames(Object.prototype), ' CONSTRUCTOR extra ', '\t__PROTO__\nextra'];
  for (const label of labels) {
    assert.equal(knownLanguage(label), undefined, label);
    assert.deepEqual(detectCodeLanguage(source, label), {language:undefined,basis:'explicit'}, label);
    assert.deepEqual(highlightedLines(source, label), source.split('\n').map(text => [{text,className:''}]), label);
  }
});
test('language normalization preserves custom and native highlighter aliases', () => {
  for (const [label, language, source] of [
    [' JS extra ', 'javascript', 'const answer = 42;'],
    ['shell', 'bash', 'echo "$value"'],
    ['tsx', 'typescript', 'const answer: number = 42;'],
    ['JSONC', 'jsonc', '{"answer":42}'],
    ['golang', 'golang', 'func main() {}'],
  ]) {
    assert.equal(knownLanguage(label), language, label);
    assert.ok(highlightedLines(source, label).flat().some(piece => piece.className), label);
  }
});
test('links only allow explicit safe protocols and code highlighting is bounded', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,<script>', 'file:///private/test', '//example.org', 'https:\n//x']) assert.equal(safeReaderUrl(url), undefined, url);
  for (const url of ['https://example.org/schema#field', 'http://example.org', 'mailto:team@example.org']) assert.equal(safeReaderUrl(url), url);
  const large = 'const x = 10;\n'.repeat(10_000);
  assert.ok(highlightedLines(large, 'js').flat().every(piece => !piece.className));
  const document = markdownDocument(('paragraph\n\n').repeat(60_000));
  assert.ok(document.remainder.length > 0);
  assert.equal(document.blocks.map(block => block.token.raw).join('') + document.remainder, ('paragraph\n\n').repeat(60_000));
});
test('CRLF and mixed line endings retain source offsets and exact nested fenced code', () => {
  const source = '# Title\r\n\r\n```bash\r\n  echo one\n\techo two\r\n```\n\n## End\r\ntext';
  const document = markdownDocument(source);
  assert.equal(document.blocks.map(block => block.token.raw).join(''), source);
  assert.equal(document.headings[1].offset, source.indexOf('## End'));
  assert.equal(codeTokenText(document.blocks.find(block => block.token.type === 'code').token), '  echo one\n\techo two\r\n');
  const quoted = markdownDocument('> ```sh\r\n> echo one\n> echo two\r\n> ```');
  assert.equal(codeTokenText(quoted.blocks[0].token.tokens[0]), 'echo one\necho two\r\n');
});
test('reference link definitions remain visible and never shift following raw ranges', () => {
  const text = '[ref]: https://example.org/schema "source"\r\n\r\n# Documentation\r\n\r\nRead [ref].\r\n\r\n```bash\r\n  echo retained\r\n```';
  const document = markdownDocument(text);
  assert.equal(document.blocks.map(block => block.token.raw).join(''), text);
  assert.equal(document.blocks[0].token.type, 'def');
  assert.equal(document.headings[0].offset, text.indexOf('# Documentation'));
  const paragraph = document.blocks.find(block => block.token.type === 'paragraph');
  assert.equal(paragraph.token.tokens.find(token => token.type === 'link').href, 'https://example.org/schema');
  assert.equal(codeTokenText(document.blocks.find(block => block.token.type === 'code').token), '  echo retained\r\n');
});
test('language detection accepts strong JS, Python and Bash syntax while respecting explicit hints', () => {
  const examples = [
    ['javascript', 'const result = await fetch("/api");\nconsole.log(result);'],
    ['javascript', 'text(await tools.exec_command({cmd:"cat /synthetic/SKILL.md",max_output_tokens:6000}));'],
    ['javascript', '// @exec: {"yield_time_ms": 1000}\ntext(\n  await tools.exec_command({cmd: "pwd"})\n);'],
    ['javascript', 'image(await tools.render({path: "/synthetic/image.png"}));'],
    ['javascript', 'function greet(name) {\n  return `Hello ${name}`;\n}'],
    ['typescript', 'interface Result { ok: boolean; }\nconst value: Result = {ok: true};'],
    ['python', '# Process a name\ndef greet(name):\n    return name.upper()\n'],
    ['python', 'import json\nprint(json.dumps({"ok": True}))'],
    ['bash', '#!/usr/bin/env bash\nset -eu\n  printf "%s\\n" "$value"\n'],
    ['bash', 'cat ./SKILL.md\n'],
    ['bash', 'npm run build', false],
  ];
  for (const [language, source, hasTokens = true] of examples) {
    const detected = detectCodeLanguage(source);
    assert.equal(detected.language, language, source);
    const lines = highlightedLines(source, detected.language);
    if (hasTokens) assert.ok(lines.flat().some(piece => piece.className), source);
    assert.equal(lines.map(line => line.map(piece => piece.text).join('')).join('\n'), source);
  }
  assert.deepEqual(detectCodeLanguage('const x = 1;', 'python'), {language:'python',basis:'explicit'});
  assert.deepEqual(detectCodeLanguage('const x = 1;', 'text'), {language:undefined,basis:'explicit'});
  assert.deepEqual(detectCodeLanguage('const x = 1;', 'unknown-language'), {language:undefined,basis:'explicit'});
});
test('plain prose, logs, ambiguous snippets and code beyond the bounded sample stay plain', () => {
  const examples = [
    'The function accepts an object and returns a string.',
    'Ready\nRequest completed successfully.\n3 files changed.',
    '[INFO] Processing input\n[ERROR] const value = broken();\n[INFO] Retrying',
    '2026-11-04 10:20:00 INFO value=1;\n2026-11-04 10:20:01 ERROR foo() failed;',
    'Traceback (most recent call last):\n  File "main.py", line 7\n    raise ValueError("missing")',
    'Use text(await tools.exec_command(...)) to read the result.',
    '[INFO] text(await tools.exec_command({cmd:"pwd"}));',
    'text is a helper; await tools completion before continuing.',
    'echo hello',
    'git status is the command to inspect changes.',
    'ordinary text\n'.repeat(1000) + 'const result = call();',
  ];
  for (const source of examples) assert.equal(detectCodeLanguage(source).language, undefined, source.slice(0, 100));
});
test('frontmatter remains an exact separate block including CRLF and untrusted values', () => {
  const frontmatter = '---\r\nname: research\ndescription: "<script>alert(1)</script>"\r\n---\r\n';
  const source = frontmatter + '# Research\r\n\r\nRead primary sources.\r\n\r\n## Workflow\r\n';
  const document = markdownDocument(source);
  assert.equal(frontmatterEnd(source), frontmatter.length);
  assert.equal(document.blocks[0].token.type, 'frontmatter');
  assert.equal(document.blocks[0].token.raw, frontmatter);
  assert.deepEqual(document.headings.map(block => block.token.text), ['Research', 'Workflow']);
  assert.equal(document.headings[0].offset, frontmatter.length);
  assert.equal(document.blocks.map(block => block.token.raw).join(''), source);
  assert.equal(frontmatterEnd('---\nA prose paragraph\n---\n# Title'), 0);
  assert.equal(frontmatterEnd('---\nname: loading'), 0);
  const prefixed = 'Output (saved fragment):\n\n' + source;
  const wrapped = markdownDocument(prefixed);
  assert.equal(wrapped.blocks.find(block => block.token.type === 'frontmatter').token.raw, frontmatter);
  assert.equal(wrapped.blocks.map(block => block.token.raw).join(''), prefixed);
});
