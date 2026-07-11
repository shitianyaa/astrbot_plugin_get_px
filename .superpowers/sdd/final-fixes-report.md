# Final Important Fixes Report

## Scope

- `main.py`: make the complete check-in artwork/render/cache/metadata/send/usage phase cancellation-safe.
- `checkin.py`: reject orphan records, duplicate profile user IDs, and duplicate record keys before database replacement.
- Regression tests only in the related check-in test modules.

## RED

### Cancellation lifecycle

Command:

```powershell
python -m pytest tests/test_main_error_handling.py -q -k "cancellation"
```

Result before the production fix: `2 failed`.

- Cache-store cancellation propagated, but the Pixiv claim release mock was awaited zero times.
- Usage cancellation propagated, but the Pixiv claim release mock was awaited zero times.
- Both failures reproduced cleanup bypass by `asyncio.CancelledError`.

### Snapshot integrity

Command:

```powershell
python -m pytest tests/test_checkin.py -q -k "orphan_record or duplicate_profile_user_id or duplicate_record_key"
```

Result before the production fix: `3 failed`.

- An orphan record was accepted.
- A duplicate profile reached SQLite and raised `IntegrityError` instead of validator `ValueError`.
- A duplicate record key reached SQLite and raised `IntegrityError` instead of validator `ValueError`.

## GREEN

### Focused regression tests

- Cancellation tests: `2 passed`.
- Snapshot integrity tests: `3 passed`.
- Related check-in, handler, and backup web tests: `46 passed`.

The cancellation tests assert that cancellation is not swallowed, the held claim is released exactly once, the downloaded Pixiv source image is deleted, and the final JPEG cache remains present. Usage cancellation is covered after a successful send, so a claim is consumed only after usage recording returns successfully.

The snapshot tests assert all three invalid shapes are rejected and compare exports before and after each failed import to prove the database is unchanged.

### Full verification

Commands:

```powershell
python -m pytest -q --ignore=tests/test_offset.py
python -m compileall -q .
python -m json.tool _conf_schema.json > $null
git diff --check
```

Results:

- Full tests: `134 passed`, with two pre-existing third-party deprecation warnings.
- Compile: exit `0`.
- JSON validation: exit `0`.
- Diff check: exit `0`.

## Self-review

- Correctness: the `finally` spans artwork selection through cache storage, metadata persistence, send, and usage recording. `CancelledError` remains uncaught and propagates after cleanup.
- Resource ownership: only the temporary Pixiv `background.image_path` is deleted; the final cache path is never passed to cleanup.
- Claim semantics: failures or cancellation retain `claim_held` and release it; only successful send plus successful usage recording clears it.
- Data integrity: uniqueness and reference validation runs after v1/v2 row normalization but before `_import_snapshot_sync()` opens the replacement transaction.
- Compatibility: v1 rows still receive v2 defaults and valid v1/v2 snapshots retain the same normalized output.
- Security/performance: validation is linear in snapshot row count, adds no dependencies, and rejects inconsistent untrusted backup data at the boundary.
- Scope: changes are limited to `main.py`, `checkin.py`, related tests, and this required report.
