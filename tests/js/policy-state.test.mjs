import test from "node:test";
import assert from "node:assert/strict";
import {
  POLICY_SCOPES,
  createPolicyBucket,
  policyRecordId,
  hasUnsavedPolicyDraft,
  selectPolicyRecord,
  replacePolicyRecords,
  upsertPolicyRecord,
  removePolicyRecord,
  discardPolicyDraft,
  addPolicyListItem,
  removePolicyListItem,
  buildPolicyBatchRequest,
  policyBatchRefreshScopes,
} from "../../pages/pluginCenter/policy-state.mjs";

const group = (id, general = true, builtin = true) => ({
  group_id: id,
  general_only_enabled: general,
  builtin_terms_enabled: builtin,
});
const user = (id, general = true, builtin = true) => ({
  user_id: id,
  general_only_enabled: general,
  builtin_terms_enabled: builtin,
});

test("independent lists are deep copied and normalized", () => {
  let bucket = selectPolicyRecord(createPolicyBucket(), "group", "1");
  bucket = { ...bucket, draft: { group_id: "1", general_only_enabled: true, builtin_terms_enabled: false, custom_terms: [], blacklisted_illust_ids: [] }, baseline: { group_id: "1", general_only_enabled: true, builtin_terms_enabled: false, custom_terms: [], blacklisted_illust_ids: [] } };
  bucket = addPolicyListItem(bucket, "custom_terms", " Ａ‐Ｂ ");
  assert.deepEqual(bucket.draft.custom_terms, ["Ａ‐Ｂ"]);
  assert.equal(hasUnsavedPolicyDraft(bucket), true);
  bucket = addPolicyListItem(bucket, "blacklisted_illust_ids", "001");
  assert.deepEqual(bucket.draft.blacklisted_illust_ids, ["1"]);
  bucket = removePolicyListItem(bucket, "custom_terms", 0);
  assert.deepEqual(bucket.draft.custom_terms, []);
});

test("separator-only terms are rejected without changing the draft", () => {
  let bucket = selectPolicyRecord(createPolicyBucket(), "group", "1");
  bucket = { ...bucket, draft: { group_id: "1", custom_terms: ["keep"], blacklisted_illust_ids: [] }, baseline: { group_id: "1", custom_terms: ["keep"], blacklisted_illust_ids: [] } };
  assert.throws(() => addPolicyListItem(bucket, "custom_terms", " -—・ "), /不能为空/);
  assert.deepEqual(bucket.draft.custom_terms, ["keep"]);
  assert.throws(() => addPolicyListItem(bucket, "custom_terms", "ｋｅｅｐ"), /已存在/);
  assert.deepEqual(bucket.draft.custom_terms, ["keep"]);
});

test("punctuation variants are stored as separate terms", () => {
  let bucket = selectPolicyRecord(createPolicyBucket(), "group", "1");
  bucket = { ...bucket, draft: { group_id: "1", custom_terms: [], blacklisted_illust_ids: [] }, baseline: { group_id: "1", custom_terms: [], blacklisted_illust_ids: [] } };
  bucket = addPolicyListItem(bucket, "custom_terms", "r18g");
  bucket = addPolicyListItem(bucket, "custom_terms", "r-18g");
  assert.deepEqual(bucket.draft.custom_terms, ["r-18g", "r18g"]);
  assert.throws(() => addPolicyListItem(bucket, "custom_terms", "Ｒ－１８Ｇ"), /已存在/);
});

test("scope definitions map independent fields and endpoints", () => {
  assert.equal(POLICY_SCOPES.group.idKey, "group_id");
  assert.equal(POLICY_SCOPES.private.idKey, "user_id");
  assert.match(POLICY_SCOPES.private.listEndpoint, /private-policies$/);
  assert.match(POLICY_SCOPES.group.removeEndpoint, /group-policy\/remove$/);
});

test("policyRecordId normalizes the active scope identifier", () => {
  assert.equal(policyRecordId("group", group(" 100 ")), "100");
  assert.equal(policyRecordId("private", user(" u1 ")), "u1");
  assert.equal(policyRecordId("private", group("100")), "");
});

test("group and private buckets remain isolated with the same ID", () => {
  const groups = upsertPolicyRecord(createPolicyBucket("group"), "group", group("1", false));
  const users = upsertPolicyRecord(createPolicyBucket("private"), "private", user("1", true, false));
  assert.equal(groups.records[0].general_only_enabled, false);
  assert.equal(users.records[0].builtin_terms_enabled, false);
  assert.equal(groups.records[0].user_id, undefined);
});

test("replace preserves a valid selection and falls back to the first record", () => {
  const bucket = replacePolicyRecords(createPolicyBucket("group"), "group", [group("1"), group("2")], "2");
  assert.equal(bucket.selectedId, "2");
  const replaced = replacePolicyRecords(bucket, "group", [group("3"), group("4")]);
  assert.equal(replaced.selectedId, "3");
});

test("select creates independent baseline and draft copies", () => {
  let bucket = replacePolicyRecords(createPolicyBucket("private"), "private", [user("u1")]);
  bucket = selectPolicyRecord(bucket, "private", "u1");
  bucket.draft.general_only_enabled = false;
  assert.equal(bucket.baseline.general_only_enabled, true);
  assert.equal(hasUnsavedPolicyDraft(bucket), true);
});

test("upsert replaces without duplication and marks the saved record clean", () => {
  let bucket = replacePolicyRecords(createPolicyBucket("group"), "group", [group("1")]);
  bucket = upsertPolicyRecord(bucket, "group", group("1", false, false));
  assert.equal(bucket.records.length, 1);
  assert.equal(bucket.draft.general_only_enabled, false);
  assert.equal(hasUnsavedPolicyDraft(bucket), false);
});

test("discard restores the saved baseline", () => {
  let bucket = replacePolicyRecords(createPolicyBucket("private"), "private", [user("u1")]);
  bucket.draft.builtin_terms_enabled = false;
  bucket = discardPolicyDraft(bucket);
  assert.equal(bucket.draft.builtin_terms_enabled, true);
  assert.equal(hasUnsavedPolicyDraft(bucket), false);
});

test("remove selects the adjacent record", () => {
  let bucket = replacePolicyRecords(createPolicyBucket("group"), "group", [group("1"), group("2"), group("3")], "2");
  bucket = removePolicyRecord(bucket, "group", "2");
  assert.deepEqual(bucket.records.map((record) => record.group_id), ["1", "3"]);
  assert.equal(bucket.selectedId, "3");
});

test("removing the final record clears selection and draft", () => {
  let bucket = replacePolicyRecords(createPolicyBucket("private"), "private", [user("u1")]);
  bucket = removePolicyRecord(bucket, "private", "u1");
  assert.equal(bucket.selectedId, "");
  assert.equal(bucket.draft, null);
  assert.equal(bucket.records.length, 0);
});

test("request state and search are local to each bucket", () => {
  const groups = { ...createPolicyBucket("group"), loading: true, search: "10" };
  const users = createPolicyBucket("private");
  assert.equal(groups.loading, true);
  assert.equal(groups.search, "10");
  assert.equal(users.loading, false);
  assert.equal(users.search, "");
});

test("batch helper builds all six field-target payloads and refresh scopes", () => {
  for (const field of ["custom_terms", "blacklisted_illust_ids"]) {
    for (const target of ["group", "private", "all"]) {
      assert.deepEqual(buildPolicyBatchRequest("group", " g1 ", field, target), { source_scope: "group", source_id: "g1", field, target });
      assert.deepEqual(policyBatchRefreshScopes(target), target === "all" ? ["group", "private"] : [target]);
    }
  }
  assert.throws(() => buildPolicyBatchRequest("group", "g1", "bad", "all"));
});

test("real policy state chain preserves deep-copy boundaries and rejects invalid IDs", () => {
  const input = { ...group("g1"), custom_terms: ["alpha"], blacklisted_illust_ids: ["1"] };
  let bucket = replacePolicyRecords(createPolicyBucket("group"), "group", [input]);
  bucket = selectPolicyRecord(bucket, "group", "g1");
  assert.notEqual(bucket.records[0], input);
  assert.notEqual(bucket.draft, bucket.baseline);
  bucket = addPolicyListItem(bucket, "custom_terms", "beta");
  bucket = addPolicyListItem(bucket, "blacklisted_illust_ids", "2");
  assert.equal(hasUnsavedPolicyDraft(bucket), true);
  const dirtySnapshot = structuredClone(bucket);
  for (const invalid of ["", "0", "-1", "1.2"]) {
    const before = structuredClone(bucket);
    assert.throws(() => addPolicyListItem(bucket, "blacklisted_illust_ids", invalid));
    assert.deepEqual(bucket, before);
  }
  assert.throws(() => addPolicyListItem(bucket, "blacklisted_illust_ids", "2"));
  assert.deepEqual(bucket, dirtySnapshot);
  bucket = removePolicyListItem(bucket, "custom_terms", 0);
  bucket = discardPolicyDraft(bucket);
  assert.equal(hasUnsavedPolicyDraft(bucket), false);
  const saved = upsertPolicyRecord(bucket, "group", { ...bucket.baseline, custom_terms: ["saved"], blacklisted_illust_ids: ["3"] });
  assert.equal(hasUnsavedPolicyDraft(saved), false);
  const privateBucket = replacePolicyRecords(createPolicyBucket("private"), "private", [{ ...user("g1"), custom_terms: ["private"] }]);
  assert.equal(privateBucket.records[0].user_id, "g1");
  assert.equal(privateBucket.records[0].group_id, undefined);
});
