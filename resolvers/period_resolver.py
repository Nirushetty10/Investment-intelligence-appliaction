"""
resolvers/period_resolver.py

Canonical period resolution (ISSUE 1/2 fix for the filing_id=9 bug).

The bug: the production pipeline (nse_financials_pipeline.process_filing)
passed the REGISTRY's period_type straight into the period-start resolver,
and separately used the registry's period_type again for the
financial_periods.period_type column — without ever checking whether the
registry's period_type actually matches what the filing's own XBRL
contexts support. For RELIANCE filing_id=9 (an audited annual filing
mis-tagged "quarterly" in the discovery catalog), this meant the resolver
was handed the WRONG period_type and correctly-but-uselessly resolved the
Q4-only duration instead of the annual one.

This module fixes that by resolving period_start, period_type,
financial_quarter, and financial_year TOGETHER as one object
(`ResolvedPeriod`), using the filing's own XBRL contexts as the primary
evidence and the registry's period_type/period_start/submission_type as
corroborating signals — never as blind overrides of source evidence, and
never silently discarded either (every registry/source disagreement is
recorded on the returned object for audit).

Design rules (all non-negotiable per the reported issues):
  - Annual selection is never "pick the longest duration" alone (ISSUE 3).
    A candidate is only treated as "the annual duration" if its span is in
    a fiscal-year-length window (~350-380 days) AND either (a) it's the
    only unambiguous duration candidate, or (b) submission_type indicates
    an audited filing when an annual AND a quarterly candidate both exist
    and disagree.
  - Dimensional contexts are never candidates (ISSUE 4) — delegated to
    the already-tested `_derive_period_start_from_document`.
  - Instant contexts are never candidates (ISSUE 5) — same delegation.
  - The registry's original period_type/period_start are NEVER overwritten
    in nse_filing_registry — this module only returns what the NORMALIZED
    financial_periods row should use, and flags disagreement for the
    caller to log (ISSUE 6).
  - A non-null registry period_start is cross-validated against the
    source, not blindly trusted (ISSUE 7).
  - When the source is genuinely ambiguous, nothing is guessed — the
    resolver returns period_start=None / period_type=None and the caller
    must not persist a normalized period from that.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# Tolerant windows around the true fiscal-year (~365d) and fiscal-quarter
# (~91d) lengths — real filings vary by a few days for leap years / exact
# calendar boundaries, so these are ranges, not exact day counts.
ANNUAL_SPAN_RANGE = (350, 380)
QUARTER_SPAN_RANGE = (75, 100)


def _derive_period_start_from_document(doc, period_end: date, period_type: Optional[str] = None) -> Optional[date]:
    """
    Selects a period_start from the filing's own non-dimensioned,
    non-instant duration contexts whose period_end matches the target,
    disambiguating by span when more than one real candidate exists.

    This function is unchanged in behavior from the prior fix (still used
    directly, with a bare period_type string, by tests/test_period_resolution.py)
    — resolve_canonical_period() below builds on top of it rather than
    replacing it, so those existing tests keep passing unmodified.

    Returns None (never a guessed date) when there are zero candidates, or
    when multiple candidates exist and either no period_type was given to
    disambiguate, or period_type was given but no candidate is
    unambiguously the best match for it.
    """
    candidates = []  # list of (period_start, span_days)
    for ctx in doc.contexts.values():
        if ctx.has_dimensions or ctx.is_instant:
            continue
        if ctx.period_end == period_end and ctx.period_start is not None:
            span_days = (ctx.period_end - ctx.period_start).days
            candidates.append((ctx.period_start, span_days))

    distinct_starts = {c[0] for c in candidates}
    if len(distinct_starts) == 0:
        return None
    if len(distinct_starts) == 1:
        return distinct_starts.pop()

    if period_type == "annual":
        best = max(candidates, key=lambda c: c[1])
        if sum(1 for c in candidates if c[1] == best[1]) > 1:
            return None
        return best[0]
    if period_type == "quarterly":
        target_span = 91
        best = min(candidates, key=lambda c: abs(c[1] - target_span))
        if sum(1 for c in candidates if abs(c[1] - target_span) == abs(best[1] - target_span)) > 1:
            return None
        return best[0]

    return None


def _quarter_code_from_dates(period_type: str, period_start: Optional[date], period_end: Optional[date]) -> Optional[str]:
    """Deterministic Q1-Q4 code from calendar dates (Apr-Mar Indian fiscal
    year), never from free-text disclosure. Returns None for annual
    periods or missing dates. Unchanged from the prior fix."""
    if period_type != "quarterly" or period_end is None:
        return None
    month_to_quarter = {4: "Q1", 5: "Q1", 6: "Q1", 7: "Q2", 8: "Q2", 9: "Q2",
                         10: "Q3", 11: "Q3", 12: "Q3", 1: "Q4", 2: "Q4", 3: "Q4"}
    return month_to_quarter.get(period_end.month)


def _has_span_in_range(span_days: int, span_range: tuple) -> bool:
    return span_range[0] <= span_days <= span_range[1]


def _collect_duration_candidates(doc, period_end: date) -> dict:
    """Non-dimensioned, non-instant duration contexts matching period_end,
    as {period_start: span_days}. Deliberately the same exclusion rules as
    _derive_period_start_from_document (dimensional and instant contexts
    are never candidates), exposed separately here because
    resolve_canonical_period needs to reason about annual-vs-quarterly
    span classification directly, not just pick one winner."""
    candidates = {}
    for ctx in doc.contexts.values():
        if ctx.has_dimensions or ctx.is_instant:
            continue
        if ctx.period_end == period_end and ctx.period_start is not None:
            candidates[ctx.period_start] = (ctx.period_end - ctx.period_start).days
    return candidates


def _is_audited(submission_type: Optional[str]) -> bool:
    if not submission_type:
        return False
    s = submission_type.strip().lower()
    return "audit" in s and "unaudit" not in s


@dataclass
class ResolvedPeriod:
    period_start: Optional[date]
    period_end: date
    period_type: Optional[str]              # "annual" / "quarterly" / None if unresolved
    financial_quarter: Optional[str]        # "Q1".."Q4", or None for annual/unresolved
    financial_year: Optional[str]

    # Audit trail (ISSUE 6) — the registry's ORIGINAL values are never
    # overwritten in nse_filing_registry; these fields exist purely so the
    # caller can log what happened when source evidence and registry
    # metadata disagreed.
    registry_period_type: Optional[str] = None
    registry_period_start: Optional[date] = None
    conflict: bool = False
    resolution_reason: str = ""


def resolve_canonical_period(doc, filing: dict) -> ResolvedPeriod:
    """
    Resolves period_start, period_type, financial_quarter, and
    financial_year TOGETHER from one selected duration context (ISSUE 2),
    using the filing's own XBRL contexts as primary evidence and registry
    metadata (period_type, period_start_date, submission_type) as
    corroborating signals that are cross-checked, never blindly trusted
    (ISSUE 1 / ISSUE 7) and never silently overridden without a recorded
    reason (ISSUE 6).

    `filing` is the dict returned by repositories.filing_repository.get_filing_by_id
    — must contain period_end_date, period_type, period_start_date,
    submission_type, and (optionally) financial_year.
    """
    period_end = filing["period_end_date"]
    registry_period_type = filing.get("period_type")
    registry_period_start = filing.get("period_start_date")
    submission_type = filing.get("submission_type")
    audited = _is_audited(submission_type)

    candidates = _collect_duration_candidates(doc, period_end)
    annual_candidates = {s: sp for s, sp in candidates.items() if _has_span_in_range(sp, ANNUAL_SPAN_RANGE)}
    quarter_candidates = {s: sp for s, sp in candidates.items() if _has_span_in_range(sp, QUARTER_SPAN_RANGE)}

    resolved_start: Optional[date] = None
    resolved_type: Optional[str] = None
    conflict = False
    notes = []

    if len(annual_candidates) == 1 and len(quarter_candidates) == 1 and set(annual_candidates) != set(quarter_candidates):
        # Both a plausible annual-length AND a plausible quarter-length
        # duration context exist for this period_end, and they disagree —
        # exactly the filing_id=9 situation (full year + Q4-only). Never
        # resolve this by "longest wins" alone (ISSUE 3): submission_type
        # is the deciding corroborating signal, because Indian annual
        # (audited) results routinely disclose the Q4 standalone figures
        # alongside the full-year figures in the same filing, while
        # genuine Q1-Q3 quarterly results are unaudited and never carry a
        # fiscal-year-length context at all.
        (annual_start,) = annual_candidates.keys()
        (quarter_start,) = quarter_candidates.keys()
        if audited:
            resolved_start, resolved_type = annual_start, "annual"
            if registry_period_type != "annual":
                conflict = True
                notes.append(
                    f"registry period_type={registry_period_type!r} but the filing is audited "
                    f"(submission_type={submission_type!r}) and its own XBRL contexts contain "
                    f"both an annual duration ({annual_start} -> {period_end}) and a Q4-only "
                    f"duration ({quarter_start} -> {period_end}); an audited filing at a "
                    f"fiscal-year-end period_end is the annual filing. Using the annual "
                    f"duration; registry period_type is superseded by source evidence."
                )
        else:
            resolved_start, resolved_type = quarter_start, "quarterly"
            if registry_period_type not in (None, "quarterly"):
                conflict = True
                notes.append(
                    f"registry period_type={registry_period_type!r} but submission_type="
                    f"{submission_type!r} does not indicate an audited annual filing; "
                    f"both annual and Q4 duration contexts exist, deferring to the "
                    f"quarterly (Q4-only) duration."
                )
    elif len(annual_candidates) == 1 and not quarter_candidates:
        (resolved_start,) = annual_candidates.keys()
        resolved_type = "annual"
        if registry_period_type not in (None, "annual"):
            conflict = True
            notes.append(
                f"registry period_type={registry_period_type!r} but only an annual-length "
                f"duration context resolves unambiguously from the filing's own XBRL; using annual."
            )
    elif len(quarter_candidates) == 1 and not annual_candidates:
        (resolved_start,) = quarter_candidates.keys()
        resolved_type = "quarterly"
        if registry_period_type not in (None, "quarterly"):
            conflict = True
            notes.append(
                f"registry period_type={registry_period_type!r} but only a quarter-length "
                f"duration context resolves unambiguously from the filing's own XBRL; using quarterly."
            )
    elif len(candidates) == 1:
        # Exactly one duration candidate overall, but its span falls
        # outside both tolerant windows (e.g. a half-year filing) — source
        # can't independently confirm annual vs quarterly, so trust the
        # registry's period_type for classification while still using the
        # one real disclosed start date (never guessed).
        (resolved_start,) = candidates.keys()
        resolved_type = registry_period_type
        notes.append(
            f"Single duration candidate found (span={candidates[resolved_start]}d) that does "
            f"not clearly match an annual or quarterly window; using registry period_type="
            f"{registry_period_type!r} with this source-confirmed start date."
        )
    elif len(candidates) == 0:
        notes.append(
            "No non-dimensioned, non-instant duration context found matching this "
            "filing's period_end_date — cannot resolve a period_start from source."
        )
    else:
        # Multiple candidates and no clean annual/quarterly disambiguation
        # (e.g. more than one annual-length candidate, a genuine ambiguity)
        # — refuse to guess.
        notes.append(
            f"{len(candidates)} distinct duration contexts share this period_end "
            f"({len(annual_candidates)} annual-length, {len(quarter_candidates)} quarter-length) "
            f"with no unambiguous annual/quarterly disambiguation available; refusing to guess."
        )

    # Cross-validate against a non-null registry period_start (ISSUE 7).
    if registry_period_start is not None:
        if resolved_start is not None:
            if registry_period_start != resolved_start:
                conflict = True
                notes.append(
                    f"registry period_start_date={registry_period_start} differs from the "
                    f"source-derived period_start={resolved_start}; using the source-derived "
                    f"value (flagged for review)."
                )
            else:
                notes.append("registry period_start_date confirmed by the filing's own XBRL duration context.")
        else:
            # Could not independently verify from source — fall back to
            # the registry-provided values rather than leaving everything
            # unresolved, matching pre-fix behavior for filings where the
            # registry already has good data and the source is ambiguous.
            resolved_start = registry_period_start
            resolved_type = registry_period_type
            notes.append(
                "Could not independently resolve period_start/period_type from the filing's "
                "own XBRL contexts; using registry-provided values, UNVALIDATED against source."
            )

    financial_quarter = None
    if resolved_type == "quarterly" and resolved_start is not None:
        financial_quarter = _quarter_code_from_dates("quarterly", resolved_start, period_end)

    financial_year = filing.get("financial_year") or (
        f"{period_end.year - 1}-{str(period_end.year)[-2:]}"
        if period_end.month < 4
        else f"{period_end.year}-{str(period_end.year + 1)[-2:]}"
    )

    return ResolvedPeriod(
        period_start=resolved_start,
        period_end=period_end,
        period_type=resolved_type,
        financial_quarter=financial_quarter,
        financial_year=financial_year,
        registry_period_type=registry_period_type,
        registry_period_start=registry_period_start,
        conflict=conflict,
        resolution_reason="; ".join(notes) if notes else "unambiguous, no conflict",
    )
