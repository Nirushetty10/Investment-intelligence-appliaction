# NSE Financial Data Ingestion Pipeline — Phase 1 Scaffold

This is **Phase 1** of the 6-phase build the spec itself lays out (§46):
get one company, one recent quarter, one source (Integrated Filing / iXBRL)
100% correct — before ever looping over years, companies, or the full NSE
universe. Nothing here has been run against the live NSE site; see
**"What is NOT verified"** below before you point this at production data.

## What's here and what's proven to work

```
config/settings.py            DB / NSE endpoint / retry-rate-limit config
db/schema.sql                 Full PostgreSQL schema (every table from the spec)
db/connection.py              SQLAlchemy engine + schema init helper
sources/nse_source.py         Session-aware NSE HTTP client (retry/backoff/429/5xx handling)
discovery/filing_discovery.py Filing discovery + registration, with mandatory diagnostics
parsers/ixbrl_parser.py       Generic inline-XBRL parser (context/unit/fact aware)
normalizers/concept_map.py    XBRL concept -> normalized field mapping (data, not code branches)
normalizers/financial_normalizer.py   Structured facts -> normalized statement, never guesses
validators/financial_validator.py     Reconciliation checks with tolerance
repositories/*.py             Explicit, NULL-safe upserts into every table
nse_financials_pipeline.py    CLI entrypoint (discovery wired; download/parse loop stubbed — see below)
tests/test_ixbrl_parser.py    12 passing tests against a real RELIANCE filing
```

**Verified by running `python3 -m pytest tests/ -v` (12/12 pass):**
the parser correctly extracts contexts (duration + instant), correctly
distinguishes a dimensioned (segment) context from the whole-company one,
correctly applies `scale`/`sign` to reproduce the exact rupee values from
RELIANCE's real Q1 FY27 (quarter ended 30-Jun-2026) Consolidated Integrated
Filing — revenue, other income, total income, PBT, current/deferred/total
tax, profit for period, EPS, and reported ratios all match the published
filing. A concept present in the filing but absent from the concept map
(`amortization`) correctly comes back as `None`, not `0`. A segment-level
revenue fact correctly does *not* leak into the company-wide revenue field.

## Provenance of the test fixture — please read before trusting it fully

I don't have live network access to `nseindia.com` / `nsearchives.nseindia.com`
from the environment this code was written in (egress there is restricted to
package registries and GitHub). I *could* use my search/fetch tools to pull
the real filing's **rendered content** — and did: the figures in
`tests/fixtures/reliance_2026q1_consolidated_ixbrl.html` are the actual
published numbers from
`https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_175608_17072026195004_iXBRL_WEB.html`
(RELIANCE, quarter ended 30-Jun-2026, Consolidated, Unaudited) — but that
fetch returns rendered/converted content, not the raw `ix:nonFraction`-tagged
markup. So the fixture is a **reconstruction**: real figures, genuine iXBRL
structure, but not a byte-for-byte copy of NSE's actual file.

**Before treating Phase 1 as done (per spec §47), you should:**
1. Run this code from a machine with normal internet access.
2. Download the actual raw HTML from a live Integrated Filing URL (discovery
   will find these once the endpoint/params below are confirmed).
3. Replace the fixture with that raw file and re-run
   `pytest tests/test_ixbrl_parser.py` unchanged. If it still passes, the
   parser is validated against NSE's real byte-for-byte output, not just a
   faithful reconstruction.

## What is NOT verified (needs a live NSE connection to confirm)

- **Discovery endpoint/params** (`discovery/filing_discovery.py`,
  `config/settings.py: NSE.integrated_filings`): the URL and query
  parameters follow NSE's publicly observed pattern but were never
  round-tripped against a live response. Run with `--discovery-only` first
  and read the diagnostic trace it prints — it shows the actual JSON keys
  NSE returns, request/response counts at each filter step (spec §41), so
  you can fix field names in `registry_entry_from_catalog_row` in one place
  if they don't match.
- **Session/cookie handling** in `NSEClient._warm_up`: NSE's anti-bot
  behavior changes periodically; you may need to add more realistic headers
  or a short random delay after warm-up if you get 403s.
- **Balance sheet / cash flow concept names** in `concept_map.py`: only
  spot-mapped, not validated against a real filing the way the income
  statement was (I didn't have a full balance sheet fixture to check
  against). Treat these as a starting point, not verified.
- **Bank/NBFC/insurance taxonomy**: `concept_map.py` covers the general
  commercial & industrial Ind-AS taxonomy only, per its own docstring.
  HDFCBANK/ICICIBANK/SBIN (in the Phase-5 test set) use a different XBRL
  taxonomy and need their own concept map before they'll normalize
  correctly — don't assume they'll "just work".
- **Legacy (pre-2023) filing formats**: `COVERAGE.provisional_integrated_filing_start`
  is a placeholder, not a discovered fact. Spec §40 requires the real
  transition date be determined empirically per company; this hasn't been
  done.

## How to actually run this

```bash
pip install -r requirements.txt

# 1. Point config/settings.py (or env vars NSE_DB_*) at a real Postgres instance
python nse_financials_pipeline.py --init-db

# 2. Discovery only, single company, to validate the endpoint against reality
python nse_financials_pipeline.py --symbols RELIANCE --start-year 2026 --end-year 2026 --discovery-only

# 3. Once discovery is confirmed working, extend main() in
#    nse_financials_pipeline.py to call, per registered filing:
#    sources.nse_source -> parsers.ixbrl_parser.parse_document
#      -> normalizers.financial_normalizer.normalize
#      -> validators.financial_validator.*
#      -> repositories.filing_repository / financial_repository
#    (deliberately not auto-wired yet — see spec §46/§47: prove Phase 1
#    correct on one real filing before looping.)
```

## Design decisions worth knowing about

- **NULL vs 0**: every repository function and the normalizer pass `None`
  straight through as `None`; nothing defaults a missing field to zero.
- **No tag-alone lookups**: `ParsedXbrlDocument.non_dimensioned_fact_for_period`
  returns `None` (not a value) if a concept resolves to zero or more-than-one
  candidate fact for a period — ambiguity is never silently resolved by
  picking "the first one".
- **Standalone/Consolidated** is read from the filing's own
  `NatureOfReportStandaloneOrConsolidated` metadata fact, never inferred
  from filename or assumed.
- **Units** are stored exactly as declared on each fact; the `scale`
  attribute is applied per XBRL semantics (`value * 10**scale`), with no
  additional crore/lakh heuristics layered on top.
- **Raw storage**: `FetchResult.save()` never overwrites existing raw bytes
  under the same filename — a hash-suffixed copy is written instead, so
  revision history is never destroyed (spec §23/§32).
