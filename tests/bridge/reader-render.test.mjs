/** 验证实际 React 输出的安全边界与按需渲染，防止 tokenizer 测试通过但 DOM 隐藏正文。 */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
let server, RichReader, CodeReader, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ RichReader, CodeReader } = await server.ssrLoadModule('/frontend/src/components/reader/RichReader.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });
function render(component, props) {
  return renderToStaticMarkup(React.createElement(PreferencesProvider, null, React.createElement(component, { copy() {}, ...props })));
}
test('actual Markdown output preserves wrappers and blocks scripts, event handlers, image fetches and unsafe links', () => {
  const html = render(RichReader, { text: '# Read me\n\n<system_context>\nImportant instruction.\n</system_context>\n\n<script>alert(1)</script>\n\n<img src="https://invalid.test/x" onerror="alert(1)">\n\n[unsafe](javascript:alert) and [safe](https://example.org/docs).\n\n![remote](https://invalid.test/image.png)\n' });
  assert.match(html, /&lt;system_context&gt;/);
  assert.match(html, /Important instruction/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|<img|<iframe|<style|<[^>]+\sonerror=/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /href="https:\/\/example.org\/docs"/);
  assert.match(html, /href="https:\/\/invalid.test\/image.png"/);
});
test('actual reader lays out tables, code and headings and identifies visual folds', () => {
  const source = '# Overview\n\nintro\n\n## Details\n\n| A | B |\n| --- | --- |\n| one | two |\n\n```bash\n  echo hello\n```\n';
  const html = render(RichReader, { text: source });
  assert.match(html, /<h1>/); assert.match(html, /<h2>/); assert.match(html, /<table>/); assert.match(html, /<th>.*A/);
  assert.match(html, /hljs-built_in/); assert.match(html, /aria-label="Section navigation"/);
  const folded = render(RichReader, { text: source, state: { collapsed: { 'section-0': true } } });
  assert.match(folded, /aria-expanded="false"/);
  assert.match(folded, /loaded and collapsed/);
  assert.doesNotMatch(folded, /<table>|echo hello/);
});
test('object-property language labels render fenced and direct code without losing surrounding text', () => {
  const source = 'const html = "<tag> & value";';
  for (const language of ['constructor', '__proto__', 'CONSTRUCTOR extra', '__PROTO__']) {
    for (const markdown of [
      `Before fence\n\n\`\`\`${language}\n${source}\n\`\`\`\n\nAfter fence`,
      `Before fence\n\n> ~~~${language}\n> ${source}\n> ~~~\n\nAfter fence`,
    ]) {
      const html = render(RichReader, {text:markdown});
      assert.match(html, /Before fence/);
      assert.match(html, /After fence/);
      assert.match(html, /const html = &quot;&lt;tag&gt; &amp; value&quot;;/);
      assert.doesNotMatch(html, /class="hljs-|<tag>/);
    }
    const html = render(CodeReader, {text:source,language});
    assert.match(html, /const html = &quot;&lt;tag&gt; &amp; value&quot;;/);
    assert.doesNotMatch(html, /class="hljs-|<tag>/);
  }
});
test('large code, table and lists render bounded initial DOM with explicit deferred counts', () => {
  const code = render(CodeReader, { text: Array.from({ length: 10000 }, (_, i) => `line-${i}`).join('\n'), language: 'text', lineNumbers: true });
  assert.equal([...code.matchAll(/class="cx-reader-code-line"/g)].length, 200);
  assert.match(code, /10000 loaded lines; 200 shown/);
  const table = render(RichReader, { text: '| A |\n| --- |\n' + '| value |\n'.repeat(5000) });
  assert.equal([...table.matchAll(/<td>/g)].length, 100);
  assert.match(table, /100\/5000/);
  const list = render(RichReader, { text: '- entry\n'.repeat(5000) });
  assert.equal([...list.matchAll(/<li>/g)].length, 100);
  assert.match(list, /100\/5000/);
});
test('tool output frontmatter is a visible code block rather than a setext heading', () => {
  const text = 'Output (saved fragment):\n\n---\r\nname: research\r\ndescription: "<script>alert(1)</script>"\r\n---\r\n# Research\r\n\r\n## Workflow\r\n\r\nRead source documents.\r\n';
  const html = render(RichReader, { text });
  assert.match(html, /data-reader-frontmatter/);
  assert.match(html, /YAML frontmatter source/);
  assert.match(html, /hljs-attr[^>]*>name/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script|<h2[^>]*>name/);
  assert.match(html, /<h1>/); assert.match(html, /Research/); assert.match(html, /Workflow/);
  assert.match(html, /Read source documents/);
});
test('code renderer detects missing languages but plain logs and explicit text remain unhighlighted', () => {
  for (const [text, language] of [['const answer = 42;\nconsole.log(answer);', 'javascript'], ['def run(value):\n    return value + 1', 'python'], ['printf "%s\\n" "$value"', 'bash']]) {
    const html = render(CodeReader, { text });
    assert.match(html, new RegExp(`${language} · detected`));
    assert.match(html, /class="hljs-/);
  }
  const plain = render(CodeReader, { text: '[INFO] Reading input\n[WARN] Need retry' });
  assert.doesNotMatch(plain, /class="hljs-/);
  const explicit = render(CodeReader, { text: 'const answer = 42;', language: 'text' });
  assert.match(explicit, /text · plain/);
  assert.doesNotMatch(explicit, /class="hljs-/);
  const fenced = render(RichReader, { text: '```\nconst answer = 42;\n```\n' });
  assert.match(fenced, /javascript · detected/);
});
