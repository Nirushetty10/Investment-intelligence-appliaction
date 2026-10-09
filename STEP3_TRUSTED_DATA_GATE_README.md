# Step 3 — Type-aware fact classification and trusted-data gate

This is an overlay patch for the project after Step 2. It is intentionally additive; it does not delete or rewrite raw XBRL facts.

## Changes

- Uses the exact imported taxonomy version and the fact QName's namespace before using `value_kind` metadata in the unmapped-fact classifier.
- Reclassifies explicitly typed string, boolean, date, enumeration, text-block, and identifier concepts as non-financial metadata/disclosures when they are not declared input metadata for a normalized field. Unknown/custom XSD types remain reviewable; the code does not guess their kind.
- Adds `TAXONOMY_NON_FINANCIAL` to the taxonomy provenance report so those facts no longer inflate the genuinely-unmapped-financial count when authoritative exact-version metadata is available.
- Adds `validators/trust_gate.py`. A period is `TRUSTED` only if there is an exact taxonomy catalog, canonical mapping is enabled, there are no unresolved financial facts, no unresolved namespaces, and no validation warnings/errors. Errors set `BLOCKED`; unresolved or warning-bearing results remain `PROVISIONAL`.
- Adds `financial_periods.trust_status`. Existing rows receive `PROVISIONAL` by default, not `TRUSTED`.
- Adds SQL views: `trusted_financial_periods`, `trusted_income_statement`, `trusted_balance_sheet`, `trusted_cashflow_statement`, and `trusted_ratios`. The ratios view also excludes rows flagged `needs_validation = TRUE`.
- Pipeline processing computes trust status and upserts it with the financial period. `validation_status` and `normalization_status` keep their existing meanings; trust eligibility is a separate gate.
- Adds type-classification, trust-gate, and PostgreSQL schema regression tests.

## Apply

1. Back up `D:\Niranjan\Investment\nse_pipeline` or create a Git checkpoint.
2. Extract this overlay ZIP into `D:\Niranjan\Investment\nse_pipeline`, preserving its relative folders and overwriting matching files.
3. Keep your PostgreSQL process running and use the same PowerShell terminal in which the `NSE_DB_*` variables are configured.
4. Run:

```powershell
python -m pytest tests/test_taxonomy_type_classification.py tests/test_trust_gate.py -v --tb=short
python -m pytest tests/test_db_schema_migration.py -v -rs
python -m pytest tests/ -v --tb=short
```

The build container's local result is `159 passed, 7 skipped`: the seven real-PostgreSQL tests are skipped here because this isolated build environment has no database credentials. On the user's configured machine, expected suite total is 166 tests; all seven DB tests must actually run and pass, not be skipped.

## ML / downstream contract

Do not build features from base financial tables without the trust gate. Join through `trusted_financial_periods`, or use the corresponding `trusted_*` views. A filing can be successfully ingested and normalized while still being `PROVISIONAL` or `BLOCKED`.

The trust gate is deliberately filing/period-level and conservative. A missing exact taxonomy catalog (including the known 2026 Ind AS catalog gap) keeps the period provisional. This patch does not claim the 2026 Ind AS package gap is resolved, and it does not mark every financial source value as correct merely because the test suite passes.
