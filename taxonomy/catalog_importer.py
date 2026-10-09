"""Import versioned SEBI/NSE XBRL taxonomy packages into a local catalog.

This importer is deliberately offline: it reads only the supplied taxonomy ZIPs,
never follows schemaLocation URLs, and records SHA-256 provenance for source ZIPs.
The resulting catalog describes taxonomy concepts; it does NOT automatically
approve canonical financial mappings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from lxml import etree

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XBRLI_NS = "http://www.xbrl.org/2003/instance"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
TP_NS = "http://xbrl.org/2016/taxonomy-package"

FAMILIES = {
    "IntegratedFinance_IndAS": ("ind_as_other_than_banks", "SEBI Integrated Filing — Ind AS (Other than Banks)"),
    "IntegratedFinance_OtherThanBank": ("other_than_banks", "SEBI Integrated Filing — Other than Banks"),
    "IntegratedFinance_Banking": ("banking", "SEBI Integrated Filing — Banking"),
    "IntegratedFinance_NBFC": ("nbfc", "SEBI Integrated Filing — NBFC"),
    "IntegratedFinance_GI": ("general_insurance", "SEBI Integrated Filing — General Insurance"),
    "IntegratedFinance_LI": ("life_insurance", "SEBI Integrated Filing — Life Insurance"),
    "Financial_Results_REITs_InvITs": ("reits_invit", "SEBI Financial Results — REITs / InvITs"),
}


def _parser() -> etree.XMLParser:
    # No DTD/entity resolution or network access: taxonomy schemaLocation
    # attributes are provenance only and are not dereferenced by this tool.
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_xml(archive: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(archive.read(name), parser=_parser())


def _root_text(root: etree._Element, local_name: str) -> str | None:
    vals = root.xpath(f"//*[local-name()='{local_name}']/text()")
    return vals[0].strip() if vals and vals[0].strip() else None


def _family_from_package(entry_names: list[str], entry_namespaces: list[str]) -> tuple[str, str, str]:
    signals = "\n".join(entry_names + entry_namespaces)
    found = [(marker, values) for marker, values in FAMILIES.items() if marker in signals]
    if len(found) != 1:
        raise ValueError(
            "Could not uniquely identify SEBI taxonomy family from official entry-point paths/namespaces; "
            f"matched {[m for m, _ in found]}"
        )
    marker, (taxonomy_id, label) = found[0]
    return marker, taxonomy_id, label


def _base_type_name(qname: str | None) -> str | None:
    return qname.split(":", 1)[-1] if qname else None


def _load_type_definitions(type_root: etree._Element | None) -> dict[str, dict[str, Any]]:
    if type_root is None:
        return {}
    nsmap = {k or "": v for k, v in (type_root.nsmap or {}).items()}
    result: dict[str, dict[str, Any]] = {}
    for element in list(type_root):
        local = etree.QName(element).localname
        if local not in {"simpleType", "complexType"}:
            continue
        name = element.get("name")
        if not name:
            continue
        restriction = element.xpath(".//*[local-name()='restriction'][1]")
        base = restriction[0].get("base") if restriction else None
        enumerations = [n.get("value") for n in element.xpath(".//*[local-name()='enumeration']") if n.get("value") is not None]
        qname = f"{{{type_root.get('targetNamespace', '')}}}{name}"
        result[qname] = {
            "name": name,
            "qname": qname,
            "base_type": base,
            "enumerations": enumerations,
            "definition_kind": local,
        }
    return result


def _type_value_kind(type_qname: str | None, custom_types: dict[str, dict[str, Any]], nsmap: dict[str, str], _seen=None) -> tuple[str, bool, str | None]:
    """Return (value_kind, is_numeric, base_type_qname) without guessing unknown custom types."""
    if not type_qname:
        return "unknown", False, None
    _seen = set() if _seen is None else _seen
    prefix, sep, local = type_qname.partition(":")
    uri = nsmap.get(prefix) if sep else None
    expanded = f"{{{uri}}}{local}" if uri else type_qname
    if expanded in _seen:
        return "unknown", False, expanded
    _seen.add(expanded)

    numeric_terms = {
        "monetaryItemType": "monetary", "sharesItemType": "shares", "pureItemType": "pure",
        "decimalItemType": "numeric", "integerItemType": "numeric", "nonZeroDecimalItemType": "numeric",
        "floatItemType": "numeric", "doubleItemType": "numeric", "percentItemType": "numeric",
        "perShareItemType": "numeric", "fractionItemType": "numeric", "precisionDecimal": "numeric",
    }
    if local in numeric_terms:
        return numeric_terms[local], True, expanded
    if "textBlock" in local or local == "textBlockItemType":
        return "text_block", False, expanded
    if "boolean" in local.lower():
        return "boolean", False, expanded
    if local in {"dateItemType", "date"} or local.lower().endswith("dateitemtype"):
        return "date", False, expanded
    if local in {"dateTimeItemType", "dateTime"}:
        return "date_time", False, expanded
    if local in {"stringItemType", "normalizedStringItemType", "tokenItemType", "languageItemType", "nameItemType", "NCNameItemType"}:
        return "string", False, expanded
    if local in {"QNameItemType", "anyURIItemType"}:
        return "identifier", False, expanded

    definition = custom_types.get(expanded)
    if definition:
        base = definition.get("base_type")
        base_kind, is_numeric, ultimate_base = _type_value_kind(base, custom_types, nsmap, _seen)
        enums = definition.get("enumerations") or []
        if enums and not is_numeric:
            return "enumeration", False, ultimate_base or base
        return base_kind, is_numeric, ultimate_base or base

    # Common XML Schema primitive aliases; preserve unknown application types
    # as unknown instead of assuming that a value is financial numeric.
    if local in {"decimal", "integer", "nonNegativeInteger", "positiveInteger", "long", "int", "short", "byte", "float", "double"}:
        return "numeric", True, expanded
    if local in {"string", "normalizedString", "token", "language", "Name", "NCName"}:
        return "string", False, expanded
    if local == "boolean":
        return "boolean", False, expanded
    if local == "date":
        return "date", False, expanded
    if local == "dateTime":
        return "date_time", False, expanded
    return "unknown", False, expanded


def _concept_from_href(href: str | None, id_to_name: dict[str, str]) -> str | None:
    if not href or "#" not in href:
        return None
    fragment = href.rsplit("#", 1)[1]
    return id_to_name.get(fragment)


def _extract_labels(archive: zipfile.ZipFile, xml_names: list[str], id_to_name: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    labels: dict[str, list[dict[str, str]]] = defaultdict(list)
    for filename in xml_names:
        if "-lab" not in filename.lower() and not filename.lower().endswith("lab.xml"):
            continue
        try:
            root = _read_xml(archive, filename)
        except (etree.XMLSyntaxError, KeyError):
            continue
        for label_link in root.xpath("//*[local-name()='labelLink']"):
            locator_labels = {}
            resources = {}
            for child in label_link:
                local = etree.QName(child).localname
                xlabel = child.get(f"{{{XLINK_NS}}}label")
                if local == "loc" and xlabel:
                    target = _concept_from_href(child.get(f"{{{XLINK_NS}}}href"), id_to_name)
                    if target:
                        locator_labels[xlabel] = target
                elif local == "label" and xlabel:
                    resources[xlabel] = {
                        "text": "".join(child.itertext()).strip(),
                        "role": child.get(f"{{{XLINK_NS}}}role", "http://www.xbrl.org/2003/role/label"),
                        "language": child.get(f"{{{XML_NS}}}lang", "en"),
                        "source": filename,
                    }
            for arc in label_link.xpath("./*[local-name()='labelArc']"):
                source = locator_labels.get(arc.get(f"{{{XLINK_NS}}}from"))
                target = resources.get(arc.get(f"{{{XLINK_NS}}}to"))
                if source and target and target["text"]:
                    labels[source].append(target)
    for name in list(labels):
        unique = {}
        for label in labels[name]:
            unique[(label["role"], label["language"], label["text"])] = label
        labels[name] = sorted(unique.values(), key=lambda x: (x["role"], x["language"], x["text"]))
    return dict(labels)


def _extract_relationships(archive: zipfile.ZipFile, xml_names: list[str], id_to_name: dict[str, str]) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for filename in xml_names:
        lower = filename.lower()
        if not any(mark in lower for mark in ("-pre-", "-cal-", "-def-", "-ref-")):
            continue
        try:
            root = _read_xml(archive, filename)
        except (etree.XMLSyntaxError, KeyError):
            continue
        kind = "presentation" if "-pre-" in lower else "calculation" if "-cal-" in lower else "definition" if "-def-" in lower else "reference"
        for link in root:
            role = link.get(f"{{{XLINK_NS}}}role")
            locators = {}
            for loc in link.xpath("./*[local-name()='loc']"):
                alias = loc.get(f"{{{XLINK_NS}}}label")
                concept = _concept_from_href(loc.get(f"{{{XLINK_NS}}}href"), id_to_name)
                if alias and concept:
                    locators[alias] = concept
            for arc in link:
                if not etree.QName(arc).localname.endswith("Arc"):
                    continue
                source = locators.get(arc.get(f"{{{XLINK_NS}}}from"))
                target = locators.get(arc.get(f"{{{XLINK_NS}}}to"))
                # Keep only concept-to-concept relationships, not label/reference resources.
                if not source or not target:
                    continue
                record = {
                    "kind": kind,
                    "role": role,
                    "arcrole": arc.get(f"{{{XLINK_NS}}}arcrole"),
                    "source": source,
                    "target": target,
                    "source_file": filename,
                }
                for attr in ("order", "weight", "preferredLabel", "use"):
                    if arc.get(attr) is not None:
                        record[attr] = arc.get(attr)
                relationships.append(record)
    relationships.sort(key=lambda r: (r["kind"], r.get("role") or "", r["source"], r["target"], r.get("order", "")))
    return relationships


def import_taxonomy_package(package_path: str | Path) -> dict[str, Any]:
    package_path = Path(package_path)
    package_bytes = package_path.read_bytes()
    with zipfile.ZipFile(package_path) as archive:
        names = archive.namelist()
        normalized = {name.replace("\\", "/"): name for name in names}
        core_path = next((orig for norm, orig in normalized.items() if norm.lower() == "core/in-capmkt.xsd"), None)
        if core_path is None:
            raise ValueError(f"{package_path.name}: required core/in-capmkt.xsd not found")
        core_root = etree.fromstring(archive.read(core_path), parser=_parser())
        core_namespace = core_root.get("targetNamespace")
        if not core_namespace:
            raise ValueError(f"{package_path.name}: core schema has no targetNamespace")
        ns_version_match = re.search(r"/xbrl/(\d{4}-\d{2}-\d{2})/in-capmkt/?$", core_namespace)
        ns_version = ns_version_match.group(1) if ns_version_match else None

        meta_path = next((orig for norm, orig in normalized.items() if norm.lower() == "meta-inf/taxonomypackage.xml"), None)
        if meta_path is None:
            raise ValueError(f"{package_path.name}: META-INF/taxonomyPackage.xml not found")
        meta_root = _read_xml(archive, meta_path)
        publication_date = _root_text(meta_root, "publicationDate")
        identifier = _root_text(meta_root, "identifier")
        publisher = _root_text(meta_root, "publisher")
        entry_hrefs = meta_root.xpath("//*[local-name()='entryPointDocument']/@href")
        entry_xsd_names = [
            name for name in names
            if name.lower().endswith(".xsd") and ("-ent-" in name.lower() or "-ent." in name.lower())
        ]
        entry_namespaces = []
        for entry_name in entry_xsd_names:
            try:
                entry_root = _read_xml(archive, entry_name)
                if entry_root.get("targetNamespace"):
                    entry_namespaces.append(entry_root.get("targetNamespace"))
            except (etree.XMLSyntaxError, KeyError):
                continue
        marker, taxonomy_id, label = _family_from_package(entry_hrefs + entry_xsd_names, entry_namespaces)
        version_candidates = {v for v in (publication_date, ns_version) if v}
        # Entry-point target namespaces provide a further version consistency check.
        for value in entry_namespaces:
            match = re.search(re.escape(marker) + r"/(\d{4}-\d{2}-\d{2})/", value)
            if match:
                version_candidates.add(match.group(1))
        if len(version_candidates) != 1:
            raise ValueError(
                f"{package_path.name}: taxonomy version evidence conflicts or is missing: {sorted(version_candidates)}"
            )
        version = next(iter(version_candidates))

        xsd_entries = [name for name in names if name.lower().endswith(".xsd")]
        type_root = None
        for name in xsd_entries:
            if name.lower().endswith("in-capmkt-types.xsd"):
                type_root = _read_xml(archive, name)
                break
        custom_types = _load_type_definitions(type_root)

        id_to_name: dict[str, str] = {}
        raw_elements = []
        for element in core_root.findall(f"{{{XSD_NS}}}element"):
            name = element.get("name")
            if not name:
                continue
            element_id = element.get("id")
            if element_id:
                id_to_name[element_id] = name
            raw_elements.append(element)
        if not raw_elements:
            raise ValueError(f"{package_path.name}: core XSD yielded no global concepts")

        labels = _extract_labels(archive, names, id_to_name)
        relationships = _extract_relationships(archive, names, id_to_name)
        concepts: dict[str, dict[str, Any]] = {}
        core_nsmap = {k or "": v for k, v in (core_root.nsmap or {}).items()}
        type_nsmap = {k or "": v for k, v in ((type_root.nsmap or {}) if type_root is not None else {}).items()}
        merged_nsmap = {**core_nsmap, **type_nsmap}
        for element in raw_elements:
            name = element.get("name")
            type_qname = element.get("type")
            value_kind, is_numeric, base_type = _type_value_kind(type_qname, custom_types, merged_nsmap)
            type_prefix, colon, type_local = (type_qname or "").partition(":")
            type_ns = merged_nsmap.get(type_prefix) if colon else None
            concepts[name] = {
                "name": name,
                "id": element.get("id"),
                "qname": f"{{{core_namespace}}}{name}",
                "display_qname": f"in-capmkt:{name}",
                "namespace": core_namespace,
                "type_qname": type_qname,
                "type_local_name": type_local if colon else (type_qname or None),
                "type_namespace": type_ns,
                "base_type_qname": base_type,
                "value_kind": value_kind,
                "is_numeric": is_numeric,
                "balance": element.get(f"{{{XBRLI_NS}}}balance"),
                "period_type": element.get(f"{{{XBRLI_NS}}}periodType"),
                "abstract": (element.get("abstract", "false").lower() == "true"),
                "nillable": (element.get("nillable", "true").lower() == "true"),
                "substitution_group": element.get("substitutionGroup"),
                "labels": labels.get(name, []),
            }

        package_files = [
            {"path": name, "sha256": _sha256(archive.read(name))}
            for name in sorted(names)
            if name.lower().endswith((".xsd", ".xml"))
        ]
        return {
            "taxonomy_id": taxonomy_id,
            "label": label,
            "version": version,
            "publication_date": publication_date,
            "identifier": identifier,
            "publisher": publisher,
            "core_namespace": core_namespace,
            "entry_namespaces": sorted(set(entry_namespaces)),
            "entry_points": sorted(entry_hrefs),
            "source_package": package_path.name,
            "source_package_sha256": _sha256(package_bytes),
            "source_kind": "SEBI XBRL taxonomy package",
            "concept_count": len(concepts),
            "concepts_with_period_type": sum(c["period_type"] in {"instant", "duration"} for c in concepts.values()),
            "concepts_with_labels": sum(bool(c["labels"]) for c in concepts.values()),
            "relationship_count": len(relationships),
            "custom_type_count": len(custom_types),
            "custom_types": custom_types,
            "concepts": dict(sorted(concepts.items())),
            "relationships": relationships,
            "source_files": package_files,
        }


def build_catalog(package_paths: list[str | Path]) -> dict[str, Any]:
    if not package_paths:
        raise ValueError("At least one SEBI taxonomy ZIP package is required")
    entries: dict[str, dict[str, Any]] = {}
    for path in package_paths:
        package = import_taxonomy_package(path)
        taxonomy_id, version = package["taxonomy_id"], package["version"]
        family_versions = entries.setdefault(taxonomy_id, {})
        if version in family_versions:
            existing = family_versions[version]
            if existing["source_package_sha256"] != package["source_package_sha256"]:
                raise ValueError(f"Conflicting source packages for {taxonomy_id}@{version}")
            continue
        family_versions[version] = package
    # Stable ordering makes the catalog suitable for review, diffs, and rebuilds.
    entries = {tid: dict(sorted(versions.items())) for tid, versions in sorted(entries.items())}
    catalog = {
        "catalog_format_version": 1,
        "generator": "taxonomy.catalog_importer",
        "source_of_truth": "Taxonomy XSD and linkbase files contained in supplied SEBI XBRL taxonomy packages",
        "mapping_policy": "Concept presence in this catalog does not by itself approve canonical financial mapping.",
        "taxonomies": entries,
    }
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("catalog_format_version") != 1:
        raise ValueError("Unsupported taxonomy catalog format version")
    taxonomies = catalog.get("taxonomies")
    if not isinstance(taxonomies, dict) or not taxonomies:
        raise ValueError("Taxonomy catalog must contain at least one taxonomy family")
    for taxonomy_id, versions in taxonomies.items():
        if not isinstance(versions, dict) or not versions:
            raise ValueError(f"Taxonomy family {taxonomy_id!r} has no versions")
        for version, entry in versions.items():
            if entry.get("taxonomy_id") != taxonomy_id or entry.get("version") != version:
                raise ValueError(f"Taxonomy key mismatch for {taxonomy_id}@{version}")
            if not entry.get("core_namespace") or not entry.get("source_package_sha256"):
                raise ValueError(f"Taxonomy {taxonomy_id}@{version} is missing source provenance")
            concepts = entry.get("concepts")
            if not isinstance(concepts, dict) or entry.get("concept_count") != len(concepts):
                raise ValueError(f"Taxonomy {taxonomy_id}@{version} concept_count does not reconcile")
            for name, concept in concepts.items():
                if concept.get("name") != name or not concept.get("qname"):
                    raise ValueError(f"Malformed concept record {taxonomy_id}@{version}:{name}")
                period_type = concept.get("period_type")
                if period_type not in {None, "instant", "duration"}:
                    raise ValueError(f"Invalid periodType {period_type!r} for {taxonomy_id}@{version}:{name}")


def write_catalog(catalog: dict[str, Any], output_path: str | Path) -> None:
    validate_catalog(catalog)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", help="SEBI taxonomy ZIP packages containing core/in-capmkt.xsd")
    parser.add_argument("--output", default="taxonomy/taxonomy_catalog.json", help="Output JSON catalog path")
    args = parser.parse_args(argv)
    try:
        catalog = build_catalog(args.packages)
        write_catalog(catalog, args.output)
        print(f"Wrote {args.output}")
        print("taxonomy_id\tversion\tconcepts\tperiod_types\tlabels\trelationships\tsha256")
        for taxonomy_id, versions in catalog["taxonomies"].items():
            for version, entry in versions.items():
                print(
                    f"{taxonomy_id}\t{version}\t{entry['concept_count']}\t"
                    f"{entry['concepts_with_period_type']}\t{entry['concepts_with_labels']}\t"
                    f"{entry['relationship_count']}\t{entry['source_package_sha256']}"
                )
    except (OSError, ValueError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
