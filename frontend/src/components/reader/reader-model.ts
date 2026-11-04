/** Markdown 只产生阅读节点，原文始终由上层保存；代码高亮结果被拆成文本与样式，不能注入 HTML。 */
import { lexer, Tokenizer, type Token, type Tokens } from 'marked';
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import diff from 'highlight.js/lib/languages/diff';
import css from 'highlight.js/lib/languages/css';
import xml from 'highlight.js/lib/languages/xml';
import sql from 'highlight.js/lib/languages/sql';
import yaml from 'highlight.js/lib/languages/yaml';
import rust from 'highlight.js/lib/languages/rust';
import go from 'highlight.js/lib/languages/go';
import swift from 'highlight.js/lib/languages/swift';

for (const [name, definition] of Object.entries({ bash, javascript, typescript, python, json, diff, css, xml, sql, yaml, rust, go, swift })) hljs.registerLanguage(name, definition);
const aliases: Record<string, string> = { sh: 'bash', shell: 'bash', zsh: 'bash', js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript', py: 'python', html: 'xml', yml: 'yaml', patch: 'diff', rs: 'rust' };
export function knownLanguage(value?: string): string | undefined {
  const first = value?.trim().split(/\s+/)[0].toLowerCase();
  const language = first ? aliases[first] ?? first : undefined;
  return language && hljs.getLanguage(language) ? language : undefined;
}

export type CodeLanguage = { language?: string; basis: 'explicit' | 'syntax' | 'heuristic' | 'plain' };
const codeLanguageCache = new Map<string, CodeLanguage>();
const AUTO_SAMPLE = 12_000;
const AUTO_LANGUAGES = ['javascript', 'python', 'bash', 'sql', 'css', 'rust', 'go', 'swift'];

/** 先看来源/围栏声明，再看正文语法。自动判断只看有界样本，普通句子和日志不参加语言竞赛。
 * highlight.js relevance 不是概率；只在有代码形状时接受明显领先的候选。显式未知语言不被偷偷替换。
 */
export function detectCodeLanguage(text: string, explicitLanguage?: string): CodeLanguage {
  if (explicitLanguage?.trim()) return { language: knownLanguage(explicitLanguage), basis: 'explicit' };
  const sample = text.slice(0, AUTO_SAMPLE).replace(/\r\n?/g, '\n');
  const cached = codeLanguageCache.get(sample);
  if (cached) return cached;
  let result: CodeLanguage = { basis: 'plain' };
  const lines = sample.split('\n').filter(line => line.trim()).slice(0, 160);
  const logged = lines.filter(line => /^\s*(?:\[?(?:\d{4}-\d{2}-\d{2}[T ]|\d{2}:\d{2}:\d{2})|\[?(?:INFO|WARN(?:ING)?|ERROR|DEBUG|TRACE|FATAL)\b|Traceback \(most recent call last\)|File ".+", line \d+)/i.test(line)).length;
  const syntax = (language: string) => { result = { language, basis: 'syntax' }; };
  if (lines.length && !(logged >= 2 || logged && logged >= lines.length / 2)) {
    if (/^#![^\n]*\b(?:bash|zsh|sh)\b/.test(sample)) syntax('bash');
    else if (/^#![^\n]*\bpython[\d.]*\b/.test(sample)) syntax('python');
    else if (/^\s*(?:(?:export|declare)\s+)?(?:interface\s+\w+(?:\s+extends\s+[\w, ]+)?\s*\{|type\s+\w+(?:<[^>]+>)?\s*=)/m.test(sample)
      || /^\s*(?:export\s+)?(?:const|let)\s+[$\w]+\s*:\s*[\w<\[\]| ,]+\s*=/m.test(sample)) syntax('typescript');
    else if (/^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*[$\w]*\s*\([^\n]*\)\s*\{/m.test(sample)
      || /^\s*(?:export\s+)?(?:const|let|var)\s+[$\w{\[][^\n]*=\s*(?:[^\n]*=>|[^\n]*[;({\[])/m.test(sample)
      || /^\s*(?:console\.(?:log|error|warn|info|debug)\(|(?:module\.exports|exports\.\w+)\s*=)/m.test(sample)
      || /^\s*import\s+(?:[\w*{}, ]+)\s+from\s+['"][^'"\n]+['"];?\s*$/m.test(sample)) syntax('javascript');
    else if (/^\s*(?:async\s+)?def\s+\w+\([^\n]*\)\s*(?:->[^\n:]+)?\s*:\s*(?:#.*)?$/m.test(sample)
      || /^\s*class\s+\w+(?:\([^\n]*\))?\s*:\s*(?:#.*)?$/m.test(sample)
      || /^\s*print\((?:f?['"])[^\n]+\)\s*$/m.test(sample)
      || /^\s*(?:from\s+[\w.]+\s+import\s+|import\s+[\w.]+\s*$)/m.test(sample) && /^\s*(?:print\(|(?:for|if|with|while)\b[^\n]*:|\w+\s*=)/m.test(sample)) syntax('python');
    else if (/^\s*(?:SELECT\b[^\n]*\bFROM\b|CREATE\s+TABLE\b|INSERT\s+INTO\b)/im.test(sample) && /[;*()]|\n\s*(?:WHERE|JOIN|ORDER BY)\b/i.test(sample)) syntax('sql');
    else if (/^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+\s*\([^\n]*\)[^\n]*\{/m.test(sample)) syntax('rust');
    else if (/^\s*func\s+(?:\([^\n]+\)\s*)?\w+\s*\([^\n]*\)[^\n]*\{/m.test(sample)) syntax(/^\s*func\s+\w+\s*\([^\n]*\w\s*:/m.test(sample) ? 'swift' : 'go');
    else {
      const commands = lines.filter(line => /^\s*(?:(?:export\s+)?[A-Z_][A-Z0-9_]*=\S+\s+)*(?:cd|pwd|ls|cat|printf|echo|npm|npx|pnpm|yarn|python[23]?|pip[23]?|git|rg|grep|sed|awk|curl|find|mkdir|cp|mv|rm|chmod|touch|tar|docker|cargo|go)\b/.test(line));
      const shellShape = commands.some(line => /['"$]|(?:^|\s)--?[\w-]+|[./\\][\w.-]+|\s(?:&&|\|\||\|)\s/.test(line)
        || /^\s*(?:npm|pnpm|yarn)\s+(?:run\s+[\w:-]+|test|install|build)(?:\s+--?[\w=-]+)*\s*$|^\s*git\s+(?:status|diff|log|show)(?:\s+--?[\w=-]+)*\s*$/.test(line));
      if (commands.length && (shellShape || commands.length >= 2 && commands.length >= lines.length / 2)
        || /^\s*(?:if\s+\[.+\];?\s*then|for\s+\w+\s+in\b[^\n]*;\s*do|set\s+-[a-z]+)\s*$/m.test(sample)) syntax('bash');
      else {
        const codeLines = lines.filter(line => /(?:[;{}]|\w\s*=(?!=)|=>)/.test(line)).length;
        if (lines.length > 1 && codeLines >= 2 && codeLines >= lines.length / 2 && /[({\[]/.test(sample)) {
          try {
            const detected = hljs.highlightAuto(sample, AUTO_LANGUAGES);
            const runnerUp = detected.secondBest?.relevance ?? 0;
            if (detected.language && detected.relevance >= 6 && detected.relevance - runnerUp >= 3 && detected.relevance >= runnerUp * 1.35) {
              result = { language: detected.language, basis: 'heuristic' };
            }
          } catch { /* 语法库无法确认时保留原文，不把内容判为错误。 */ }
        }
      }
    }
  }
  codeLanguageCache.set(sample, result);
  if (codeLanguageCache.size > 64) codeLanguageCache.delete(codeLanguageCache.keys().next().value!);
  return result;
}

/** 只把文首有闭合分隔符及 YAML 字段形状的区段视为 frontmatter；不解析/改写字段值。 */
export function frontmatterEnd(text: string): number {
  const opening = /^(?:\uFEFF)?---[ \t]*(?:\r\n|\r|\n)/.exec(text);
  if (!opening) return 0;
  const rest = text.slice(opening[0].length, 32_768);
  const closing = /^(?:---|\.\.\.)[ \t]*(?:\r\n|\r|\n|$)/m.exec(rest);
  if (!closing || !/^[ \t]*[\w.-]+:[ \t]*(?:\S|$)/m.test(rest.slice(0, closing.index))) return 0;
  return opening[0].length + closing.index + closing[0].length;
}

/** 仅允许显式安全协议；不把来源相对路径解释成本地应用 URL。 */
export function safeReaderUrl(value: string): string | undefined {
  if (/^(https?:|mailto:)/i.test(value) && !/[\u0000-\u0020\u007f]/.test(value)) return value;
  return undefined;
}
export function decodeHighlightText(value: string): string {
  return value.replace(/&(amp|lt|gt|quot|#x27|#39);/g, (_, entity: string) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#x27': "'", '#39': "'" }[entity] ?? _));
}
export type CodePiece = { text: string; className: string };
/** 高亮器的 HTML 仅作为词法数据读取；渲染端不会使用 innerHTML。未知语言保留普通代码。 */
export function highlightedLines(text: string, language?: string): CodePiece[][] {
  const actual = knownLanguage(language);
  if (!actual || text.length > 100_000) return text.split('\n').map(line => [{ text: line, className: '' }]);
  try {
    const output = hljs.highlight(text, { language: actual, ignoreIllegals: true }).value;
    const lines: CodePiece[][] = [[]], classes: string[] = [];
    for (const match of output.matchAll(/<span class="([\w -]+)">|(<\/span>)|([^<]+)/g)) {
      if (match[1] !== undefined) classes.push(match[1]);
      else if (match[2]) classes.pop();
      else {
        decodeHighlightText(match[3]).split('\n').forEach((part, index) => {
          if (index) lines.push([]);
          if (part) lines[lines.length - 1].push({ text: part, className: classes.join(' ') });
        });
      }
    }
    // 词法依赖升级后也不能静默改动正文；不一致就退回无高亮代码。
    if (lines.map(line => line.map(piece => piece.text).join('')).join('\n') !== text) throw new Error('highlight changed text');
    return lines;
  } catch { return text.split('\n').map(line => [{ text: line, className: '' }]); }
}
export type MarkdownBlock = { id: string; token: Token; offset: number; end: number; sectionEnd: number };
export type MarkdownDocument = { blocks: MarkdownBlock[]; headings: (MarkdownBlock & { token: Tokens.Heading })[]; remainder: string };
/** 用原文偏移作为章节身份；增量追加不会重置已有折叠。巨量正文分段呈现且明确保留尾部。 */
export function markdownDocument(text: string): MarkdownDocument {
  const limit = 500_000;
  const boundary = text.length > limit ? Math.max(text.lastIndexOf('\n\n', limit), limit - 10_000) : text.length;
  const prefix = text.slice(0, boundary);
  // marked 将 CRLF/CR 归一为 LF。显式位置映射将原始范围还原，代码复制和搜索锚点不能沿用归一后的偏移。
  const positions: number[] = [0];
  let normalized = '';
  for (let index = 0; index < prefix.length; index++) {
    if (prefix[index] === '\r') { normalized += '\n'; if (prefix[index + 1] === '\n') index++; }
    else normalized += prefix[index];
    positions.push(index + 1);
  }
  function restoreNested(tokens: Token[] | undefined, original: string) {
    const codes: Token[] = [];
    function visit(values: Token[] | undefined) {
      for (const token of values ?? []) {
        if (token.type === 'code') codes.push(token);
        else {
          if ('tokens' in token) visit(token.tokens);
          if (token.type === 'list') for (const item of token.items) visit(item.tokens);
        }
      }
    }
    visit(tokens);
    if (!codes.length) return;
    // 每个外层块只建立一次行映射；大型列表不能对每个项目重新扫描整块来源。
    const sourceLines = [...original.matchAll(/([^\r\n]*)(\r\n|\r|\n|$)/g)].filter(match => match[0]);
    let nextLine = 0;
    for (const token of codes) {
      const rawLines = token.raw.split('\n');
      if (rawLines.at(-1) === '') rawLines.pop();
      // 列表/引用只移除 Markdown 包装前缀，按行对应原始 EOL，混合换行也不经过 stringify。
      for (let start = nextLine; start + rawLines.length <= sourceLines.length; start++) {
        if (!rawLines.every((line, i) => sourceLines[start + i][1].endsWith(line))) continue;
        token.raw = rawLines.map((line, i) => line + (i < rawLines.length - 1 || token.raw.endsWith('\n') ? sourceLines[start + i][2] : '')).join('');
        nextLine = start + rawLines.length;
        break;
      }
    }
  }
  const tokenizer = new Tokenizer();
  const metadataEnd = frontmatterEnd(normalized);
  let offset = 0;
  const tokens: Token[] = [
    ...(metadataEnd ? [{ type: 'frontmatter', raw: normalized.slice(0, metadataEnd) }] : []),
    ...lexer(normalized.slice(metadataEnd), { gfm: true, tokenizer, extensions: {
    renderers: {}, childTokens: {}, block: [function (source) {
      // 命令输出可在文件正文前加读取/截断提示。块边界处同样保留闭合 YAML 区段，不能把它吞成 setext 标题。
      const yamlEnd = frontmatterEnd(source);
      if (yamlEnd) return { type: 'frontmatter', raw: source.slice(0, yamlEnd) };
      // 默认 lexer 会吞掉引用链接定义；保留该节点与链接表，既不隐藏正文，也不破坏后续位置。
      const definition = tokenizer.def(source);
      if (!definition) return undefined;
      this.lexer.tokens.links[definition.tag] ??= { href: definition.href, title: definition.title };
      return { ...definition };
    }],
  } })];
  const blocks: MarkdownBlock[] = tokens.map(token => {
    const normalizedStart = offset; offset += token.raw.length;
    const start = positions[normalizedStart], end = positions[offset];
    token.raw = prefix.slice(start, end);
    if (token.type !== 'code') restoreNested([token], token.raw);
    return { id: `section-${start}`, token, offset: start, end, sectionEnd: 0 };
  });
  blocks.forEach((block, index) => {
    let end = index + 1;
    if (block.token.type === 'heading') {
      while (end < blocks.length && !(blocks[end].token.type === 'heading' && (blocks[end].token as Tokens.Heading).depth <= block.token.depth)) end++;
    }
    block.sectionEnd = end;
  });
  return { blocks, headings: blocks.filter((block): block is MarkdownBlock & { token: Tokens.Heading } => block.token.type === 'heading'), remainder: text.slice(boundary) };
}
/** fenced code 的复制保留正文换行；不复制围栏、语言标签或行号。 */
export function codeTokenText(token: Token): string {
  const firstLine = /^( {0,3})(`{3,}|~{3,})[^\r\n]*(?:\r\n|\r|\n)/.exec(token.raw);
  if (!firstLine) return 'text' in token ? token.text : token.raw;
  const body = token.raw.slice(firstLine[0].length);
  const fence = firstLine[2][0], count = firstLine[2].length;
  const closing = new RegExp(`(?:^|\\r\\n|\\r|\\n) {0,3}${fence === '`' ? '`' : '~'}{${count},}[ \\t]*(?:\\r\\n|\\r|\\n)?$`).exec(body);
  // 正文复制保留围栏内部的原始缩进；仅移除围栏本身。
  const closingBreak = closing ? /^(\r\n|\r|\n)/.exec(body.slice(closing.index))?.[0].length ?? 0 : 0;
  const content = closing ? body.slice(0, closing.index + closingBreak) : body;
  return content;
}
