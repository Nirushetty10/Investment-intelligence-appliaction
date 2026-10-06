"""
tests/test_discovery_classification.py

Regression tests for discovery/filing_discovery.py's
_classify_discovery_period_type — catalog-only (pre-XBRL-download)
period_type classification.

Two regressions covered here, in opposite directions:
  1. The ORIGINAL bug: every filing was hardcoded "quarterly", so audited
     annual filings were mis-registered.
  2. The REGRESSION introduced while fixing #1: a blank/missing
     reporting_quarter field combined with an audited submission_type was
     treated as "looks like annual", which caused real Q1/Q3 filings to be
     mis-classified "annual" whenever their quarter-field extraction
     happened to come back blank (most likely because discovery's guessed
     catalog field names didn't match NSE's actual JSON keys) while
     submission_type still said "Audited" (e.g. a company that gets a full
     audit on an interim quarter instead of a limited review).

The fix requires a POSITIVE, explicit Q1/Q2/Q3 or Q4/annual marker in the
text — never an inference from the field simply being blank.
"""
from datetime import date

from discovery.filing_discovery import _classify_discovery_period_type


def test_q1_unaudited_classified_quarterly():
    assert _classify_discovery_period_type("Unaudited", "First Quarter", date(2026, 6, 30)) == "quarterly"


def test_q2_unaudited_classified_quarterly():
    assert _classify_discovery_period_type("Unaudited", "Q2", date(2026, 9, 30)) == "quarterly"


def test_q3_unaudited_classified_quarterly():
    assert _classify_discovery_period_type("Unaudited", "Third Quarter", date(2026, 12, 31)) == "quarterly"


def test_q1_audited_still_classified_quarterly():
    """A company CAN fully audit an interim quarter instead of a limited
    review — that must NOT make it annual. Explicit Q1 marker wins
    regardless of audited status."""
    assert _classify_discovery_period_type("Audited", "Q1", date(2026, 6, 30)) == "quarterly"


def test_q3_audited_still_classified_quarterly():
    assert _classify_discovery_period_type("Audited", "Quarter 3", date(2026, 12, 31)) == "quarterly"


def test_audited_fourth_quarter_classified_annual():
    assert _classify_discovery_period_type("Audited", "Fourth Quarter", date(2026, 3, 31)) == "annual"


def test_audited_explicit_annual_marker_classified_annual():
    assert _classify_discovery_period_type("Audited", "Annual", date(2026, 3, 31)) == "annual"
    assert _classify_discovery_period_type("Audited", "Year Ended", date(2026, 3, 31)) == "annual"


def test_audited_q4_marker_classified_annual():
    assert _classify_discovery_period_type("Audited", "Q4", date(2026, 3, 31)) == "annual"


# --------------------------------------------------------------------------
# THE regression: blank/missing quarter field must NEVER imply annual,
# even when submission_type says audited.
# --------------------------------------------------------------------------

def test_audited_with_blank_quarter_field_defaults_to_quarterly_not_annual():
    """This is the exact regression: a previous version of this function
    treated (audited AND blank-quarter) as 'looks like annual', which
    mis-classified real Q1/Q3 filings whenever their quarter field
    extraction came back empty. Must default to quarterly, not guess
    annual from absence of a marker."""
    assert _classify_discovery_period_type("Audited", None, date(2026, 6, 30)) == "quarterly"
    assert _classify_discovery_period_type("Audited", "", date(2026, 9, 30)) == "quarterly"
    assert _classify_discovery_period_type("Audited", "   ", date(2026, 12, 31)) == "quarterly"


def test_unaudited_with_blank_quarter_field_defaults_to_quarterly():
    assert _classify_discovery_period_type("Unaudited", None, date(2026, 6, 30)) == "quarterly"


def test_no_submission_type_at_all_defaults_to_quarterly():
    assert _classify_discovery_period_type(None, None, date(2026, 3, 31)) == "quarterly"
    assert _classify_discovery_period_type(None, "Fourth Quarter", date(2026, 3, 31)) == "quarterly"  # not audited -> stays quarterly


def test_unaudited_with_annual_marker_text_stays_quarterly():
    """An explicit annual/Q4 marker ALONE, without audited status, is not
    enough — audited status is required corroboration (the whole point of
    requiring BOTH signals, not just one)."""
    assert _classify_discovery_period_type("Unaudited", "Fourth Quarter", date(2026, 3, 31)) == "quarterly"
    assert _classify_discovery_period_type("Limited Review", "Annual", date(2026, 3, 31)) == "quarterly"
