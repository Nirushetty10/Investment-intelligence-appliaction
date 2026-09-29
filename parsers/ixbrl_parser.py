"""
parsers/ixbrl_parser.py

Parses NSE's inline-XBRL (iXBRL) filings — HTML documents with embedded
<ix:nonFraction>, <ix:nonNumeric> etc. tags — into a fully structured,
context-and-unit-aware fact set. Also handles plain (non-inline) XBRL
instance documents (<xbrli:context>, raw fact elements), which NSE uses
for some older/legacy filings.

Hard rules enforced by this module (spec §10–§12):
  * We never do `find_tag("RevenueFromOperations")` and treat the result
    as THE revenue. Every fact is extracted together with its full
    context (period/instant + dimensions), unit, scale, sign and decimals.
    Picking "the" company-wide, non-dimensioned, correct-period value for
    a normalized field is the normalizer's job (financial_normalizer.py),
    operating over this structured fact table — never this parser's job.
  * Units are stored exactly as declared. No crore/lakh/INR assumption is
    made here. The `scale` attribute (if present) is recorded but NOT
    silently multiplied into numeric_value beyond what XBRL itself defines
    (value * 10**scale) — see _apply_scale_and_sign.
  * Dimensioned facts (segment/scenario) are kept distinguishable via
    dimensions_json; they are never conflated with the non-dimensioned,
    whole-company fact for the same concept+period.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from lxml import etree

IX_NS = "http://www.xbrl.org/2013/inlineXBRL"
XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRL_INSTANCE_ROOT_TAGS = {"xbrl", "{http://www.xbrl.org/2003/instance}xbrl"}


@dataclass
class XbrlContext:
    context_ref: str
    entity_identifier: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    instant_date: Optional[date] = None
    dimensions: dict = field(default_factory=dict)

    @property
    def is_instant(self) -> bool:
        return self.instant_date is not None

    @property
    def has_dimensions(self) -> bool:
        return bool(self.dimensions)


@dataclass
class XbrlUnit:
    unit_ref: str
    measure: str


@dataclass
class XbrlFact:
    context_ref: str
    namespace: Optional[str]
    concept: str
    raw_tag: str
    raw_value: Optional[str]
    numeric_value: Optional[Decimal]
    unit_ref: Optional[str]
    decimals: Optional[str]
    scale: Optional[int]
    sign: Optional[str]
    dimensions: dict = field(default_factory=dict)


@dataclass
class ParsedXbrlDocument:
    contexts: dict          # context_ref -> XbrlContext
    units: dict              # unit_ref -> XbrlUnit
    facts: list               # list[XbrlFact]
    source_format: str       # "IXBRL_HTML" or "XBRL_XML"

    def facts_for_concept(self, concept_local_name: str) -> list:
        """Every fact matching this local concept name, across ALL contexts
        and dimensions. Deliberately does not collapse to one value — that
        decision belongs to the normalizer, which must pick the right
        context (correct period, no dimensions) explicitly."""
        return [f for f in self.facts if f.concept == concept_local_name]

    def non_dimensioned_fact_for_period(
        self, concept_local_name: str, period_end: date, period_start: Optional[date] = None
    ) -> Optional[XbrlFact]:
        """Find the single whole-company (no segment/scenario dimensions)
        fact for a concept matching an exact period. Returns None rather
        than guessing if zero or multiple ambiguous matches exist — the
        caller must treat None as 'not available', not as zero."""
        candidates = []
        for f in self.facts_for_concept(concept_local_name):
            ctx = self.contexts.get(f.context_ref)
            if ctx is None or ctx.has_dimensions:
                continue
            if ctx.is_instant:
                if ctx.instant_date == period_end:
                    candidates.append(f)
            else:
                if ctx.period_end == period_end and (
                    period_start is None or ctx.period_start == period_start
                ):
                    candidates.append(f)
        if len(candidates) == 1:
            return candidates[0]
        return None  # ambiguous or absent — normalizer must NULL the field, not guess


def _parse_date(value: str) -> Optional[date]:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _namespace_of(tag: str) -> Optional[str]:
    if tag.startswith("{"):
        return tag[1:].split("}")[0]
    return None


def _clean_number_text(text: str) -> str:
    """Strip thousands separators / parenthesis-negative / whitespace, but
    do NOT interpret magnitude — that is what `scale` is for."""
    text = text.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").strip()
    if negative and not text.startswith("-"):
        text = "-" + text
    return text


def _apply_scale_and_sign(raw_text: str, scale: Optional[int], sign: Optional[str]) -> Optional[Decimal]:
    cleaned = _clean_number_text(raw_text)
    if cleaned in ("", "-", "—", "–"):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if scale:
        value = value * (Decimal(10) ** scale)
    if sign == "-":
        value = -value
    return value


def parse_ixbrl_contexts_and_units(root: etree._Element) -> tuple:
    """Parses <xbrli:context> and <xbrli:unit> elements, which appear
    identically whether embedded in an iXBRL HTML <head>/<body> or in a
    standalone XBRL instance document."""
    contexts: dict = {}
    units: dict = {}

    for ctx_el in root.iter(f"{{{XBRLI_NS}}}context"):
        context_ref = ctx_el.get("id")
        if not context_ref:
            continue
        entity_id_el = ctx_el.find(f".//{{{XBRLI_NS}}}identifier")
        entity_identifier = entity_id_el.text.strip() if entity_id_el is not None and entity_id_el.text else None

        period_el = ctx_el.find(f"{{{XBRLI_NS}}}period")
        period_start = period_end = instant_date = None
        if period_el is not None:
            instant_el = period_el.find(f"{{{XBRLI_NS}}}instant")
            start_el = period_el.find(f"{{{XBRLI_NS}}}startDate")
            end_el = period_el.find(f"{{{XBRLI_NS}}}endDate")
            if instant_el is not None and instant_el.text:
                instant_date = _parse_date(instant_el.text)
            if start_el is not None and start_el.text:
                period_start = _parse_date(start_el.text)
            if end_el is not None and end_el.text:
                period_end = _parse_date(end_el.text)

        dimensions = {}
        # scenario lives under <period>, segment lives under <entity> — both
        # are descendants of the context element, not direct children, so
        # a plain (non-recursive) find() would miss segment entirely.
        scenario_el = ctx_el.find(f".//{{{XBRLI_NS}}}scenario")
        segment_el = ctx_el.find(f".//{{{XBRLI_NS}}}segment")
        for container in (scenario_el, segment_el):
            if container is None:
                continue
            for member_el in container:
                # typically xbrldi:explicitMember with @dimension attribute
                dim_name = member_el.get("dimension")
                dim_value = (member_el.text or "").strip()
                if dim_name:
                    dimensions[_local_name(dim_name)] = dim_value

        contexts[context_ref] = XbrlContext(
            context_ref=context_ref,
            entity_identifier=entity_identifier,
            period_start=period_start,
            period_end=period_end,
            instant_date=instant_date,
            dimensions=dimensions,
        )

    for unit_el in root.iter(f"{{{XBRLI_NS}}}unit"):
        unit_ref = unit_el.get("id")
        if not unit_ref:
            continue
        measure_el = unit_el.find(f".//{{{XBRLI_NS}}}measure")
        measure = measure_el.text.strip() if measure_el is not None and measure_el.text else "UNKNOWN"
        # strip namespace prefix from measure display (e.g. "iso4217:INR" -> "INR")
        # but keep it recognizable; we don't assume meaning beyond the label.
        units[unit_ref] = XbrlUnit(unit_ref=unit_ref, measure=measure.split(":")[-1])

    return contexts, units


def parse_ixbrl_facts(root: etree._Element) -> list:
    """Extract every <ix:nonFraction> and <ix:nonNumeric> element with its
    full attribute set. Nothing here decides which fact 'is' revenue —
    that's the normalizer's job."""
    facts = []
    for tag_name in ("nonFraction", "nonNumeric"):
        for el in root.iter(f"{{{IX_NS}}}{tag_name}"):
            name_attr = el.get("name")  # e.g. "in-capmkt:RevenueFromOperations"
            context_ref = el.get("contextRef")
            if not name_attr or not context_ref:
                continue
            namespace_prefix, _, local_concept = name_attr.partition(":")
            if not local_concept:
                local_concept = namespace_prefix
                namespace_prefix = None

            unit_ref = el.get("unitRef")
            decimals = el.get("decimals")
            sign = el.get("sign")
            scale_attr = el.get("scale")
            scale = int(scale_attr) if scale_attr not in (None, "") else None

            raw_text = "".join(el.itertext())
            if tag_name == "nonFraction":
                numeric_value = _apply_scale_and_sign(raw_text, scale, sign)
            else:
                numeric_value = None  # nonNumeric = textual disclosure, not a number

            # dimensions on the fact itself are inherited from its context;
            # we don't duplicate parsing here, the normalizer joins on context_ref.
            facts.append(
                XbrlFact(
                    context_ref=context_ref,
                    namespace=namespace_prefix,
                    concept=local_concept,
                    raw_tag=name_attr,
                    raw_value=raw_text.strip() if raw_text else None,
                    numeric_value=numeric_value,
                    unit_ref=unit_ref,
                    decimals=decimals,
                    scale=scale,
                    sign=sign,
                )
            )
    return facts


def parse_plain_xbrl_facts(root: etree._Element, contexts: dict) -> list:
    """For standalone (non-inline) XBRL instance documents: every direct
    child of the root with a contextRef attribute is a fact."""
    facts = []
    for el in root:
        context_ref = el.get("contextRef")
        if not context_ref:
            continue
        tag = el.tag
        namespace = _namespace_of(tag)
        concept = _local_name(tag)
        unit_ref = el.get("unitRef")
        decimals = el.get("decimals")
        sign = el.get("sign")
        scale_attr = el.get("scale")
        scale = int(scale_attr) if scale_attr not in (None, "") else None
        raw_text = (el.text or "").strip()
        numeric_value = _apply_scale_and_sign(raw_text, scale, sign) if unit_ref else None
        facts.append(
            XbrlFact(
                context_ref=context_ref,
                namespace=namespace,
                concept=concept,
                raw_tag=f"{namespace}:{concept}" if namespace else concept,
                raw_value=raw_text or None,
                numeric_value=numeric_value,
                unit_ref=unit_ref,
                decimals=decimals,
                scale=scale,
                sign=sign,
            )
        )
    return facts


def parse_document(raw_bytes: bytes) -> ParsedXbrlDocument:
    """Entry point. Detects iXBRL-in-HTML vs plain XBRL XML and dispatches
    accordingly. Raises ValueError if neither pattern is recognized —
    caller must record this as a PARSE FAILURE, never fall back to
    scraping displayed numbers (spec: 'do not scrape displayed HTML
    numbers when XBRL is available')."""
    # iXBRL documents are XHTML — well-formed XML with namespace-qualified
    # tags (ix:, xbrli:, ...). lxml's HTML parser is NOT namespace-aware in
    # the way this parsing depends on (it silently mangles/drops custom
    # namespaced tags), so we parse as XML first. Only if that fails
    # outright do we fall back to the lenient HTML parser, in which case
    # inline-XBRL namespace tags will likely not be recoverable and the
    # "no contexts found" check below will correctly flag it as unparseable
    # rather than silently scraping displayed text.
    root = None
    try:
        root = etree.fromstring(raw_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        root = None

    if root is None:
        try:
            root = etree.fromstring(raw_bytes, parser=etree.HTMLParser(recover=True))
        except etree.XMLSyntaxError as exc:
            raise ValueError(f"Could not parse as XML/HTML: {exc}")

    if root is None:
        raise ValueError("Empty document tree")

    has_ix = any(True for _ in root.iter(f"{{{IX_NS}}}nonFraction")) or any(
        True for _ in root.iter(f"{{{IX_NS}}}nonNumeric")
    )
    contexts, units = parse_ixbrl_contexts_and_units(root)

    if not contexts:
        raise ValueError(
            "No xbrli:context elements found — this document is not a "
            "recognizable XBRL/iXBRL source. Do not fall back to scraping "
            "displayed HTML table values."
        )

    if has_ix:
        facts = parse_ixbrl_facts(root)
        source_format = "IXBRL_HTML"
    else:
        facts = parse_plain_xbrl_facts(root, contexts)
        source_format = "XBRL_XML"

    return ParsedXbrlDocument(contexts=contexts, units=units, facts=facts, source_format=source_format)


def _looks_like_html(raw_bytes: bytes) -> bool:
    head = raw_bytes[:2048].lower()
    return b"<html" in head or b"<!doctype html" in head
