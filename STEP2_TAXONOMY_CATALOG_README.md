# NSE Pipeline — Step 2: Versioned Taxonomy Catalog

## What this patch changes

- Adds an offline importer for SEBI XBRL taxonomy ZIPs (`taxonomy/catalog_importer.py`).
- Includes a versioned catalog generated from the seven taxonomy packages supplied with the project.
- Preserves concept QName, source namespace, datatype/type QName, base type, numeric/non-numeric classification, `periodType`, balance, abstract/nillable flags, labels, concept relationships, package hashes, and per-file hashes.
- Adds exact-version lookup and confirmation APIs; taxonomy identity, taxonomy concept presence, and canonical mapping approval remain separate concepts.
- Uses exact taxonomy metadata when the exact family/version is in the catalog. If the matching version is unavailable, the existing period-type list is only a defensive fallback and a quality warning is emitted.
- Refuses to map a known-version alias into a canonical financial field when the concept is absent from that exact version's imported schema.
- Adds regression tests for catalog completeness, concept metadata, version mismatch, and strict mapping behavior.

## Imported sources and coverage

The bundled catalog was generated from these supplied SEBI XBRL taxonomy packages:

| Taxonomy family | Imported version | Core XSD concepts |
|---|---:|---:|
| Ind AS — other than banks | 2025-01-31 | 507 |
| Other than banks | 2025-01-31 | 434 |
| Banking | 2025-01-31 | 404 |
| NBFC | 2025-01-31 | 517 |
| General insurance | 2025-01-31 | 519 |
| Life insurance | 2025-01-31 | 595 |
| REITs / InvITs | 2026-01-31 | 763 |

Each record includes the SHA-256 hash of its original source package. The importer does not download or dereference external schema URLs. It reads the source XSD and linkbase XML inside the ZIP.

## Important limitation

The supplied package set does **not** include an exact `ind_as_other_than_banks@2026-01-31` XSD catalog. The RELIANCE 2026 filing can still be parsed and guarded using the legacy period type fallback, but the normalizer adds `taxonomy_catalog_exact_version_unavailable`. Do not treat this as proof that the 2025 and 2026 taxonomies are identical, and do not promote such filings into fully trusted ML data until the matching version package has been verified/imported.

A sector catalog being present does not automatically enable canonical mappings for that sector. The current mapping-enable policy remains unchanged; sector mappings require separate validation and tests.

## Apply

Back up or checkpoint the working tree, then extract this ZIP into the project root so the included paths merge with the existing `taxonomy/`, `normalizers/`, and `tests/` directories.

Run from PowerShell:

```powershell
cd D:\Niranjan\Investment\nse_pipeline
python -m pytest tests/ -v --tb=short
```

The complete test suite in the isolated build environment completed with **149 passed and 6 database tests skipped** because no local PostgreSQL test credentials were configured there. Your local PostgreSQL configuration should allow those six tests to execute rather than skip.

## Rebuild the catalog from new official packages

When new official SEBI taxonomy ZIPs become available, run the importer against the official taxonomy-package ZIPs (the ones containing `META-INF/taxonomyPackage.xml` and `core/in-capmkt.xsd`):

```powershell
python -m taxonomy.catalog_importer `
  "D:\path\to\Taxonomy_Integrated_Filing_IndAS.zip" `
  "D:\path\to\Taxonomy_Integrated_Filing_Banking.zip" `
  --output taxonomy/taxonomy_catalog.json
```

Supply all relevant sector packages in one command to rebuild the complete bundled catalog. Review the printed taxonomy family, version, concept count, period-type count, relationship count, and hashes before replacing the catalog used in a release. The template utility `.xlsm` files are not taxonomy-package ZIPs and are not valid inputs to this importer.
