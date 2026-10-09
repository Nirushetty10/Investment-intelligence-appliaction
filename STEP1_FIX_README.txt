NSE PIPELINE — STEP 1 PATCH: TAXONOMY IDENTITY SAFETY

Purpose
-------
Resolve filing taxonomy family/version from schemaRef + official SEBI
family namespace evidence rather than guessing from the shared in-capmkt
core namespace. The 2025 taxonomy packages for multiple sectors reuse the
same core namespace, so the core URI by itself is ambiguous.

Changes
-------
1. parsers/ixbrl_parser.py
   - Preserves link:schemaRef href values.
   - Preserves root namespace prefix -> full namespace URI declarations.

2. taxonomy/registry.py
   - Adds profiles for Ind AS, Other-than-Banks, Banking, NBFC, General
     Insurance, Life Insurance, and REIT/InvIT based on supplied package
     namespace patterns.
   - Fails closed when family evidence is absent, conflicting, or the core
     namespace version conflicts with the family namespace version.
   - Separates identity resolution from permission to canonical-map facts.
   - Only the existing Ind AS (Other than Banks) mapping path is enabled.
     Other recognized sectors are identified but not normalized into common
     canonical fields until sector-specific mappings are validated.
   - Exposes the version of the supplied package catalog separately from the
     filing namespace version. This does not imply all catalog data is loaded.

3. normalizers/financial_normalizer.py and taxonomy_classifier.py
   - Do not map other recognized sector taxonomies using the Ind AS mappings.
   - Emit taxonomy_mapping_disabled guard finding and TAXONOMY_UNSUPPORTED
     classification while preserving source facts.

4. tests/
   - Adds regression tests for shared core namespace ambiguity, Banking
     identity, conflicting taxonomy evidence, non-SEBI namespace spoofing,
     and blocking cross-sector canonical mapping.
   - Adds schema metadata to the reconstructed fixtures. The actual Q1 XBRL
     fixture already included sector-specific namespace evidence.

Validation in the build environment
-----------------------------------
- python -m compileall -q . : passed
- python -m pytest tests/ -q : 143 passed, 6 skipped
- The 6 skips are PostgreSQL integration tests because this build environment
  has no NSE_DB_* credentials. The user's Windows environment previously ran
  those six successfully; rerun the suite there after applying this overlay.

Known remaining issue
---------------------
This patch resolves taxonomy identity, not complete taxonomy ingestion. The
concept/periodType registry is not yet a full, exact-version import of every
supplied XSD, label, data type, relationship, and dimensional definition.
The current Ind AS periodType catalog remains a 2025 source fallback for
2026 filings and must remain visibly flagged until the matching 2026 schema
is available and imported. Do not treat all normalized facts as trusted ML
inputs just because tests pass.

Apply
-----
Back up the current project first. Extract this ZIP into the project root:
D:\Niranjan\Investment\nse_pipeline
Allow files to merge/overwrite the same relative paths.
Then run:
python -m pytest tests/ -v --tb=short

If all six DB tests are configured and reachable, they should execute instead
of being skipped. Do not point them at a production database.
