import test from 'node:test';
import assert from 'node:assert/strict';
import { collectSettingsPatch, normalizeChoiceOptions } from '../../pages/pluginCenter/settings.mjs';

test('declared string options keep their value as label', () => {
  const options = normalizeChoiceOptions(['Lolicon', 'Pixiv'], 'Lolicon');
  assert.deepEqual(options, [
    { value: 'Lolicon', label: 'Lolicon' },
    { value: 'Pixiv', label: 'Pixiv' },
  ]);
});

test('provider options carry model labels and an explicit empty choice', () => {
  const options = normalizeChoiceOptions(
    [{ value: 'p1', label: 'gpt-4o（p1）' }],
    'p1',
    true,
  );
  assert.deepEqual(options, [
    { value: '', label: '（留空）使用会话默认模型' },
    { value: 'p1', label: 'gpt-4o（p1）' },
  ]);
});

test('a stored value missing from the list stays selectable without widening declared options', () => {
  const declared = ['Lolicon', 'Pixiv'];
  const options = normalizeChoiceOptions(declared, 'legacy-source');
  assert.deepEqual(declared, ['Lolicon', 'Pixiv']);
  assert.deepEqual(options.map((item) => item.value), ['legacy-source', 'Lolicon', 'Pixiv']);
});

test('partial patch keeps only changed fields and separates explicit secret clearing', () => {
  const groups = [
    {
      key: 'pixiv_source',
      fields: [{ key: 'pixiv_refresh_token' }, { key: 'max_count' }, { key: 'image_quality' }],
    },
    { key: 'runtime', fields: [{ key: 'rate_limit_seconds' }] },
  ];
  const results = {
    pixiv_refresh_token: { changed: true, clear: true },
    max_count: { changed: true, value: 8 },
    image_quality: { changed: false, value: 'original' },
    rate_limit_seconds: { changed: false, value: 3 },
  };
  const snapshot = JSON.stringify(groups);
  const { patch, clearSecrets } = collectSettingsPatch(groups, (field) => results[field.key]);
  assert.deepEqual(patch, { pixiv_source: { max_count: 8 } });
  assert.deepEqual(clearSecrets, ['pixiv_refresh_token']);
  assert.equal(JSON.stringify(groups), snapshot);
});

test('nothing to save yields an empty patch and no clear request', () => {
  const groups = [{ key: 'checkin_basic', fields: [{ key: 'checkin_enabled' }] }];
  const { patch, clearSecrets } = collectSettingsPatch(groups, () => ({ changed: false }));
  assert.deepEqual(patch, {});
  assert.deepEqual(clearSecrets, []);
});
