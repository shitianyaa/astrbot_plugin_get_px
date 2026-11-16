import test from 'node:test';
import assert from 'node:assert/strict';
import { createGroupDraft, splitLines, validateGroupDraft } from '../../pages/pluginCenter/safety.mjs';

test('draft edits do not mutate saved rules and R18 is opt-in', () => {
  const saved = { group_id: 'a', name: '插画', sessions: ['bot:GroupMessage:1'], custom_terms: ['猫耳'] };
  const draft = createGroupDraft(saved);
  draft.sessions.push('bot:FriendMessage:2');
  draft.custom_terms.push('女仆');
  assert.equal(draft.allow_r18, false);
  assert.deepEqual(saved.sessions, ['bot:GroupMessage:1']);
  assert.deepEqual(saved.custom_terms, ['猫耳']);
  assert.deepEqual(splitLines(' 猫耳 \n\n猫耳\n女仆\r\n'), ['猫耳', '女仆']);
});

test('full sessions enforce single group ownership, allowing edits of their own group', () => {
  const groups = [{ group_id: 'a', sessions: ['bot:GroupMessage:1'] }];
  const draft = createGroupDraft({ group_id: 'a', name: '插画', sessions: ['bot:GroupMessage:1'] });
  assert.equal(validateGroupDraft(draft, groups), draft);
  assert.throws(() => validateGroupDraft({ ...draft, group_id: 'b' }, groups));
  assert.throws(() => validateGroupDraft({ ...draft, sessions: ['1'] }, groups));
  assert.throws(() => validateGroupDraft({ ...draft, name: '' }, groups));
  assert.doesNotThrow(() => validateGroupDraft({ ...draft, group_id: 'b', sessions: ['another:GroupMessage:1', 'bot:FriendMessage:1'] }, groups));
});
