# Bug-fix notes — current stabilization pass

## Fixes applied in this snapshot

1. **Namespace eligibility before canonical mapping.** Facts cannot be mapped into canonical financial fields based only on local concept name. The standard taxonomy namespace must resolve; unresolved/unsupported namespaces fail closed and are surfaced as quality findings. Raw facts remain intact.
2. **iXBRL namespace prefix resolution.** Inline fact QNames are resolved using the XML element's namespace map so the parser stores the namespace URI instead of the textual prefix (for example, `in-capmkt`).
3. **Taxonomy `periodType` validation.** The normalization path checks an instant/duration context against a concept's taxonomy-derived period type. A duration-based revenue concept in an instant context cannot populate quarterly revenue.
4. **Taxonomy-version warning.** The supplied Ind AS schema catalog is version `2025-01-31` but the current 2026 filing namespace is `2026-01-31`; this mismatch is surfaced and prevents a filing from being described as fully trusted.
5. **Validation-status semantics.** Warning-level findings now result in `PARTIAL`; errors remain `FAILED`; no findings gives `VALIDATED` subject to all checks actually being present and complete.
6. **Pipeline-level quality findings.** Taxonomy/normalization guards and unresolved financial-fact counts are converted to validation issues so they affect the filing status and quality log.
7. **Coverage diagnostics.** Reporting includes unrecognized namespace counts.
8. **Regression tests.** Added tests for extension spoofing, wrong context period type, unsupported namespaces, raw-fact preservation, status semantics, and full URI parsing. Updated synthetic fixtures to use full namespace URIs.

## Validation status

The latest full test run after these code changes reported **129 passed, 6 skipped**. The six skipped tests are PostgreSQL integration tests requiring a reachable, configured `NSE_DB_*` connection; they must not be reported as passed. See the release notes in the conversation for the exact test result.

## Known remaining blockers (not hidden)

- Exact 2026 schema/linkbases have not yet been loaded; the checked-in periodType catalog derives from the supplied 2025 Ind AS XSD. The mismatch warning is intentional. Do not call 2026 filings fully trusted until their matching taxonomy is loaded and verified.
- SchemaRef/import-based namespace-extension identification is not yet complete. Unsupported namespaces are safely prevented from populating canonical fields, but the project should later positively classify true issuer extensions and maintain reviewed extension mappings.
- Dimension interpretation is not sophisticated enough to classify every dimensional fact accurately as a business segment. Use axis/member semantics before treating dimensioned facts as segment reporting.
- A definitive database trusted-data view/API gate for analytics and ML remains a next step; a filing status alone is insufficient to filter every fact safely.
- PostgreSQL integration tests remain unexecuted in the current environment.
