import test from 'node:test';
import assert from 'node:assert/strict';
import {
  collectListSelection,
  collectSettingsPatch,
  filterSettingsFields,
  normalizeChoiceOptions,
  parseNumberDraft,
  pickSearchGroup,
  sameSettingValue,
  settingsSearchHits,
} from '../../pages/pluginCenter/settings.mjs';

const groups = () => [
  {
    key: 'content_dedupe',
    label: '去重与安全',
    fields: [
      { key: 'dedupe_days', label: '去重天数', hint: '最近自然日', type: 'int', value: 1, slider: { min: 0, max: 7 } },
      { key: 'global_safety_terms', label: '全局屏蔽关键词', type: 'list', delegated: true, view: 'safety' },
    ],
  },
  {
    key: 'checkin_basic',
    label: '签到基础',
    fields: [
      { key: 'checkin_enabled', label: '签到总开关', type: 'bool', value: true },
      { key: 'checkin_hitokoto_categories', label: '一言类型', type: 'list', value: ['全部', '动画', '旧分类'], options: ['全部', '动画', '文学'] },
      { key: 'checkin_ai_greeting_prompt', label: 'AI 问候提示词', type: 'text', value: '原始提示词' },
      { key: 'pixiv_refresh_token', label: 'Pixiv 令牌', type: 'string', secret: true, configured: true },
    ],
  },
];

test('declared string options keep their value as label', () => {
  assert.deepEqual(normalizeChoiceOptions(['Lolicon', 'Pixiv'], 'Lolicon'), [
    { value: 'Lolicon', label: 'Lolicon' },
    { value: 'Pixiv', label: 'Pixiv' },
  ]);
});

test('provider options carry model labels and an explicit empty choice', () => {
  assert.deepEqual(
    normalizeChoiceOptions([{ value: 'p1', label: 'gpt-4o（p1）' }], 'p1', true),
    [
      { value: '', label: '（留空）使用会话默认模型' },
      { value: 'p1', label: 'gpt-4o（p1）' },
    ],
  );
});

test('a stored value missing from the list stays selectable without widening declared options', () => {
  const declared = ['Lolicon', 'Pixiv'];
  const options = normalizeChoiceOptions(declared, 'legacy-source');
  assert.deepEqual(declared, ['Lolicon', 'Pixiv']);
  assert.deepEqual(options.map((item) => item.value), ['legacy-source', 'Lolicon', 'Pixiv']);
});

test('search matches label, key and hint and reports per-group hits', () => {
  const list = groups();
  assert.equal(filterSettingsFields(list[0].fields, '自然日').length, 1);
  assert.equal(filterSettingsFields(list[1].fields, 'checkin_enabled').length, 1);
  assert.equal(filterSettingsFields(list[1].fields, '提示词').length, 1);
  assert.deepEqual(settingsSearchHits(list, '安全'), [
    { key: 'content_dedupe', hits: 0, matched: true },
    { key: 'checkin_basic', hits: 0, matched: false },
  ]);
  assert.deepEqual(settingsSearchHits(list, '自然日'), [
    { key: 'content_dedupe', hits: 1, matched: true },
    { key: 'checkin_basic', hits: 0, matched: false },
  ]);
});

test('search lands on the group with the most hits unless the user picked one', () => {
  const list = groups();
  assert.equal(pickSearchGroup(list, '安全', 'checkin_basic', false), 'content_dedupe');
  assert.equal(pickSearchGroup(list, '安全', 'checkin_basic', true), 'checkin_basic');
  assert.equal(pickSearchGroup(list, '', 'checkin_basic', false), 'checkin_basic');
  assert.equal(pickSearchGroup([], 'x', '', false), '');
});

test('numeric drafts reject empty text, non-integers and out-of-range values', () => {
  const field = { type: 'int', slider: { min: 0, max: 7 } };
  assert.deepEqual(parseNumberDraft('', field), { valid: false, message: '请填写数值' });
  assert.equal(parseNumberDraft('  ', field).valid, false);
  assert.equal(parseNumberDraft('abc', field).valid, false);
  assert.equal(parseNumberDraft('1.5', field).valid, false);
  assert.equal(parseNumberDraft('8', field).valid, false);
  assert.equal(parseNumberDraft('-1', field).valid, false);
  assert.deepEqual(parseNumberDraft('0', field), { valid: true, value: 0 });
  assert.deepEqual(parseNumberDraft('7', field), { valid: true, value: 7 });
});

test('numeric drafts enforce slider step relative to min with float tolerance', () => {
  const coins = { type: 'int', slider: { min: 0, max: 200, step: 5 } };
  assert.equal(parseNumberDraft('22', coins).valid, false);
  assert.deepEqual(parseNumberDraft('20', coins), { valid: true, value: 20 });
  assert.deepEqual(parseNumberDraft('25', coins), { valid: true, value: 25 });
  assert.deepEqual(parseNumberDraft('0', coins), { valid: true, value: 0 });

  const size = { type: 'float', slider: { min: 0, max: 25, step: 0.5 } };
  assert.deepEqual(parseNumberDraft('10.5', size), { valid: true, value: 10.5 });
  assert.equal(parseNumberDraft('10.25', size).valid, false);
  assert.equal(
    parseNumberDraft('0.30000000000000004', { type: 'float', slider: { min: 0, max: 1, step: 0.1 } }).valid,
    true,
  );

  const offset = { type: 'int', slider: { min: 1, max: 30, step: 2 } };
  assert.deepEqual(parseNumberDraft('7', offset), { valid: true, value: 7 });
  assert.equal(parseNumberDraft('8', offset).valid, false);

  const free = { type: 'float', slider: { min: 0, max: 1, step: 'any' } };
  assert.deepEqual(parseNumberDraft('0.123', free), { valid: true, value: 0.123 });
});

test('list selections keep declared order and append preserved legacy values', () => {
  assert.deepEqual(
    collectListSelection(['全部', '动画', '文学'], ['旧分类'], ['文学', '旧分类', '全部']),
    ['全部', '文学', '旧分类'],
  );
  assert.deepEqual(collectListSelection(['全部'], [], []), []);
});

test('partial patch keeps only changed fields and separates explicit secret clearing', () => {
  const list = groups();
  const snapshot = JSON.stringify(list);
  const { patch, clearSecrets } = collectSettingsPatch(
    list,
    {
      dedupe_days: 3,
      checkin_enabled: true,
      checkin_ai_greeting_prompt: '新提示词',
      pixiv_refresh_token: '  new-token  ',
    },
    new Set(['pixiv_refresh_token']),
  );
  assert.deepEqual(patch, {
    content_dedupe: { dedupe_days: 3 },
    checkin_basic: { checkin_ai_greeting_prompt: '新提示词' },
  });
  assert.deepEqual(clearSecrets, ['pixiv_refresh_token']);
  assert.equal(JSON.stringify(list), snapshot);
});

test('reverting a field to its saved value yields no patch, including list order changes', () => {
  const list = groups();
  assert.deepEqual(
    collectSettingsPatch(list, { dedupe_days: 1, checkin_enabled: true }),
    { patch: {}, clearSecrets: [] },
  );
  const reordered = collectSettingsPatch(list, {
    checkin_hitokoto_categories: ['动画', '全部', '旧分类'],
  });
  assert.deepEqual(reordered.patch, {});
});

test('multi-select keeps preserved legacy values when another option changes', () => {
  const list = groups();
  const { patch } = collectSettingsPatch(list, {
    checkin_hitokoto_categories: collectListSelection(
      ['全部', '动画', '文学'],
      ['旧分类'],
      ['全部', '旧分类'],
    ),
  });
  assert.deepEqual(patch, {
    checkin_basic: { checkin_hitokoto_categories: ['全部', '旧分类'] },
  });
});

test('delegated fields and blank secrets never enter the patch', () => {
  const list = groups();
  assert.deepEqual(
    collectSettingsPatch(list, {
      global_safety_terms: ['x'],
      pixiv_refresh_token: '',
    }),
    { patch: {}, clearSecrets: [] },
  );
});

test('sameSettingValue compares lists as sets', () => {
  assert.equal(sameSettingValue(['a', 'b'], ['b', 'a']), true);
  assert.equal(sameSettingValue(['a'], ['a', 'b']), false);
  assert.equal(sameSettingValue(1, 1), true);
  assert.equal(sameSettingValue('1', 1), false);
});
