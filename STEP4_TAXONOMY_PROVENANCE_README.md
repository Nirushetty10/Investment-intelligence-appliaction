# Step 4 — Persist taxonomy provenance for every normalized period

This additive overlay makes taxonomy identity and the reason for every trust decision queryable in PostgreSQL. It does not invent or backfill an unavailable taxonomy version. Existing periods receive safe defaults and remain `PROVISIONAL` until reprocessed.

## Changes

- Adds `taxonomy_id`, `taxonomy_version`, `taxonomy_namespace`, `taxonomy_source_package_version`, `taxonomy_catalog_status`, `canonical_mapping_enabled`, `source_schema_refs` (JSONB), and `trust_reasons` (JSONB) to `financial_periods`.
- Keeps the trust status independent from parser/normalization success. Missing exact catalog versions and mapping-disabled families are recorded explicitly.
- Preserves the original schemaRef href values from the parsed filing.
- Extends `trusted_financial_periods` with the lineage fields for auditing eligible rows.
- Adds unit tests for resolved, unresolved, mismatched-version, warning, and unmapped-fact provenance.
- Extends PostgreSQL migration tests to verify old-schema migration recreates all lineage columns and trusted views.

## Apply

Back up `D:\Niranjan\Investment\nse_pipeline` (or create a Git checkpoint), then extract this overlay ZIP into the project root, preserving relative paths. Do not change or delete raw XBRL records.

Run:

```powershell
python -m pytest tests/test_taxonomy_provenance.py -v --tb=short
python -m pytest tests/test_db_schema_migration.py -v -rs
python -m pytest tests/ -v --tb=short
```

Use the same PowerShell session with `NSE_DB_HOST`, `NSE_DB_PORT`, `NSE_DB_USER`, `NSE_DB_PASSWORD`, and `NSE_TEST_DB_ADMIN` set. Database tests create and drop temporary `nse_pytest_*` databases; do not point this at a production database.

## Important limitation

The provided Ind AS taxonomy package declares 2025-01-31, while the included RELIANCE Q1 2026 fixture declares a 2026-01-31 schema namespace/schemaRef. This step persists that mismatch as explicit provenance; it does not substitute or fabricate the missing official 2026 package. Keep such periods provisional until the exact applicable taxonomy schema and linkbases are obtained and validated.
