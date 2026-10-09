# NSE Financial Data Pipeline — Clean-Data Plan

## Goal

Keep every downloaded filing and its source facts traceable and immutable. Only admit financial values into the **trusted dataset** when the source taxonomy, concept, context period, dimensions, units, mapping, and validation results support the interpretation. “Clean” means auditable and appropriately gated, not a promise that an issuer's original filing can never contain errors.

## Current position and known blockers

The current project snapshot now has regression guards for namespace spoofing and XBRL `periodType` / context mismatches, resolves iXBRL QName prefixes to full namespace URIs, and downgrades validation warnings to `PARTIAL`. Regression tests are in `tests/test_taxonomy_validation_guards.py`.

Important known limitations:

1. The supplied official Ind AS core XSD is version `2025-01-31`, while the RELIANCE fixture/filer taxonomy registry recognizes `2026-01-31`. Its 499-concept periodType catalog is only a fallback. A filing with a version mismatch is deliberately warned and must not be called fully trusted.
2. The registry/catalog needs complete, versioned metadata for the taxonomy actually referenced by each filing. Do not infer the schema version from only a filing date or local concept name.
3. The code fails closed for an unrecognized namespace. A full `schemaRef`/`import` graph resolver is still required to positively distinguish genuine issuer-extension namespaces from unsupported/unregistered standard taxonomies.
4. A filing-level `PARTIAL` status is not, by itself, a universal row-level trusted-data interface. Before ML use, enforce a database/view/API gate so provisional and quarantined facts cannot leak into feature tables.
5. The existing treatment of every dimensioned fact as segment data must be refined using axis/member semantics. A dimension can be an expense breakdown or another analysis, not necessarily an operating segment.
6. PostgreSQL migration tests have not run unless the `NSE_DB_*` variables point to a reachable, dedicated test database. Never run migration test code against production.

## Target ingestion flow

`NSE discovery → immutable raw archive → parser → taxonomy resolution → fact/context validation → canonical normalization → reconciliation / quality findings → trusted eligibility gate → API / analytics / ML`

### 1. Discovery and immutable source archive

- Keep the NSE filing identifier, issuer identifier, symbol, filing URL/source, filing/submission date when available, reporting period, standalone/consolidated marker, source file name, source MIME/content type, retrieval timestamp, and SHA-256 hash.
- Store original HTML/iXBRL/XML and supporting artifacts byte-for-byte in the raw archive. Never rewrite a downloaded source file to “fix” its content.
- Make discovery and downloads idempotent. Identical bytes for the same filing must not duplicate the filing; a revised filing must be stored as a new version with its own hash and relationship to the earlier version.
- Treat the registry's `period_type` as metadata/hint only. Resolve actual facts using filing context periods, concept taxonomy metadata, financial-year boundaries, and filing semantics.

### 2. Versioned taxonomy registry

For every filing, resolve the exact taxonomy identity and version from its `schemaRef`, namespace URI, imports, and taxonomy package. Store package/version/checksum/source alongside the filing.

Import and index, by QName and version, as applicable:

- concept name and full namespace URI;
- labels and documentation;
- `periodType`, datatype, balance, and abstract status;
- presentation, calculation, definition, references, dimensional axes and members;
- schema import/includes and extension relationships.

Maintain separate supported catalogs for Ind AS/other-than-banks, banking, NBFC, general insurance, life insurance, and REIT/InvIT, plus relevant historical versions. Never use a concept's local name alone as a canonical identity. A taxonomy update should be diffed against the previous version and reviewed before being marked active.

**Release blocker:** load and verify the exact 2026 schema(s) for current 2026 filings. Until that is done, current 2026 filings that use the 2026 namespace remain provisional/`PARTIAL` when validated with the 2025 fallback catalog. For 2015–2024 backfill, load the actual historical versions rather than reusing today's mappings blindly.

### 3. Parsing without data loss

- Resolve prefixes from the XML namespace map to full namespace URIs; prefixes are aliases, not identifiers.
- Preserve every fact including unmapped, dimensional, duplicate, nil, and suspicious facts. Preserve original text, QName, context reference, unit reference, `decimals`, `scale`, `sign`, and source location where available.
- Parse contexts with entity identifier, instant or duration start/end, and every explicit/typed dimension. Parse units precisely.
- Detect duplicate facts and conflicting values rather than silently selecting one. Capture parser warnings and source-file/line or element provenance when available.
- The raw fact layer is append-only; validation and correction metadata belongs in separate quality/decision records.

### 4. Normalization rules

A source fact may populate a canonical field only if all required checks pass:

1. Taxonomy version is resolved and supported.
2. Full QName/namespace has an approved mapping for that taxonomy version, or an explicit reviewed extension mapping exists.
3. Taxonomy concept's `periodType` matches the context type (`instant` or `duration`). Missing metadata is unknown, not permission to guess.
4. The context dates match the requested period exactly. Annual, cumulative/YTD, Q4-only, and standalone-quarter durations must remain distinguishable.
5. Context dimensions are consistent with the target field. Do not collapse segment/dimensional values into issuer-wide totals.
6. Unit and sign/scale/decimals semantics are handled once and traceably; no silent crore/lakh scaling or duplicate conversions.
7. Candidate facts do not conflict. If several candidates could populate the same field and the difference cannot be resolved authoritatively, leave the canonical field null and flag the ambiguity.

Mapping must be QName- and taxonomy-version-aware. Keep the raw fact ID/source provenance alongside each canonical fact so every value can be traced back to its exact source context and unit.

### 5. Validation layers and status model

Track the results independently; do not let successful parsing imply financially trusted data.

- **RAW_ARCHIVED:** source bytes are saved and hashed.
- **PARSED:** contexts, units, and facts were parsed; parse limitations are recorded.
- **NORMALIZED_PROVISIONAL:** candidate canonical fields were derived, but at least one trust gate is still unresolved.
- **TRUSTED:** exact taxonomy and mapping are supported, required period/unit/dimension checks pass, and blocking quality rules pass.
- **QUARANTINED:** a fact or filing failed a blocking check or has unresolved semantic ambiguity that makes canonical use unsafe.

At filing level, distinguish ingestion/structural status from financial semantic status. `PARTIAL` should be used for unresolved warnings and mapping gaps; `FAILED` for blocking errors; and `VALIDATED` only when all required checks pass. Keep a list of individual warnings and errors regardless of aggregate status.

Validation rules should include:

- context/type compatibility and period boundaries;
- dimensional consistency and correct consolidation basis;
- units, signs, scale/decimals, nils, duplicates, and conflicting facts;
- statement arithmetic and applicable cross-statement reconciliations;
- known taxonomy calculation relationships where authoritative;
- plausibility checks and ratio anomalies (flag only; do not rewrite values);
- coverage reporting that distinguishes “not reported in this filing” from “failed to parse/normalize”.

A reported ratio such as debt-equity or DSCR that looks anomalous must be preserved as reported, marked for review, and excluded from trusted use if the relevant validation policy says so. Never silently rescale it to make it look reasonable.

### 6. A hard trusted-data gate for ML and APIs

Before feature engineering, create a single documented eligibility function/view that excludes facts from unsupported taxonomy versions, missing required metadata, failed context/unit/dimension checks, unapproved mappings, and unresolved blocking anomalies. It must be impossible for downstream ML code to read provisional/quarantined facts accidentally.

For each trusted value, retain provenance at least to filing ID/version/hash, source fact ID, QName/namespace, taxonomy package/version, context dates, dimensions, unit, canonical mapping version, and validation result. For historical backtests, preserve the date a filing became publicly available and use only information available as of each prediction date. Never use subsequently published financials to construct earlier features.

## Rollout and completion gates

### Gate 0 — Correctness regressions

- Fake extension concept `RevenueFromOperations=999` cannot override a standard-taxon revenue value.
- Duration concept on instant context cannot populate quarterly revenue.
- Valid standard-taxon fact with supported namespace/period type continues to normalize.
- Raw source fact is retained for every rejected mapping.
- Warning-level quality findings cannot produce a misleading filing status of `VALIDATED`.

### Gate 1 — Taxonomy completeness

- Exact schemas and supporting linkbases are loaded for every taxonomy/version in the test matrix.
- Taxonomy version mismatch is zero for filings designated `TRUSTED`.
- Unknown taxonomy identities remain provisional; extension status is confirmed from schema evidence, not a guess.

### Gate 2 — Filing gold set

Start with RELIANCE Q1 FY2026–27 and FY2025–26, then build a reviewed gold set across diverse sectors and filing types (manufacturing/services, bank, NBFC, insurance, and REIT/InvIT). Compare selected facts and statement totals with the source filing. Record expected exceptions and reviewed mappings as fixtures so future changes cannot silently change outcomes.

### Gate 3 — Scale in controlled batches

Expand from approximately 10–20 diverse issuers to 100, then 500, then the full universe. At each stage, sample both common and edge cases: quarterly vs annual, consolidated vs standalone, original vs revised filing, dimensioned data, historical taxonomy versions, and sector-specific statements. Do not move to the next batch just because parsing completed.

### Gate 4 — Production trust criteria

A batch is eligible to advance only when:

- every downloaded filing has immutable source bytes and a verified hash;
- every trusted canonical fact has source provenance and a supported mapping;
- no quarantined or provisional facts enter trusted views/features;
- no trusted fact has an unresolved namespace, context period type, unit, or dimensional ambiguity;
- reconciliation breaks are zero unless explicitly explained and reviewed;
- unresolved financial facts and suspicious ratios are itemized, not hidden in an aggregate count;
- all automated unit/integration tests run in CI, with PostgreSQL integration tests executed against a dedicated test database (not silently skipped);
- the backfill can be re-run idempotently without duplicate facts or loss of filing history.

## First next actions

1. Load the exact official taxonomy schema for the namespace referenced by current 2026 filings and test its checksum/version; then build the versioned registry from the package instead of relying on the 2025 fallback.
2. Implement schemaRef/import graph handling to confirm issuer extensions and dimension semantics.
3. Add the database-backed `TRUSTED` eligibility view and make feature engineering depend on it.
4. Build the RELIANCE gold tests, then broaden to a sector-diverse 10–20 issuer set.
5. Configure CI with a dedicated PostgreSQL test database and require integration tests to execute before merge/release.
