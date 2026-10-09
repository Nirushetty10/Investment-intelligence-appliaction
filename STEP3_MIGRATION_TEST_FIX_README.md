# Step 3 migration-test fix

## What failed

The old-schema migration test creates the current schema and then tries to simulate an old schema by dropping `ratios.needs_validation`. Step 3 added the `trusted_ratios` view, which depends on that column, so PostgreSQL correctly refuses to drop the column while the view exists. The test fails during its downgrade setup, before it tests the migration path.

## Fix

`tests/test_db_schema_migration.py` now drops `trusted_ratios` before removing the new column to simulate a genuine pre-trust-gate schema. After running `init_schema()` again, the test asserts that `trusted_ratios` was recreated and that PostgreSQL can resolve the view against the restored column. The migration assertion was not removed or weakened.

## Apply

Back up your project, then extract the ZIP at the project root:

`D:\Niranjan\Investment\nse_pipeline`

It contains `tests/test_db_schema_migration.py` and this README.

## Verify

Run from the project root with the same PostgreSQL test credentials configured:

```powershell
python -m pytest tests/test_db_schema_migration.py::test_existing_old_schema_database_migrates_safely -v --tb=short
python -m pytest tests/test_db_schema_migration.py -v -rs
python -m pytest tests/ -v --tb=short
```

Expected if all other tests remain unchanged: 7 database tests pass, then the full suite reaches 166 passed, 0 failed, 0 skipped. Do not count skipped tests as passing.
