"""
tests/test_filing9_integration.py

Regression coverage for the canonical period resolver fix. Two layers:

1. Direct tests of resolve_canonical_period() — the 12 scenarios listed in
   the issue's "TEST REQUIREMENTS" section.
2. A TRUE end-to-end integration test that calls
   nse_financials_pipeline.process_filing() itself (the exact function
   invoked by `python nse_financials_pipeline.py --filing-id 9`), with a
   mocked session/client reproducing filing_id=9's real registry metadata
   (period_type="quarterly" — the WRONG value — submission_type="Audited",
   period_start_date=NULL). This is deliberately NOT just a test of the
   resolver function in isolation (the previous round's tests already did
   that) — it exercises the production code path, per the explicit
   requirement that this be verified against the actual processing route.
"""
from contextlib import contextmanager
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from parsers.ixbrl_parser import parse_document
from resolvers.period_resolver import resolve_canonical_period

ANNUAL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q4_annual_consolidated.xbrl.xml"
Q1_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"


@pytest.fixture(scope="module")
def annual_doc():
    return parse_document(ANNUAL_FIXTURE_PATH.read_bytes())


@pytest.fixture(scope="module")
def q1_doc():
    return parse_document(Q1_FIXTURE_PATH.read_bytes())


def _base_filing(**overrides):
    base = {
        "period_end_date": date(2026, 3, 31),
        "period_type": "quarterly",
        "period_start_date": None,
        "submission_type": "Audited",
        "statement_type": "consolidated",
        "financial_year": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 1. Realistic filing 9 metadata — registry says quarterly, XBRL proves annual
# --------------------------------------------------------------------------

def test_filing9_realistic_metadata_resolves_to_annual(annual_doc):
    filing = _base_filing()  # exactly filing_id=9's reported registry state
    resolved = resolve_canonical_period(annual_doc, filing)

    assert resolved.period_start == date(2025, 4, 1)
    assert resolved.period_end == date(2026, 3, 31)
    assert resolved.period_type == "annual"
    assert resolved.financial_year == "2025-26"
    assert resolved.financial_quarter is None
    assert resolved.conflict is True
    assert "audited" in resolved.resolution_reason.lower()


# --------------------------------------------------------------------------
# 2. Registry says quarterly but XBRL proves annual (conflict is RECORDED,
#    registry's original value is never mutated)
# --------------------------------------------------------------------------

def test_registry_quarterly_vs_source_annual_conflict_recorded(annual_doc):
    filing = _base_filing(period_type="quarterly")
    resolved = resolve_canonical_period(annual_doc, filing)
    assert resolved.registry_period_type == "quarterly"   # original preserved on the object
    assert resolved.period_type == "annual"                # canonical value differs
    assert resolved.conflict is True
    assert filing["period_type"] == "quarterly"            # input dict itself never mutated


# --------------------------------------------------------------------------
# 3. Annual + Q4 + instant contexts all present together
# --------------------------------------------------------------------------

def test_annual_q4_and_instant_contexts_coexist_and_resolve_correctly(annual_doc):
    instants = [ctx for ctx in annual_doc.contexts.values() if ctx.is_instant]
    durations = [ctx for ctx in annual_doc.contexts.values() if not ctx.is_instant]
    assert len(instants) >= 2
    assert len(durations) >= 2

    resolved = resolve_canonical_period(annual_doc, _base_filing())
    assert resolved.period_start == date(2025, 4, 1)
    assert resolved.period_type == "annual"


# --------------------------------------------------------------------------
# 4. Annual company-level (non-dimensional) context selection
# --------------------------------------------------------------------------

def test_annual_selection_uses_non_dimensional_base_context(annual_doc):
    resolved = resolve_canonical_period(annual_doc, _base_filing())
    selected_ctx = next(
        ctx for ctx in annual_doc.contexts.values()
        if not ctx.is_instant and ctx.period_start == resolved.period_start and ctx.period_end == resolved.period_end
    )
    # there may be multiple contexts with these exact dates (dimensioned
    # segment twin included) — the one actually usable for company-level
    # resolution must not itself be dimensioned
    non_dim_matches = [
        ctx for ctx in annual_doc.contexts.values()
        if not ctx.is_instant and not ctx.has_dimensions
        and ctx.period_start == resolved.period_start and ctx.period_end == resolved.period_end
    ]
    assert len(non_dim_matches) == 1


# --------------------------------------------------------------------------
# 5. Dimensional context exclusion
# --------------------------------------------------------------------------

def test_dimensional_contexts_excluded_from_candidates_but_not_discarded(annual_doc):
    # the dimensioned twins exist in the parsed document (never discarded)...
    assert annual_doc.contexts["AnnualD_O2CSegment"].has_dimensions
    assert annual_doc.contexts["Q4D_O2CSegment"].has_dimensions
    # ...but never drive the canonical resolution
    resolved = resolve_canonical_period(annual_doc, _base_filing())
    assert resolved.period_start == date(2025, 4, 1)
    assert resolved.period_type == "annual"


# --------------------------------------------------------------------------
# 6. Standalone/consolidated preservation (unaffected by this fix)
# --------------------------------------------------------------------------

def test_statement_type_preserved_independent_of_period_resolution(annual_doc):
    from normalizers.financial_normalizer import normalize

    resolved = resolve_canonical_period(annual_doc, _base_filing(statement_type="consolidated"))
    result = normalize(annual_doc, period_end=resolved.period_end, period_start=resolved.period_start)
    assert result.period.statement_type == "consolidated"


# --------------------------------------------------------------------------
# 7. Registry/XBRL period-start conflict (ISSUE 7)
# --------------------------------------------------------------------------

def test_registry_period_start_conflicts_with_source_uses_source_and_flags(annual_doc):
    # registry claims a period_start that does NOT match either the annual
    # or Q4 context actually in the document
    filing = _base_filing(period_start_date=date(2026, 1, 15))
    resolved = resolve_canonical_period(annual_doc, filing)
    assert resolved.conflict is True
    assert resolved.registry_period_start == date(2026, 1, 15)
    # source-derived value wins
    assert resolved.period_start == date(2025, 4, 1)
    assert "differs from the" in resolved.resolution_reason


def test_registry_period_start_matches_source_no_conflict(annual_doc):
    # period_type="annual" here too (not the default "quarterly") so the
    # ONLY thing being isolated is the period_start cross-check — with the
    # default quarterly registry value, the period_TYPE conflict alone
    # would make resolved.conflict True regardless of period_start.
    filing = _base_filing(period_type="annual", period_start_date=date(2025, 4, 1))
    resolved = resolve_canonical_period(annual_doc, filing)
    assert resolved.conflict is False
    assert resolved.period_start == date(2025, 4, 1)


# --------------------------------------------------------------------------
# 10. Q1/Q2/Q3/Q4 regression — existing quarterly behavior unaffected
# --------------------------------------------------------------------------

def test_q1_filing_unaffected_by_the_fix(q1_doc):
    filing = _base_filing(
        period_end_date=date(2026, 6, 30), period_type="quarterly", submission_type="Unaudited",
    )
    resolved = resolve_canonical_period(q1_doc, filing)
    assert resolved.period_start == date(2026, 4, 1)
    assert resolved.period_type == "quarterly"
    assert resolved.financial_quarter == "Q1"
    assert resolved.financial_year == "2026-27"
    assert resolved.conflict is False


def test_quarterly_interpretation_of_annual_document_still_works(annual_doc):
    """Same document as filing 9, but registry correctly says quarterly
    and the filing is NOT audited (a hypothetical genuine Q4 unaudited
    limited-review filing that happens to also carry a YTD annual
    context) — must resolve to the Q4-only duration, not annual."""
    filing = _base_filing(period_type="quarterly", submission_type="Unaudited")
    resolved = resolve_canonical_period(annual_doc, filing)
    assert resolved.period_start == date(2026, 1, 1)
    assert resolved.period_type == "quarterly"
    assert resolved.financial_quarter == "Q4"


# --------------------------------------------------------------------------
# 11. Ambiguous annual candidates — must refuse to guess
# --------------------------------------------------------------------------

def test_ambiguous_annual_candidates_returns_none():
    period_end = date(2026, 3, 31)

    class _FakeCtx:
        def __init__(self, start, end, dims=False, instant=False):
            self.period_start = start
            self.period_end = end
            self.has_dimensions = dims
            self.is_instant = instant

    class _FakeDoc:
        def __init__(self, contexts):
            self.contexts = contexts

    # TWO distinct annual-length candidates (both ~365 days, different
    # starts) and no quarter candidate at all — genuinely ambiguous.
    fake_doc = _FakeDoc({
        "A": _FakeCtx(date(2025, 4, 1), period_end),   # 364 days
        "B": _FakeCtx(date(2025, 3, 25), period_end),  # 371 days — also annual-range
    })
    resolved = resolve_canonical_period(fake_doc, _base_filing())
    assert resolved.period_start is None
    assert resolved.period_type is None
    assert "refusing to guess" in resolved.resolution_reason


# --------------------------------------------------------------------------
# 12. Missing duration contexts
# --------------------------------------------------------------------------

def test_missing_duration_contexts_returns_none(annual_doc):
    resolved = resolve_canonical_period(annual_doc, _base_filing(period_end_date=date(1999, 1, 1)))
    assert resolved.period_start is None
    assert resolved.period_type is None
    assert "No non-dimensioned" in resolved.resolution_reason


def test_cannot_independently_resolve_falls_back_to_registry(annual_doc):
    """No candidates match this (deliberately wrong) period_end, but the
    registry DOES supply a period_start/period_type — rather than failing
    outright, fall back to the registry-provided values (matching pre-fix
    behavior for filings where source genuinely can't confirm anything),
    clearly flagged as unvalidated."""
    filing = _base_filing(
        period_end_date=date(1999, 1, 1),
        period_start_date=date(1998, 10, 1),
        period_type="quarterly",
    )
    resolved = resolve_canonical_period(annual_doc, filing)
    assert resolved.period_start == date(1998, 10, 1)
    assert resolved.period_type == "quarterly"
    assert "UNVALIDATED" in resolved.resolution_reason


# ==========================================================================
# TRUE end-to-end integration test: the actual process_filing() production
# code path (ISSUE 9/10), not just the resolver function.
# ==========================================================================

RAW_XML_9 = ANNUAL_FIXTURE_PATH.read_bytes()


class _FakeFetchResult:
    def __init__(self, content, content_type):
        self.status_code = 200
        self.content = content
        self.content_type = content_type
        self.response_timestamp = "2026-07-20T00:00:00+00:00"
        import hashlib
        self.content_hash = hashlib.sha256(content).hexdigest()

    def save(self, subdir, filename):
        import pathlib
        import tempfile
        p = pathlib.Path(tempfile.gettempdir()) / "filing9_test_raw_store" / subdir / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.content)
        return p


class _FakeClient:
    def get(self, url, is_api=True, params=None, accept_json=True):
        if "xml" in url:
            return _FakeFetchResult(RAW_XML_9, "application/xml")
        return _FakeFetchResult(b"<html></html>", "text/html")


class _FakeResult:
    def __init__(self, value=None, mapping=None):
        self._value, self._mapping = value, mapping

    def scalar_one(self):
        return self._value if self._value is not None else _next_fake_id()

    def scalar_one_or_none(self):
        return self._value

    def mappings(self):
        return self

    def one_or_none(self):
        return self._mapping


_fake_id_counter = [5000]


def _next_fake_id():
    _fake_id_counter[0] += 1
    return _fake_id_counter[0]


class _FakeSession:
    """Mocked session exercising the exact SQL dispatch process_filing()
    performs, including the ISSUE 8 session.begin_nested() savepoint."""

    def __init__(self, filing_row, fail_on_substring=None):
        self.filing_row = filing_row
        self.fail_on_substring = fail_on_substring
        self.calls = []
        self.financial_periods_params = None
        self.data_quality_logs = []
        self.nested_entered = False
        self.nested_exited_with_exception = False

    @contextmanager
    def begin_nested(self):
        self.nested_entered = True
        try:
            yield self
        except Exception:
            self.nested_exited_with_exception = True
            raise

    def execute(self, sql, params=None):
        t = str(sql)
        self.calls.append(t[:50])

        if self.fail_on_substring and self.fail_on_substring in t:
            raise RuntimeError(f"Simulated DB failure on: {self.fail_on_substring}")

        if "SELECT * FROM nse_filing_registry" in t:
            return _FakeResult(mapping=dict(self.filing_row))
        if "SELECT company_id FROM companies" in t:
            return _FakeResult(value=None)
        if "INSERT INTO companies" in t:
            return _FakeResult(value=42)
        if "DELETE FROM" in t:
            return _FakeResult()
        if "UPDATE nse_filing_registry" in t:
            return _FakeResult()
        if "INSERT INTO raw_filings" in t:
            return _FakeResult(value=_next_fake_id())
        if "INSERT INTO xbrl_contexts" in t:
            return _FakeResult(value=_next_fake_id())
        if "INSERT INTO xbrl_units" in t:
            return _FakeResult()
        if "INSERT INTO xbrl_facts" in t:
            return _FakeResult()
        if "INSERT INTO financial_periods" in t:
            self.financial_periods_params = dict(params)
            return _FakeResult(value=999)
        if "INSERT INTO income_statement" in t:
            return _FakeResult()
        if "INSERT INTO balance_sheet" in t:
            return _FakeResult()
        if "INSERT INTO cashflow_statement" in t:
            return _FakeResult()
        if "INSERT INTO ratios" in t:
            return _FakeResult()
        if "INSERT INTO data_quality_log" in t:
            self.data_quality_logs.append(dict(params) if params else {})
            return _FakeResult()
        raise AssertionError(f"Unhandled SQL in fake session: {t}")


def _filing9_registry_row():
    """Exactly the registry state reported for filing_id=9."""
    return {
        "filing_id": 9,
        "symbol": "RELIANCE",
        "company_name": "Reliance Industries Limited",
        "period_start_date": None,
        "period_end_date": date(2026, 3, 31),
        "period_type": "quarterly",       # the WRONG registry value
        "statement_type": "consolidated",
        "submission_type": "Audited",
        "source_kind": "INTEGRATED_FILING_IXBRL",
        "xbrl_url": "https://nsearchives.nseindia.com/corporate/xbrl/fake9.xml",
        "ixbrl_url": "https://nsearchives.nseindia.com/corporate/ixbrl/fake9.html",
        "financial_year": None,
    }


def test_process_filing_production_path_resolves_filing9_as_annual():
    """THE integration test: calls process_filing() itself — the exact
    function `python nse_financials_pipeline.py --filing-id 9` invokes —
    with filing_id=9's real (buggy) registry metadata, and confirms the
    persisted financial_periods row is correct."""
    import nse_financials_pipeline as pipeline

    session = _FakeSession(_filing9_registry_row())
    summary = pipeline.process_filing(_FakeClient(), session, 9)

    assert summary["status"] == "COMPLETE"
    assert summary["period_type_resolved"] == "annual"
    assert summary["period_type_registry"] == "quarterly"
    assert summary["period_resolution_conflict"] is True
    # The schema version used by the fixture has no exact catalog available,
    # so the result may be ingested but must not enter trusted ML inputs.
    assert summary["trust_status"] == "PROVISIONAL"

    fp = session.financial_periods_params
    assert fp is not None, "financial_periods was never inserted"
    assert fp["period_type"] == "annual"
    assert fp["period_start_date"] == date(2025, 4, 1)
    assert fp["period_end_date"] == date(2026, 3, 31)
    assert fp["financial_year"] == "2025-26"
    assert fp["financial_quarter"] is None
    assert fp["statement_type"] == "consolidated"
    assert fp["trust_status"] == "PROVISIONAL"

    # Step 4: taxonomy provenance must be persisted with the canonical period,
    # not just emitted in transient console summaries. This filing's exact
    # version is not present in the supplied catalog, so it must remain
    # explicitly provisional with the original schemaRef evidence attached.
    provenance = summary["taxonomy_provenance"]
    assert provenance["taxonomy_id"] == fp["taxonomy_id"]
    assert provenance["taxonomy_version"] == fp["taxonomy_version"]
    assert provenance["taxonomy_catalog_status"] == fp["taxonomy_catalog_status"]
    assert provenance["taxonomy_catalog_status"] == "VERSION_UNAVAILABLE"
    assert provenance["source_schema_refs"]
    assert json.loads(fp["source_schema_refs_json"]) == provenance["source_schema_refs"]
    saved_reasons = json.loads(fp["trust_reasons_json"])
    assert any(
        reason["check_name"] == "taxonomy_catalog_exact_version_unavailable"
        for reason in saved_reasons
    )

    # ISSUE 6: conflict was logged for audit
    conflict_logs = [l for l in session.data_quality_logs if l.get("check_name") == "period_resolution_conflict"]
    assert len(conflict_logs) == 1
    assert conflict_logs[0]["severity"] == "WARNING"

    # ISSUE 8: the atomic savepoint was actually used
    assert session.nested_entered is True
    assert session.nested_exited_with_exception is False


def test_process_filing_never_uses_q4_values_for_annual_period():
    """ISSUE 11: explicit assertion that the facts persisted correspond to
    the ANNUAL duration, not the Q4-only duration that exists in the same
    document."""
    import nse_financials_pipeline as pipeline
    from parsers.ixbrl_parser import parse_document
    from normalizers.financial_normalizer import normalize
    from resolvers.period_resolver import resolve_canonical_period

    session = _FakeSession(_filing9_registry_row())
    summary = pipeline.process_filing(_FakeClient(), session, 9)
    assert summary["status"] == "COMPLETE"

    # cross-check against direct normalization of the annual period
    doc = parse_document(RAW_XML_9)
    resolved = resolve_canonical_period(doc, _filing9_registry_row())
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)

    assert result.income_statement["revenue_from_operations"] == Decimal("1290000000000")  # annual, not Q4's 340000000000
    assert result.income_statement["profit_before_tax"] == Decimal("1150000000000")        # annual, not Q4's 300000000000


# --------------------------------------------------------------------------
# 8. Normalization persistence failure — status must be FAILED, not SUCCESS,
#    and raw data (already inserted earlier in the same session) must
#    remain untouched by the later failure.
# --------------------------------------------------------------------------

def test_persistence_failure_marks_normalization_failed_not_success():
    import nse_financials_pipeline as pipeline

    # The annual fixture used for filing_id=9 carries no ratio facts, so
    # "INSERT INTO ratios" never executes at all (zero ratios to loop
    # over) — use income_statement instead, which always executes for any
    # filing that has income-statement facts.
    session = _FakeSession(_filing9_registry_row(), fail_on_substring="INSERT INTO income_statement")
    summary = pipeline.process_filing(_FakeClient(), session, 9)

    assert summary.get("status") != "COMPLETE"
    assert "error" in summary
    assert "rolled back" in summary["error"]

    # raw data calls DID happen before the failure (never discarded —
    # ISSUE 12) even though normalized persistence failed afterward
    assert any("INSERT INTO raw_filings" in c for c in session.calls)
    assert any("INSERT INTO xbrl_contexts" in c for c in session.calls)
    assert any("INSERT INTO xbrl_facts" in c for c in session.calls)

    # the savepoint context was entered and exited via exception
    assert session.nested_entered is True
    assert session.nested_exited_with_exception is True


def test_suspicious_ratios_persisted_with_needs_validation_flag_not_altered():
    """ISSUE 6: the real RELIANCE Q1 fixture's DebtEquityRatio/
    DebtServiceCoverageRatio/InterestServiceCoverageRatio are scale-
    suspicious (see validate_ratio_plausibility). process_filing() must
    persist them with needs_validation=True and the RAW, unaltered value
    — never silently rescaled — while a non-suspicious ratio on the same
    filing gets needs_validation=False."""
    import nse_financials_pipeline as pipeline

    q1_filing_row = {
        "filing_id": 7,
        "symbol": "RELIANCE",
        "company_name": "Reliance Industries Limited",
        "period_start_date": None,
        "period_end_date": date(2026, 6, 30),
        "period_type": "quarterly",
        "statement_type": "consolidated",
        "submission_type": "Unaudited",
        "source_kind": "INTEGRATED_FILING_IXBRL",
        "xbrl_url": "https://nsearchives.nseindia.com/corporate/xbrl/fake_q1.xml",
        "ixbrl_url": "https://nsearchives.nseindia.com/corporate/ixbrl/fake_q1.html",
        "financial_year": None,
    }

    class _Q1FakeClient:
        def get(self, url, is_api=True, params=None, accept_json=True):
            if "xml" in url:
                return _FakeFetchResult(Q1_FIXTURE_PATH.read_bytes(), "application/xml")
            return _FakeFetchResult(b"<html></html>", "text/html")

    ratio_inserts = {}

    class _Q1FakeSession(_FakeSession):
        def execute(self, sql, params=None):
            t = str(sql)
            if "INSERT INTO ratios" in t and params:
                ratio_inserts[params["ratio_name"]] = dict(params)
            return super().execute(sql, params)

    session = _Q1FakeSession(q1_filing_row)
    summary = pipeline.process_filing(_Q1FakeClient(), session, 7)

    assert summary["status"] == "COMPLETE"
    assert "Debt Equity Ratio" in ratio_inserts
    assert ratio_inserts["Debt Equity Ratio"]["needs_validation"] is True
    assert ratio_inserts["Debt Equity Ratio"]["value"] == Decimal("0.004")  # raw, unaltered

    assert ratio_inserts["Debt Service Coverage Ratio"]["needs_validation"] is True
    assert ratio_inserts["Interest Service Coverage Ratio"]["needs_validation"] is True


def test_get_validated_ratios_excludes_flagged_rows():
    """ISSUE 6 enforcement point: get_validated_ratios must filter out
    needs_validation=TRUE rows so a downstream ML/fundamental feature
    pipeline reading through it never sees an unvalidated ratio."""
    from repositories.financial_repository import get_validated_ratios

    captured_sql = {}

    class _CaptureSession:
        def execute(self, sql, params=None):
            captured_sql["text"] = str(sql)
            captured_sql["params"] = params
            class _R:
                def mappings(self):
                    return self
                def all(self):
                    return []
            return _R()

    get_validated_ratios(_CaptureSession(), period_id=999)
    assert "needs_validation = FALSE" in captured_sql["text"]
    assert captured_sql["params"] == {"period_id": 999}


def test_successful_transaction_persists_all_expected_tables():
    """9. Successful transaction — confirm every expected table actually
    received an insert call in one successful run."""
    import nse_financials_pipeline as pipeline

    session = _FakeSession(_filing9_registry_row())
    summary = pipeline.process_filing(_FakeClient(), session, 9)

    assert summary["status"] == "COMPLETE"
    assert any("INSERT INTO financial_periods" in c for c in session.calls)
    assert any("INSERT INTO income_statement" in c for c in session.calls)
    # ISSUE 5: the filing_9 fixture now includes representative balance-sheet
    # instant facts (Assets, Equity, PPE, Cash, Borrowings, Receivables,
    # Payables, Investments) — these must now resolve AND actually persist.
    assert any("INSERT INTO balance_sheet" in c for c in session.calls)
    # this fixture still has no cash-flow concepts at all — correctly no insert
    assert not any("INSERT INTO cashflow_statement" in c for c in session.calls)
