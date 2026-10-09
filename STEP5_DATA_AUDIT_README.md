# Step 5 — Read-only source-to-database integrity audit

This step adds a **read-only diagnostic command**, not a database migration. It lets you compare the saved XBRL source bytes with the PostgreSQL raw layer and compare today's normalization output with the persisted normalized rows.

## What it checks

- SHA-256 of saved source bytes against `raw_filings.content_hash`.
- Source-vs-database counts for XBRL contexts, units, and facts.
- Context periods and dimensions, unit declarations, and a multiset comparison of individual facts (including namespace, concept, raw value, numeric value, unit, scale, sign, decimals, and context reference).
- Whether a filing marked normalization `SUCCESS` has a linked normalized period.
- A read-only recomputation of current normalized statement values compared with stored values. Differences are marked for review only; the audit never reprocesses or updates rows.
- Whether a period marked `TRUSTED` contradicts persisted taxonomy provenance or has blocking data-quality log entries.

## Safety

The script starts a PostgreSQL transaction with `SET TRANSACTION READ ONLY`. It does not modify tables, statements, statuses, or raw records. A normalized drift finding is not an instruction to overwrite data; it can be caused by code/taxonomy version changes and must be reviewed before any reprocessing.

## Run for RELIANCE

From the project root and the same PowerShell session where the database settings are configured:

```powershell
python tools/audit_filing_integrity.py --symbols RELIANCE --limit 10 --output reports/reliance_data_audit.json
```

To audit known filings directly:

```powershell
python tools/audit_filing_integrity.py --filing-ids 7,9 --limit 10 --output reports/reliance_7_9_audit.json
```

The IDs `7,9` are examples based on the previously inspected fixture/log context; confirm they correspond to the desired rows in your own database before relying on them.

Exit code `0` means every selected filing passed all checks. Exit code `1` means the audit ran but one or more findings need review. Exit code `2` means it could not run or the arguments/configuration were invalid.

## Test

```powershell
python -m pytest tests/test_data_audit.py -v --tb=short
python -m pytest tests/ -v --tb=short
```

Run against your project copy. Do not run a data pull or reprocessing command as part of this audit.
