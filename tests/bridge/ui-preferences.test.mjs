import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { readUIPreferences, saveUIPreferences, localizeClientError, UI_PREFERENCES_KEY, uiStyles } from '../../frontend/src/ui-preferences.ts';
import { kindLabels, statusLabels, localizedKindLabels, localizedStatusLabels, makeRows, overviewFrame, relationshipEndpoints } from '../../frontend/src/components/model.ts';

function memoryStorage(initial = null) {
  const entries = new Map(initial == null ? [] : [[UI_PREFERENCES_KEY, initial]]);
  return { entries, getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value) };
}
function fixture(name) {
  return JSON.parse(readFileSync(new URL(`../fixtures/contracts/${name}.json`, import.meta.url)));
}

test('client errors translate only known UI messages and preserve arbitrary source text', () => {
  const timeout = '本地工具读取超时；宿主通道未在限定时间内返回。';
  assert.equal(localizeClientError(timeout, 'zh'), timeout);
  assert.match(localizeClientError(timeout, 'en'), /timed out/);
  for (const source of ['来源原始说明 remains verbatim', 'toString', '__proto__', '']) {
    assert.equal(localizeClientError(source, 'en'), source);
  }
});

test('first use follows the preferred browser language and defaults to classic style', () => {
  for (const languages of [[], ['zh-CN'], ['zh-TW', 'en-US'], ['fr-FR']]) {
    assert.deepEqual(readUIPreferences(memoryStorage(), languages), { language: 'zh', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
  }
  for (const languages of [['en'], ['en-US', 'zh-CN'], ['EN-gb']]) {
    assert.deepEqual(readUIPreferences(memoryStorage(), languages), { language: 'en', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
  }
});

test('malformed, primitive and array storage values fall back without throwing', () => {
  for (const saved of ['', '{broken', 'null', 'false', '42', '"en"', '[]', '[{"language":"en"}]']) {
    assert.deepEqual(readUIPreferences(memoryStorage(saved), ['en-US']), { language: 'en', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
  }
});

test('invalid fields fall back independently while valid fields remain usable', () => {
  for (const language of ['fr', '', null, 7, {}, ['en']]) {
    assert.deepEqual(readUIPreferences(memoryStorage(JSON.stringify({ language, uiStyle: 'soft' })), ['en-US']), { language: 'en', uiStyle: 'soft', showSpanSummary: false, showTimelineCursors: false });
  }
  for (const uiStyle of ['dark', '', null, 7, {}, ['terminal'], 'toString', '__proto__']) {
    assert.deepEqual(readUIPreferences(memoryStorage(JSON.stringify({ language: 'zh', uiStyle })), ['en-US']), { language: 'zh', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
  }
  for (const showSpanSummary of ['true', 'false', '', null, 0, 1, {}, []]) {
    assert.deepEqual(readUIPreferences(memoryStorage(JSON.stringify({ language: 'en', uiStyle: 'soft', showSpanSummary }))), { language: 'en', uiStyle: 'soft', showSpanSummary: false, showTimelineCursors: false });
  }
  for (const showTimelineCursors of ['true', 'false', '', null, 0, 1, {}, []]) {
    assert.deepEqual(readUIPreferences(memoryStorage(JSON.stringify({ language: 'en', uiStyle: 'soft', showSpanSummary: true, showTimelineCursors }))), { language: 'en', uiStyle: 'soft', showSpanSummary: true, showTimelineCursors: false });
  }
  assert.deepEqual(readUIPreferences(memoryStorage('{}'), ['en-US']), { language: 'en', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
});

test('missing or denied storage preserves defaults and never blocks saving', () => {
  const denied = {
    getItem() { throw new Error('Storage access denied'); },
    setItem() { throw new Error('Storage quota exceeded'); },
  };
  for (const storage of [null, denied]) {
    assert.deepEqual(readUIPreferences(storage, ['en-US']), { language: 'en', uiStyle: 'classic', showSpanSummary: false, showTimelineCursors: false });
    assert.doesNotThrow(() => saveUIPreferences(storage, { language: 'en', uiStyle: 'terminal', showSpanSummary: true, showTimelineCursors: true }));
  }
});

test('every language and style persists independently of task data', () => {
  for (const language of ['zh', 'en']) for (const uiStyle of uiStyles) for (const showSpanSummary of [false, true]) for (const showTimelineCursors of [false, true]) {
    const storage = memoryStorage();
    const expected = { language, uiStyle, showSpanSummary, showTimelineCursors };
    const withTaskContent = { ...expected, threadId: 'private-task', events: ['private-content'], filters: { text: 'secret' } };
    saveUIPreferences(storage, withTaskContent);
    assert.equal(storage.entries.size, 1);
    assert.deepEqual(JSON.parse(storage.getItem(UI_PREFERENCES_KEY)), expected);
    assert.deepEqual(readUIPreferences(storage, language === 'zh' ? ['en-US'] : ['zh-CN']), expected);
    assert.ok(!storage.getItem(UI_PREFERENCES_KEY).includes('private'));
  }
  assert.deepEqual(readUIPreferences(memoryStorage(JSON.stringify({ language: 'en', uiStyle: 'soft', events: ['private-content'] }))), { language: 'en', uiStyle: 'soft', showSpanSummary: false, showTimelineCursors: false });
});

test('localized kind, status and relationship labels keep their original keys and endpoints', () => {
  assert.deepEqual(localizedKindLabels(), kindLabels);
  assert.deepEqual(localizedStatusLabels(), statusLabels);
  assert.deepEqual(Object.keys(localizedKindLabels('en')), Object.keys(kindLabels));
  assert.deepEqual(Object.keys(localizedStatusLabels('en')), Object.keys(statusLabels));
  assert.equal(localizedKindLabels('en').toolCall, 'Tool call');
  assert.equal(localizedStatusLabels('en').running, 'Running');
  for (const type of ['spawned-by', 'forked-from', 'communicates-with']) {
    const relation = { type, fromThreadId: 'from', toThreadId: 'to' };
    const chinese = relationshipEndpoints(relation, 'zh');
    const english = relationshipEndpoints(relation, 'en');
    assert.deepEqual(english.map(([, id]) => id), chinese.map(([, id]) => id));
    assert.notDeepEqual(english.map(([label]) => label), chinese.map(([label]) => label));
    assert.deepEqual(relation, { type, fromThreadId: 'from', toThreadId: 'to' });
  }
});

test('language changes row labels without changing event content, scope, order or layout', () => {
  const snapshot = fixture('snapshot-running');
  const view = fixture('view-state');
  view.filters.text = '';
  view.expandedGroupIds = snapshot.groups.map(group => group.groupId);
  view.expandedTurnIds = snapshot.turns.map(turn => turn.turnId);
  snapshot.events.unshift({ ...structuredClone(snapshot.events[0]), eventId: 'context-record', turnId: null, preview: '原始上下文 retained verbatim' });
  const originalSnapshot = structuredClone(snapshot);
  const originalView = structuredClone(view);
  for (const agentScope of [{ kind: 'all', groupId: null }, { kind: 'selected', groupIds: snapshot.groups.map(group => group.groupId) }]) {
    const scopedView = { ...view, agentScope };
    const chinese = makeRows(snapshot, scopedView, snapshot.events, 'zh');
    const english = makeRows(snapshot, scopedView, snapshot.events, 'en');
    const withoutLabels = rows => rows.map(({ label, ...row }) => row);
    assert.deepEqual(withoutLabels(english), withoutLabels(chinese));
    assert.notDeepEqual(english.filter(row => row.type === 'turn').map(row => row.label), chinese.filter(row => row.type === 'turn').map(row => row.label));
    assert.equal(english.find(row => row.key === 'turn:main:context').label, 'Task context · position not recorded');
    assert.ok(english.some(row => row.type === 'turn' && row.label.startsWith('Child task · Turn ')));
    assert.equal(english.find(row => row.type === 'event' && row.event.eventId === 'context-record').event.preview, '原始上下文 retained verbatim');
  }
  assert.deepEqual(snapshot, originalSnapshot);
  assert.deepEqual(view, originalView);
});

test('localized frame explanations preserve timeline evidence and coordinates in every timing mode', () => {
  const snapshot = fixture('snapshot-running');
  const originalSnapshot = structuredClone(snapshot);
  const cases = [snapshot, { ...snapshot, timelineOrigin: undefined }, {
    ...snapshot,
    events: snapshot.events.map(event => ({ ...event, timing: { ...event.timing, startMs: null, endMs: null, durationMs: null } })),
  }];
  for (const data of cases) {
    const { explanation: chinese, ...chineseFrame } = overviewFrame(data, data.events, 'zh');
    const { explanation: english, ...englishFrame } = overviewFrame(data, data.events, 'en');
    assert.deepEqual(englishFrame, chineseFrame);
    assert.notEqual(english, chinese);
    if (englishFrame.origin != null && !englishFrame.ordered) assert.ok(english.endsWith(data.timelineOrigin.explanation));
  }
  assert.deepEqual(snapshot, originalSnapshot);
});
