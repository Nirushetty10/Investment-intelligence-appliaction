"""
tests/test_period_resolution.py

Regression tests for the period-resolution bug exposed by filing_id=9: an
audited ANNUAL RELIANCE filing ending 2026-03-31 whose XBRL legitimately
carries BOTH a full-year duration context (2025-04-01 -> 2026-03-31) and a
Q4-only duration context (2026-01-01 -> 2026-03-31) sharing the same
period_end, plus instant contexts and dimensioned (segment) contexts that
also share those dates. The old resolver required exactly one candidate
and returned None (ambiguous) whenever more than one existed, incorrectly
stopping the pipeline at period-resolution for a filing that states its
own annual period perfectly clearly.

Fixture provenance: tests/fixtures/reliance_2026q4_annual_consolidated.xbrl.xml
is a reconstruction (I do not have the literal NSE download for filing_id=9)
built to match the exact context structure reported for that filing —
see the fixture file's own header comment.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_financials_pipeline import _derive_period_start_from_document, _quarter_code_from_dates
from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import parse_document

ANNUAL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q4_annual_consolidated.xbrl.xml"
Q1_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"

ANNUAL_PERIOD_END = date(2026, 3, 31)
EXPECTED_ANNUAL_START = date(2025, 4, 1)
EXPECTED_Q4_START = date(2026, 1, 1)


@pytest.fixture(scope="module")
def annual_doc():
    return parse_document(ANNUAL_FIXTURE_PATH.read_bytes())


@pytest.fixture(scope="module")
def q1_doc():
    return parse_document(Q1_FIXTURE_PATH.read_bytes())


# --------------------------------------------------------------------------
# Context structure sanity (confirms the fixture reproduces the reported bug)
# --------------------------------------------------------------------------

def test_fixture_has_ambiguous_duration_contexts_sharing_period_end(annual_doc):
    matching_duration_contexts = [
        ctx for ctx in annual_doc.contexts.values()
        if not ctx.is_instant and ctx.period_end == ANNUAL_PERIOD_END
    ]
    distinct_starts = {ctx.period_start for ctx in matching_duration_contexts}
    # at least the annual and Q4 non-dimensioned starts, plus their
    # dimensioned segment twins — multiple contexts, multiple distinct
    # start dates, all sharing the same period_end. This IS the ambiguity.
    assert EXPECTED_ANNUAL_START in distinct_starts
    assert EXPECTED_Q4_START in distinct_starts
    assert len(distinct_starts) >= 2


def test_fixture_has_instant_contexts_separate_from_durations(annual_doc):
    instants = [ctx for ctx in annual_doc.contexts.values() if ctx.is_instant]
    instant_dates = {ctx.instant_date for ctx in instants}
    assert date(2025, 3, 31) in instant_dates
    assert date(2026, 3, 31) in instant_dates
    # instants must never carry a period_start/period_end
    for ctx in instants:
        assert ctx.period_start is None
        assert ctx.period_end is None


# --------------------------------------------------------------------------
# Core fix: annual period selection prefers the full-year duration
# --------------------------------------------------------------------------

def test_annual_period_type_selects_full_year_not_q4(annual_doc):
    """THE bug from filing_id=9: must resolve to 2025-04-01, not None and
    not the Q4-only 2026-01-01."""
    derived = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "annual")
    assert derived == EXPECTED_ANNUAL_START


def test_quarterly_period_type_on_same_document_selects_q4_not_annual(annual_doc):
    """The same document, asked for the QUARTERLY interpretation of the
    same period_end, must select the short (Q4) span instead — proves the
    disambiguation is driven by period_type, not by luck or ordering."""
    derived = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "quarterly")
    assert derived == EXPECTED_Q4_START


def test_no_period_type_given_is_ambiguous_and_returns_none(annual_doc):
    """Without period_type to disambiguate, multiple genuinely different
    candidate spans must NOT be silently resolved — must return None,
    never guess."""
    derived = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, None)
    assert derived is None


# --------------------------------------------------------------------------
# Dimensional contexts must never be candidates, even with matching dates
# --------------------------------------------------------------------------

def test_dimensioned_contexts_never_selected_despite_matching_dates(annual_doc):
    """AnnualD_O2CSegment and Q4D_O2CSegment share EXACT dates with the
    real annual/Q4 contexts but are dimensioned (segment) — the resolver
    must ignore them entirely, for both period_type interpretations."""
    annual_ctx = annual_doc.contexts["AnnualD_O2CSegment"]
    q4_ctx = annual_doc.contexts["Q4D_O2CSegment"]
    assert annual_ctx.has_dimensions
    assert q4_ctx.has_dimensions

    # both resolutions still work correctly with these dimensioned
    # contexts present in the document — proof they were excluded, not
    # coincidentally skipped
    assert _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "annual") == EXPECTED_ANNUAL_START
    assert _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "quarterly") == EXPECTED_Q4_START


# --------------------------------------------------------------------------
# Existing quarterly (Q1) behavior must be unchanged (requirement 8)
# --------------------------------------------------------------------------

def test_q1_single_candidate_behavior_unchanged(q1_doc):
    """The RELIANCE Q1 fixture has exactly ONE non-dimensioned duration
    context matching its period_end — the original single-candidate fast
    path must still work exactly as before, regardless of period_type."""
    period_end = date(2026, 6, 30)
    assert _derive_period_start_from_document(q1_doc, period_end, "quarterly") == date(2026, 4, 1)
    # even an unrelated/wrong period_type must not change the answer when
    # there's genuinely only one candidate to begin with
    assert _derive_period_start_from_document(q1_doc, period_end, "annual") == date(2026, 4, 1)
    assert _derive_period_start_from_document(q1_doc, period_end, None) == date(2026, 4, 1)


# --------------------------------------------------------------------------
# Full normalization: correct statement resolved from correct context,
# not mixed up between annual and Q4 values that both exist in the file
# --------------------------------------------------------------------------

def test_annual_normalization_uses_annual_values_not_q4(annual_doc):
    period_start = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "annual")
    result = normalize(annual_doc, period_end=ANNUAL_PERIOD_END, period_start=period_start)

    # annual figures (1,290,000,000,000 / 1,150,000,000,000), NOT the Q4
    # figures (340,000,000,000 / 300,000,000,000) that also exist in the
    # same document under the same concept names.
    assert result.income_statement["revenue_from_operations"] == Decimal("1290000000000")
    assert result.income_statement["profit_before_tax"] == Decimal("1150000000000")


def test_quarterly_normalization_of_same_document_uses_q4_values(annual_doc):
    """Sanity check the inverse: asking for the quarterly interpretation
    of the identical document must pull the Q4-only values."""
    period_start = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "quarterly")
    result = normalize(annual_doc, period_end=ANNUAL_PERIOD_END, period_start=period_start)

    assert result.income_statement["revenue_from_operations"] == Decimal("340000000000")
    assert result.income_statement["profit_before_tax"] == Decimal("300000000000")


# --------------------------------------------------------------------------
# Standalone/consolidated resolution unaffected by the period-resolution fix
# --------------------------------------------------------------------------

def test_statement_type_still_resolves_correctly(annual_doc):
    period_start = _derive_period_start_from_document(annual_doc, ANNUAL_PERIOD_END, "annual")
    result = normalize(annual_doc, period_end=ANNUAL_PERIOD_END, period_start=period_start)
    assert result.period.statement_type == "consolidated"
    assert result.period.audited_status == "Audited"


# --------------------------------------------------------------------------
# Quarter-code derivation: annual periods must never get a Q-code
# --------------------------------------------------------------------------

def test_quarter_code_is_none_for_annual_period():
    code = _quarter_code_from_dates("annual", EXPECTED_ANNUAL_START, ANNUAL_PERIOD_END)
    assert code is None


def test_quarter_code_is_q4_for_the_quarterly_interpretation():
    code = _quarter_code_from_dates("quarterly", EXPECTED_Q4_START, ANNUAL_PERIOD_END)
    assert code == "Q4"


# --------------------------------------------------------------------------
# Missing/ambiguous handling: zero candidates, and a genuine span tie
# --------------------------------------------------------------------------

def test_zero_candidates_returns_none(annual_doc):
    """A period_end that matches nothing in the document at all."""
    derived = _derive_period_start_from_document(annual_doc, date(1999, 1, 1), "annual")
    assert derived is None


def test_genuine_span_tie_returns_none_not_an_arbitrary_pick():
    """Two candidates with DIFFERENT start dates but equal closeness to
    the period_type's expected span (91 days for quarterly) — a true tie.
    Since two different start dates against the same period_end always
    produce different absolute spans, an exact tie in *closeness to
    target* (not span itself) is what's tested here: 89 days and 93 days
    are both exactly 2 days off the 91-day target. Must return None, never
    pick arbitrarily (e.g. by dict/insertion order)."""
    period_end = date(2026, 3, 31)

    class _FakeCtx:
        def __init__(self, start, end):
            self.period_start = start
            self.period_end = end
            self.has_dimensions = False
            self.is_instant = False

    class _FakeDoc:
        def __init__(self, contexts):
            self.contexts = contexts

    fake_doc = _FakeDoc({
        "A": _FakeCtx(date(2026, 1, 1), period_end),    # 89 days -> |89-91| = 2
        "B": _FakeCtx(date(2025, 12, 28), period_end),  # 93 days -> |93-91| = 2 (ties with A)
    })
    result = _derive_period_start_from_document(fake_doc, period_end, "quarterly")
    assert result is None, "an exact tie in closeness-to-target span must return None, not an arbitrary pick"
