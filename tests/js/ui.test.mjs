import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeSelectOptions, stepOptionIndex } from '../../pages/pluginCenter/ui.mjs';

test('select options keep explicit empty values, dedupe by value and accept string shorthands', () => {
  const options = normalizeSelectOptions([
    'Lolicon',
    { value: 'p1', label: 'gpt-4o（p1）' },
    { value: '', label: '（留空）使用会话默认模型' },
    { value: 'p1', label: '重复项' },
    { value: 'p2', text: '备用模型' },
  ]);
  assert.deepEqual(options.map((item) => item.value), ['Lolicon', 'p1', '', 'p2']);
  assert.deepEqual(options.map((item) => item.label), [
    'Lolicon',
    'gpt-4o（p1）',
    '（留空）使用会话默认模型',
    '备用模型',
  ]);
});

test('select options ignore entries without a value and keep disabled markers', () => {
  const options = normalizeSelectOptions([
    { label: '没有值' },
    { value: 'locked', label: '不可选', disabled: true },
  ]);
  assert.deepEqual(options, [
    { value: 'locked', label: '不可选', disabled: true },
  ]);
});

test('keyboard navigation stops at both ends and Home/End jump to the edges', () => {
  assert.equal(stepOptionIndex(0, 3, 'ArrowUp'), 0);
  assert.equal(stepOptionIndex(2, 3, 'ArrowDown'), 2);
  assert.equal(stepOptionIndex(1, 3, 'ArrowDown'), 2);
  assert.equal(stepOptionIndex(1, 3, 'ArrowUp'), 0);
  assert.equal(stepOptionIndex(1, 3, 'Home'), 0);
  assert.equal(stepOptionIndex(1, 3, 'End'), 2);
  assert.equal(stepOptionIndex(-1, 3, 'ArrowDown'), 0);
  assert.equal(stepOptionIndex(-1, 3, 'ArrowUp'), 2);
  assert.equal(stepOptionIndex(0, 0, 'ArrowDown'), -1);
});
