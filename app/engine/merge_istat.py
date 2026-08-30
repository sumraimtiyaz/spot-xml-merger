#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_istat.py — Merge/aggregate Italian ISTAT-SPOT daily tourism XML files
============================================================================

Purpose
-------
A property management system (Lodgify/Chekin) that maps each room as an
independent listing exports ONE monthly XML per listing. The DMS Puglia (SPOT)
portal accepts only ONE file per business ID.

Concatenating the files does not work: the XML is organised chronologically,
one <movimento data="YYYY-MM-DD"> block per day, so back-to-back merging
produces duplicate date blocks, which the portal rejects.

This script merges N files for the same month into a single compliant file:

  * one <movimento> block per calendar date, in chronological order
  * the capacity/occupancy counters are SUMMED across the source files
  * guest / arrival / departure records are CONCATENATED, never summed
  * type="NM" ("nessun movimento") is kept only when EVERY source file
    reports no movement for that date; otherwise the real movement type wins

Safety rules
------------
Only values inside a declared counter block (SUM_CONTAINERS) or with a
declared counter name (SUM_TAGS) are ever added together. Everything else —
business IDs, ISTAT codes, dates, names — is passed through untouched, with
leading zeros preserved. When two files disagree about something the script
was not told to add up, it keeps both entries as separate records and says so,
rather than silently inventing a value.

Requirements
------------
Python 3.8+ (standard library only — nothing to install).

Usage
-----
    python3 merge_istat.py                      # uses ./input -> ./output
    python3 merge_istat.py /path/to/folder
    python3 merge_istat.py file1.xml file2.xml file3.xml
    python3 merge_istat.py -i ./input -o ./output --name gennaio2026.xml

Licence: do whatever you like with it.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime
import json
import os
import re
import io
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict

# --------------------------------------------------------------------------
# CONFIGURATION — the only part you may ever need to edit
# --------------------------------------------------------------------------

# Blocks whose numeric children are ADDED together across files.
# <datistruttura> holds the per-listing capacity/occupancy counters, so the
# three listings' values must be summed to describe the whole guest house.
# NOTHING outside these blocks (or SUM_TAGS below) is ever added up.
SUM_CONTAINERS = {
    "datistruttura",
    "datistrutture",
}

# Individual counter tags that may appear outside a counter block.
SUM_TAGS = {
    "cameredisponibili",
    "postilettodisponibili",
    "camereoccupate",
    "postilettooccupati",
    "lettidisponibili",
    "lettioccupati",
}

# Blocks that hold a LIST of records. Their children are always kept as
# separate records and never merged into one another.
RECORD_WRAPPERS = {
    "arrivi", "partenze", "presenze",
    "ospiti", "alloggiati", "clienti",
    "camere", "unitaabitative", "appartamenti",
    "schedine", "prenotazioni",
}

# movimento/@type, from datatype-0.6.xsd:
#   MP = Movimento Presente   (arrivals and/or departures that day)
#   NM = Nessuna Movimentazione (open, but nobody arrived or left)
#   EC = Esercizio Chiuso     (the premises were closed)
# Merging three listings into one property: if any listing had movement the
# property had movement; the property is only "closed" if every listing was.
TYPE_PRECEDENCE = ["MP", "NM", "EC"]
NO_MOVEMENT_TYPES = {"NM", "EC"}

# movimento is an xs:sequence in the official schema. Order is fixed and must
# not be inferred from the input files.
CANONICAL_ORDER = {
    "movimento": ["arrivi", "partenze", "datistruttura"],
    "datistruttura": ["cameredisponibili", "postilettodisponibili",
                      "camereoccupate"],
}

# Guest identifier. Must be unique within <arrivi>, and SPOT also uses it to
# track who is currently in the property, so it must not collide between the
# listings being merged.
CODE_ELEMENT = "codiceclientesr"
CODE_MAX_LENGTH = 100

# The element repeated once per calendar day, and the attribute holding the date.
DAY_ELEMENT = "movimento"
DAY_ATTRIBUTE = "data"

# --------------------------------------------------------------------------

XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("xsi", XSI)

INT_RE = re.compile(r"^[+-]?\d+$")
DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
MERGED_MARKER = "_MERGED"
MARKER_COMMENT = "<!-- produced by merge_istat.py - do not use as input -->"
MARKER_PROBE = "produced by merge_istat.py"


class MergeError(Exception):
    """Fatal problem that stops the merge."""


# ==========================================================================
# Small helpers
# ==========================================================================

def human(path):
    return os.path.basename(path) or path


def local(tag):
    """Tag name without any namespace prefix."""
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else str(tag)


def namespace_of(tag):
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def text_of(el):
    return (el.text or "").strip()


def is_leaf(el):
    return len(el) == 0


def date_key(value):
    """Sort dates chronologically even if a file uses 2026-1-5 instead of
    2026-01-05. Unparseable values sort last, by text."""
    match = DATE_RE.match(value or "")
    if match:
        return (0, int(match.group(1)), int(match.group(2)), int(match.group(3)), "")
    return (1, 0, 0, 0, value or "")


def compact(value):
    """YYYY-MM-DD -> YYYYMMDD, tolerating single-digit months/days."""
    match = DATE_RE.match(value or "")
    if match:
        return "%04d%02d%02d" % (int(match.group(1)), int(match.group(2)),
                                 int(match.group(3)))
    return re.sub(r"[^0-9A-Za-z]", "", value or "undated")


# ==========================================================================
# Structure learning
# ==========================================================================

def learn_repeatable(roots):
    """
    (parent tag, child tag) pairs seen more than once under the same parent
    anywhere in the input. Those children are records and must be kept
    separate. Keyed by parent so a duplicate in one place cannot change how an
    unrelated element is treated elsewhere.
    """
    repeatable = set()

    def walk(el):
        counts = defaultdict(int)
        for child in el:
            counts[local(child.tag)] += 1
        parent = local(el.tag)
        for tag, n in counts.items():
            if n > 1:
                repeatable.add((parent, tag))
        for child in el:
            walk(child)

    for root in roots:
        walk(root)
    return repeatable


def merge_sequences(sequences):
    """
    Merge several observed orderings into one that respects them all
    (a simplified C3 linearisation). Falls back to first-seen order if the
    orderings genuinely contradict each other.
    """
    pending = [list(seq) for seq in sequences if seq]
    result = []
    while pending:
        chosen = None
        for seq in pending:
            head = seq[0]
            if not any(head in other[1:] for other in pending):
                chosen = head
                break
        if chosen is None:                      # contradictory orders
            chosen = pending[0][0]
        result.append(chosen)
        for seq in pending:
            if seq and seq[0] == chosen:
                del seq[0]
        pending = [seq for seq in pending if seq]
    return result


def learn_child_order(roots):
    """
    Canonical child order for every parent tag, derived from all input files.
    Without this the output order would depend on which file happens to sort
    first, which can break an xs:sequence in the schema.
    """
    observed = defaultdict(list)

    def walk(el):
        parent = local(el.tag)
        seq = []
        for child in el:
            tag = local(child.tag)
            if tag not in seq:
                seq.append(tag)
        if seq:
            observed[parent].append(seq)
        for child in el:
            walk(child)

    for root in roots:
        walk(root)
    return {parent: merge_sequences(seqs) for parent, seqs in observed.items()}


def ordered_child_tags(elems, child_order):
    """Child tags of this group, in canonical order."""
    seen = []
    for el in elems:
        for child in el:
            tag = local(child.tag)
            if tag not in seen:
                seen.append(tag)
    parent = local(elems[0].tag)
    canonical = CANONICAL_ORDER.get(parent) or child_order.get(parent, [])
    ordered = [tag for tag in canonical if tag in seen]
    ordered += [tag for tag in seen if tag not in ordered]
    return ordered


# ==========================================================================
# Identity detection
# ==========================================================================

def values_disagree(elems):
    """Do these same-tag elements carry different attribute or leaf values?"""
    keys = []
    for el in elems:
        for key in el.attrib:
            if key not in keys:
                keys.append(key)
    for key in keys:
        if len({el.attrib.get(key) for el in elems}) > 1:
            return True
    if all(is_leaf(el) for el in elems):
        return len({text_of(el) for el in elems}) > 1
    return False


def attributes_disagree(elems):
    keys = []
    for el in elems:
        for key in el.attrib:
            if key not in keys:
                keys.append(key)
    for key in keys:
        if len({el.attrib.get(key) for el in elems}) > 1:
            return True
    return False


def has_identity_conflict(elems, child_order):
    """
    True when these same-tag elements describe DIFFERENT things (two guests,
    two bookings) rather than one thing reported by several files.

    Only this element's own attributes and its immediate leaf children are
    inspected. A disagreement deeper down means the real split belongs to a
    lower element, and the recursion handles it there — that keeps a list
    wrapper as one wrapper while still splitting the records inside it.
    """
    if len(elems) < 2:
        return False
    if values_disagree(elems):
        return True
    for tag in ordered_child_tags(elems, child_order):
        groups = [[c for c in el if local(c.tag) == tag] for el in elems]
        if any(len(group) > 1 for group in groups):
            continue                       # already treated as records
        present = [group[0] for group in groups if group]
        if len(present) < 2 or not all(is_leaf(c) for c in present):
            continue
        if values_disagree(present):
            return True
    return False


# ==========================================================================
# Recursive merge
# ==========================================================================

def sum_text(values, path, tag, warnings):
    """Add integer values, preserving zero padding where the source used it."""
    total = sum(int(v) for v in values)
    widths = {len(v) for v in values}
    if len(widths) == 1 and any(v.lstrip("+-").startswith("0") and
                                v.strip("+-") != "0" for v in values):
        width = widths.pop()
        return str(total).zfill(width)
    return str(total)


def merge_leaf(elems, summable, path, warnings):
    out = ET.Element(elems[0].tag)
    for el in elems:
        for key, value in el.attrib.items():
            out.attrib.setdefault(key, value)

    raw = [text_of(el) for el in elems]
    filled = [v for v in raw if v != ""]
    tag = local(elems[0].tag)

    if not filled:
        out.text = "0" if summable else (raw[0] if raw else "")
        return out

    if summable and all(INT_RE.match(v) for v in filled):
        out.text = sum_text(filled, path, tag, warnings)
        return out

    unique = list(dict.fromkeys(filled))
    out.text = unique[0]
    if len(unique) > 1:
        warnings.append(
            "%s: the files give different values %s and <%s> is not a counter, "
            "so %r was kept. If <%s> IS a counter, add \"%s\" to SUM_TAGS at "
            "the top of merge_istat.py and run again."
            % (path, unique, tag, unique[0], tag, tag)
        )
    elif len(filled) < len(raw):
        pass                                   # some files simply omitted it
    return out


def merge_elements(elems, ctx, path, quiet_attrs=frozenset()):
    """Merge same-tag elements (normally one per source file) into one."""
    repeatable = ctx["repeatable"]
    child_order = ctx["child_order"]
    warnings = ctx["warnings"]

    parent_tag = local(elems[0].tag)
    in_sum_block = parent_tag in SUM_CONTAINERS

    if all(is_leaf(el) for el in elems):
        return merge_leaf(elems, in_sum_block or parent_tag in SUM_TAGS,
                          path, warnings)

    out = ET.Element(elems[0].tag)
    for el in elems:
        for key, value in el.attrib.items():
            if key not in out.attrib:
                out.attrib[key] = value
            elif out.attrib[key] != value and local(key) not in quiet_attrs:
                warnings.append(
                    "%s/@%s: files disagree (%r vs %r); kept %r"
                    % (path, local(key), out.attrib[key], value, out.attrib[key])
                )

    for tag in ordered_child_tags(elems, child_order):
        groups = [[c for c in el if local(c.tag) == tag] for el in elems]
        flat = [c for group in groups for c in group]
        child_path = path + "/" + tag
        repeats_here = any(len(group) > 1 for group in groups)

        if in_sum_block:
            as_record = False                       # counters: always aggregate
        elif parent_tag in RECORD_WRAPPERS:
            as_record = True                        # list items: keep separate
        elif tag in SUM_CONTAINERS:
            as_record = False                       # counter block: one per day
        elif repeats_here or (parent_tag, tag) in repeatable:
            as_record = True                        # observed repeating
        elif tag in RECORD_WRAPPERS:
            as_record = False                       # list wrapper: one per day
        elif tag in SUM_TAGS and all(is_leaf(c) for c in flat):
            as_record = False                       # named counter
        elif all(is_leaf(c) for c in flat):
            as_record = attributes_disagree(flat)   # attribute-tagged records
            if as_record:
                warnings.append(
                    "%s: <%s> carries different attributes in each file, so "
                    "every entry was kept" % (child_path, tag)
                )
        else:
            as_record = has_identity_conflict(flat, child_order)
            if as_record:
                warnings.append(
                    "%s: <%s> appears once per file but the contents differ, "
                    "so the entries were kept as separate records rather than "
                    "added together. If <%s> is a block of counters, add "
                    "\"%s\" to SUM_CONTAINERS at the top of merge_istat.py "
                    "and run again." % (child_path, tag, tag, tag)
                )

        if as_record:
            for child in flat:
                out.append(copy.deepcopy(child))
        else:
            out.append(merge_elements(flat, ctx, child_path))

    return out


def merge_day(day_elems, date, ctx):
    """Merge all <movimento> blocks for one calendar date."""
    merged = merge_elements(day_elems, ctx, "%s[%s]" % (DAY_ELEMENT, date),
                            quiet_attrs={"type"})

    # Movement type: MP beats NM beats EC (see TYPE_PRECEDENCE).
    types = [(el.get("type") or "").strip().upper() for el in day_elems]
    present = [t for t in types if t]
    unknown = [t for t in present if t not in TYPE_PRECEDENCE]
    if unknown:
        ctx["warnings"].append(
            "%s: unrecognised movement type(s) %s - the schema allows only %s"
            % (date, sorted(set(unknown)), ", ".join(TYPE_PRECEDENCE))
        )
    chosen = None
    for candidate in TYPE_PRECEDENCE:
        if candidate in present:
            chosen = candidate
            break
    if chosen is None and present:
        chosen = present[0]
    if chosen:
        merged.set("type", chosen)

    # "totale" is optional and the official example omits it. If the exports
    # carry it, recompute rather than keeping one listing's figure.
    if merged.get("totale") is not None:
        merged.set("totale", str(count_movements(merged)))

    merged.set(DAY_ATTRIBUTE, date)
    return merged


def count_movements(movimento):
    """Arrivals plus departures in this day, per the schema's note on @totale."""
    total = 0
    for wrapper in movimento:
        tag = local(wrapper.tag)
        if tag == "arrivi":
            total += sum(1 for c in wrapper if local(c.tag) == "arrivo")
        elif tag == "partenze":
            total += sum(1 for c in wrapper if local(c.tag) == CODE_ELEMENT)
    return total


# ==========================================================================
# SPOT rules: guest codes, date continuity, sanity checks
# ==========================================================================

def code_elements(root):
    """Every <codiceclientesr> in the document, wherever it appears."""
    found = []

    def walk(el):
        for child in el:
            if local(child.tag) == CODE_ELEMENT:
                found.append(child)
            walk(child)

    walk(root)
    return found


def listing_key(path):
    """
    A stable identifier for the listing a file came from, independent of the
    month. Lodgify names the exports <period>.xml, <period> 1.xml, <period> 2.xml,
    so stripping the period leaves the part that identifies the listing.
    """
    name = os.path.splitext(os.path.basename(path))[0]
    stripped = re.sub(r"\d{8}[_-]\d{8}", "", name).strip(" _-")
    return stripped or "0"


def make_prefix(key, taken):
    """A short, unique, stable tag for one listing."""
    base = "L" + re.sub(r"[^0-9A-Za-z]", "", str(key))[:8]
    if base == "L":
        base = "L0"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = "%s%d" % (base, n)
    return candidate


def load_listing_map(out_dir):
    path = os.path.join(out_dir or ".", "listing-map.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("prefixes"), dict):
            return path, data
    except (OSError, ValueError):
        pass
    return path, {"prefixes": {}, "active": False}


def save_listing_map(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    except OSError:
        pass


def prefix_guest_codes(parsed, mode, state, warnings):
    """
    Each listing numbers its guests independently, so the same
    <codiceclientesr> can mean two different people. SPOT rejects a code that
    belongs to someone already in the property, so colliding codes must be made
    distinct before merging.

    The prefix is derived from the listing, not from the month, and is
    remembered in listing-map.json so a guest who arrives in one month and
    leaves in the next keeps the same code.
    """
    per_file = []
    for path, root in parsed:
        per_file.append((path, code_elements(root)))

    seen_in = defaultdict(set)
    for path, elements in per_file:
        for element in elements:
            seen_in[text_of(element)].add(path)
    collisions = sorted(code for code, files in seen_in.items()
                        if code and len(files) > 1)

    state = dict(state or {"prefixes": {}, "active": False})
    if mode == "never":
        if collisions:
            warnings.append(
                "%d guest code(s) appear in more than one export (%s%s) but "
                "--prefix-codes never was used, so they were left alone. SPOT "
                "will treat them as the same person."
                % (len(collisions), ", ".join(collisions[:3]),
                   ", ..." if len(collisions) > 3 else ""))
        return 0, state

    active = state.get("active") or bool(collisions) or mode == "always"
    if not active:
        return 0, state

    prefixes = dict(state.get("prefixes") or {})
    changed = 0
    for index, (path, elements) in enumerate(per_file):
        key = listing_key(path)
        if key not in prefixes:
            prefixes[key] = make_prefix(key, set(prefixes.values()))
            if state.get("active"):
                warnings.append(
                    "%s looks like a listing this tool has not seen before; it "
                    "was given the guest-code prefix %r. Check that your "
                    "exports are named the same way every month."
                    % (human(path), prefixes[key]))
        tag = prefixes[key]
        for element in elements:
            value = text_of(element)
            if not value or value.startswith(tag + "-"):
                continue
            new = "%s-%s" % (tag, value)
            if len(new) > CODE_MAX_LENGTH:
                warnings.append(
                    "%s: guest code %r is too long to prefix (limit %d); left "
                    "unchanged" % (human(path), value, CODE_MAX_LENGTH))
                continue
            element.text = new
            changed += 1

    state = {"prefixes": prefixes, "active": True}
    if collisions and not mode == "always":
        warnings.append(
            "%d guest code(s) were used by more than one listing, so every "
            "guest code was prefixed per listing (%s). This is required: SPOT "
            "identifies guests by this code and would otherwise treat two "
            "people as one. The mapping is saved in listing-map.json and "
            "reused every month - keep that file."
            % (len(collisions),
               ", ".join("%s=%s" % (k, v) for k, v in sorted(prefixes.items()))))
    return changed, state


def check_date_continuity(dates, warnings):
    """
    Functional rule 7: the daily blocks must be a continuous run of dates.
    On a gap, SPOT stops processing the file from the missing day onwards.
    """
    parsed = []
    for value in dates:
        match = DATE_RE.match(value or "")
        if not match:
            return
        parsed.append(datetime.date(int(match.group(1)), int(match.group(2)),
                                    int(match.group(3))))
    parsed.sort()
    missing = []
    cursor = parsed[0]
    while cursor <= parsed[-1]:
        if cursor not in parsed:
            missing.append(cursor.isoformat())
        cursor += datetime.timedelta(days=1)
    if missing:
        warnings.append(
            "THE DATES ARE NOT CONTINUOUS - %d day(s) missing (%s%s). SPOT "
            "stops processing at the first gap, so everything after it would "
            "be ignored. Re-export the month covering every day."
            % (len(missing), ", ".join(missing[:5]),
               ", ..." if len(missing) > 5 else ""))


def check_spot_rules(days, warnings):
    """The functional rules from the SPOT technical specification."""
    for date, movimento in days:
        kind = movimento.get("type")
        has_arrivi = any(local(c.tag) == "arrivi" for c in movimento)
        has_partenze = any(local(c.tag) == "partenze" for c in movimento)
        counters = counter_snapshot(movimento)

        if kind == "MP" and not (has_arrivi or has_partenze):
            warnings.append(
                "%s: type is MP but there are no arrivals or departures "
                "(rule 14)" % date)
        if kind in NO_MOVEMENT_TYPES and (has_arrivi or has_partenze):
            warnings.append(
                "%s: type is %s, so <arrivi>/<partenze> must not be present"
                % (date, kind))
        if kind == "MP" and "cameredisponibili" not in counters:
            warnings.append(
                "%s: type is MP, which requires a <datistruttura> block" % date)

        rooms = counters.get("cameredisponibili")
        beds = counters.get("postilettodisponibili")
        busy = counters.get("camereoccupate")
        if rooms is not None and busy is not None and busy > rooms:
            warnings.append(
                "%s: rooms occupied (%d) exceeds rooms available (%d) - "
                "SPOT rejects this (rule 18)" % (date, busy, rooms))
        if rooms is not None and beds is not None and rooms > beds:
            warnings.append(
                "%s: rooms available (%d) exceeds bed places (%d) - SPOT "
                "rejects this (rule 19)" % (date, rooms, beds))
        if kind == "EC" and any(v for v in counters.values()):
            warnings.append(
                "%s: the premises are marked closed (EC) but the counters are "
                "not all zero" % date)


# ==========================================================================
# Schema validation
# ==========================================================================

def schema_path():
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "schema", "movimentogiornaliero-0.6.xsd")
    return candidate if os.path.isfile(candidate) else None


def validate_text_against_schema(xml_text):
    """Validate a document held in memory. Same contract as the path version."""
    xsd = schema_path()
    if not xsd:
        return "skipped", "the official XSD was not found next to the script"
    try:
        from lxml import etree
    except ImportError:
        handle, tmp = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(xml_text)
            return validate_against_schema(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    try:
        schema = etree.XMLSchema(etree.parse(xsd))
        doc = etree.fromstring(xml_text.encode("utf-8"))
        if schema.validate(doc):
            return "ok", "checked against movimentogiornaliero-0.6.xsd"
        return "failed", "\n     ".join(
            str(e.message) for e in schema.error_log[:8])
    except etree.Error as exc:
        return "skipped", "validator error: %s" % exc


def validate_against_schema(xml_path):
    """
    Validate with the official XSD. Returns (status, detail) where status is
    'ok', 'failed' or 'skipped'.
    """
    xsd = schema_path()
    if not xsd:
        return "skipped", "the official XSD was not found next to the script"

    try:
        from lxml import etree
    except ImportError:
        pass
    else:
        try:
            schema = etree.XMLSchema(etree.parse(xsd))
            doc = etree.parse(xml_path)
            if schema.validate(doc):
                return "ok", "checked against movimentogiornaliero-0.6.xsd"
            errors = [str(e.message) for e in schema.error_log][:8]
            return "failed", "\n     ".join(errors)
        except etree.Error as exc:
            return "skipped", "validator error: %s" % exc

    exe = shutil.which("xmllint")
    if not exe:
        return "skipped", ("no XML validator available (install lxml with "
                           "'pip install lxml' for automatic checking)")
    proc = subprocess.run([exe, "--noout", "--schema", xsd, xml_path],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return "ok", "checked against movimentogiornaliero-0.6.xsd"
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return "failed", "\n     ".join(detail[:8])


# ==========================================================================
# Input handling
# ==========================================================================

def looks_merged(path):
    if MERGED_MARKER in os.path.basename(path).upper():
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return MARKER_PROBE in handle.read(4096)
    except OSError:
        return False


def collect_input_files(inputs, skipped):
    """Turn the CLI arguments into a sorted list of XML files."""
    found = []
    for item in inputs:
        if os.path.isdir(item):
            for name in sorted(os.listdir(item)):
                full = os.path.join(item, name)
                if name.startswith("."):
                    continue
                if not name.lower().endswith(".xml"):
                    continue
                if not os.path.isfile(full):
                    continue
                found.append(full)
        elif os.path.isfile(item):
            found.append(item)                  # explicit file: always accepted
        else:
            raise MergeError("Not found: %s" % item)

    kept = []
    for path in found:
        if looks_merged(path):
            skipped.append(human(path))
            continue
        kept.append(path)
    return sorted(dict.fromkeys(os.path.abspath(p) for p in kept))


def parse_files(paths):
    parsed = []
    for path in paths:
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            raise MergeError("%s is not valid XML: %s" % (human(path), exc))
        parsed.append((path, tree.getroot()))
    return parsed


# ==========================================================================
# Reporting
# ==========================================================================

def counter_snapshot(movimento):
    values = {}
    for child in movimento:
        tag = local(child.tag)
        if tag in SUM_CONTAINERS:
            for leaf in child:
                if is_leaf(leaf) and INT_RE.match(text_of(leaf)):
                    values[local(leaf.tag)] = int(text_of(leaf))
        elif tag in SUM_TAGS and is_leaf(child) and INT_RE.match(text_of(child)):
            values[tag] = int(text_of(child))
    return values


def count_records(movimento):
    total = 0
    for child in movimento:
        if local(child.tag) in SUM_CONTAINERS:
            continue
        total += len(child) if len(child) else 1
    return total


def build_report(days):
    columns = []
    for _, movimento in days:
        for key in counter_snapshot(movimento):
            if key not in columns:
                columns.append(key)
    rows = []
    for date, movimento in days:
        snap = counter_snapshot(movimento)
        rows.append([date] + [snap.get(c, 0) for c in columns]
                    + [movimento.get("type", ""), count_records(movimento)])
    return columns, rows


def print_report(columns, rows):
    if not rows:
        return
    headers = ["date"] + columns + ["type", "records"]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def line(cells):
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    print()
    print("Daily totals in the merged file")
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row))
    totals = ["TOTAL"] + [sum(r[i + 1] for r in rows) for i in range(len(columns))]
    totals += ["", sum(r[-1] for r in rows)]
    print(line(["-" * w for w in widths]))
    print(line(totals))


def write_csv(csv_path, columns, rows):
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        handle.write(csv_text(columns, rows))


# ==========================================================================
# Output
# ==========================================================================

def indent(elem, level=0, space="\t"):
    """Pretty-print (ET.indent equivalent for Python < 3.9)."""
    if not len(elem):
        return
    pad = "\n" + level * space
    if not elem.text or not elem.text.strip():
        elem.text = pad + space
    last = None
    for child in elem:
        indent(child, level + 1, space)
        last = child
        if not child.tail or not child.tail.strip():
            child.tail = pad + space
    if last is not None and (not last.tail or not last.tail.strip()):
        last.tail = pad


def serialise_xml(root, marker=True):
    """The merged document as text, in the same style as the source files."""
    indent(root)
    body = ET.tostring(root, encoding="unicode")
    parts = ['<?xml version="1.0" encoding="utf-8"?>\n']
    # if marker:
    #     parts.append(MARKER_COMMENT + "\n")
    parts.append(body.rstrip())
    parts.append("\n")
    return "".join(parts)


def write_xml(root, path, marker=True):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialise_xml(root, marker=marker))


def csv_text(columns, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date"] + columns + ["type", "records"])
    writer.writerows(rows)
    return buffer.getvalue()


# ==========================================================================
# Main
# ==========================================================================

def merge_documents(parsed, prefix_mode="auto", listing_state=None):
    """
    The whole merge, on documents already in memory.

    `parsed` is a list of (label, ElementTree root). The label is only used in
    messages and to derive the per-listing guest-code prefix, so it should be
    the original file name.

    Returns a dict with the merged tree, the notes raised, the daily report and
    the period covered. Nothing is read from or written to disk, which is what
    lets the same code serve the desktop tool and the web service.
    """
    roots = [root for _, root in parsed]
    if not roots:
        raise MergeError("Nothing to merge.")

    root_tags = {local(r.tag) for r in roots}
    if len(root_tags) > 1:
        raise MergeError("Files have different root elements: %s" % sorted(root_tags))
    namespaces = {namespace_of(r.tag) for r in roots}
    if len(namespaces) > 1:
        raise MergeError(
            "Files use different XML namespaces (%s). They come from different "
            "schema versions and must not be merged."
            % sorted(ns or "(none)" for ns in namespaces))
    default_ns = namespaces.pop()
    if default_ns:
        ET.register_namespace("", default_ns)

    warnings = []
    recoded, listing_state = prefix_guest_codes(parsed, prefix_mode,
                                               listing_state, warnings)

    ctx = {"repeatable": learn_repeatable(roots),
           "child_order": learn_child_order(roots),
           "warnings": warnings}
    ctx["repeatable"].discard((local(roots[0].tag), DAY_ELEMENT))

    # Root element: keep the schema reference and vendor attributes.
    out_root = ET.Element(roots[0].tag)
    attr_sources = defaultdict(list)
    for label, root in parsed:
        for key, value in root.attrib.items():
            attr_sources[key].append(human(label))
            if key not in out_root.attrib:
                out_root.attrib[key] = value
            elif out_root.attrib[key] != value:
                warnings.append(
                    "root/@%s differs between files (%r vs %r); kept %r"
                    % (local(key), out_root.attrib[key], value, out_root.attrib[key]))
    for key, sources in attr_sources.items():
        if len(sources) != len(parsed):
            warnings.append(
                "root/@%s is present in only %d of %d files (%s) but was kept "
                "in the output" % (local(key), len(sources), len(parsed),
                                   ", ".join(sources)))

    by_date = defaultdict(list)
    header_elements = defaultdict(list)
    header_order = []
    seen_per_file = []
    for label, root in parsed:
        dates = set()
        found = 0
        for child in root:
            tag = local(child.tag)
            if tag != DAY_ELEMENT:
                if tag not in header_order:
                    header_order.append(tag)
                    warnings.append(
                        "Top-level element <%s> is not a <%s> block; it was "
                        "merged into the header of the output" % (tag, DAY_ELEMENT))
                header_elements[tag].append(child)
                continue
            date = child.get(DAY_ATTRIBUTE)
            if not date:
                raise MergeError("%s: a <%s> block has no %s attribute"
                                 % (human(label), DAY_ELEMENT, DAY_ATTRIBUTE))
            if not DATE_RE.match(date):
                warnings.append("%s: unusual date format %r" % (human(label), date))
            elif len(date) != 10:
                warnings.append("%s: date %r is not zero-padded" % (human(label), date))
            if date in dates:
                warnings.append(
                    "%s already contains two blocks for %s - both were aggregated"
                    % (human(label), date))
            dates.add(date)
            by_date[date].append(child)
            found += 1
        seen_per_file.append((label, dates))
        if found == 0:
            warnings.append("%s contains no <%s> blocks" % (human(label), DAY_ELEMENT))

    if not by_date:
        raise MergeError("No <%s> blocks found in any file." % DAY_ELEMENT)

    all_dates = set(by_date)
    if len(parsed) > 1:
        for label, dates in seen_per_file:
            missing = sorted(all_dates - dates, key=date_key)
            if missing:
                warnings.append(
                    "%s has no entry for %d date(s) (%s%s) - counted as zero "
                    "for those days" % (human(label), len(missing),
                                        ", ".join(missing[:3]),
                                        ", ..." if len(missing) > 3 else ""))
    months = {d[:7] for d in all_dates if DATE_RE.match(d)}
    if len(months) > 1:
        warnings.append("Input covers more than one month: %s" % sorted(months))

    for tag in header_order:
        group = header_elements[tag]
        if len(group) == 1 or (local(roots[0].tag), tag) in ctx["repeatable"]:
            for element in group:
                out_root.append(copy.deepcopy(element))
        else:
            out_root.append(merge_elements(group, ctx, tag))

    days = []
    for date in sorted(all_dates, key=date_key):
        merged = merge_day(by_date[date], date, ctx)
        out_root.append(merged)
        days.append((date, merged))

    check_date_continuity([d for d, _ in days], warnings)
    check_spot_rules(days, warnings)

    columns, rows = build_report(days)
    ordered = sorted(all_dates, key=date_key)

    return {
        "root": out_root,
        "warnings": warnings,
        "days": days,
        "columns": columns,
        "rows": rows,
        "recoded": recoded,
        "listing_state": listing_state,
        "first": ordered[0],
        "last": ordered[-1],
        "sources": [human(label) for label, _ in parsed],
    }


def merge_uploads(uploads, prefix_mode="auto", listing_state=None):
    """
    Merge documents supplied as (filename, bytes). Used by the web service;
    nothing touches the filesystem.
    """
    parsed = []
    for name, data in uploads:
        try:
            parsed.append((name, ET.fromstring(data)))
        except ET.ParseError as exc:
            raise MergeError("%s is not valid XML: %s" % (human(name), exc))
    result = merge_documents(parsed, prefix_mode=prefix_mode,
                             listing_state=listing_state)
    result["xml"] = serialise_xml(result["root"])
    result["csv"] = csv_text(result["columns"], result["rows"])
    status, detail = validate_text_against_schema(result["xml"])
    result["schema_status"] = status
    result["schema_detail"] = detail
    if status == "failed":
        result["warnings"].append(
            "the merged file does not satisfy the official XSD")
    result["filename"] = "%s_%s%s.xml" % (compact(result["first"]),
                                          compact(result["last"]), MERGED_MARKER)
    return result


def run(args):
    skipped = []
    files = collect_input_files(args.input, skipped)

    if not files:
        extra = ""
        if skipped:
            extra = ("\nSkipped %d file(s) that were already produced by this "
                     "script: %s" % (len(skipped), ", ".join(skipped)))
        raise MergeError(
            "No XML files found.\n"
            "Put the monthly exports (one per room) in the 'input' folder,\n"
            "or run:  python3 merge_istat.py /path/to/folder" + extra)

    print("Reading %d file%s:" % (len(files), "" if len(files) == 1 else "s"))
    for path in files:
        print("   - %s" % human(path))
    for name in skipped:
        print("   (skipped %s - it was produced by this script)" % name)
    if len(files) == 1:
        print("   ! Only one file found - the output will just be a tidied copy.")

    parsed = parse_files(files)
    map_path, state = load_listing_map(args.output)
    result = merge_documents(parsed, prefix_mode=args.prefix_codes,
                             listing_state=state)

    warnings = result["warnings"]
    if not args.quiet:
        print_report(result["columns"], result["rows"])

    if warnings and args.strict:
        print()
        print("Notes (%d):" % len(warnings))
        for note in warnings:
            print("   ! %s" % note)
        raise MergeError("--strict was requested and %d note(s) were raised. "
                         "Nothing was written." % len(warnings))

    out_dir = args.output or "."
    os.makedirs(out_dir, exist_ok=True)
    default_name = "%s_%s%s.xml" % (compact(result["first"]),
                                    compact(result["last"]), MERGED_MARKER)
    out_path = os.path.join(out_dir, args.name or default_name)
    write_xml(result["root"], out_path, marker=not args.no_marker)
    if result["listing_state"].get("active"):
        save_listing_map(map_path, result["listing_state"])

    if args.csv:
        csv_path = os.path.splitext(out_path)[0] + ".csv"
        write_csv(csv_path, result["columns"], result["rows"])
        print("\nCSV summary: %s" % csv_path)

    print()
    print("Merged %d file(s) into %d daily block(s)."
          % (len(files), len(result["days"])))
    print("Period: %s to %s" % (result["first"], result["last"]))
    print("Saved:  %s" % os.path.abspath(out_path))
    if result["recoded"]:
        print("Guest codes prefixed per listing: %d" % result["recoded"])

    status, detail = validate_against_schema(out_path)
    if status == "ok":
        print("Schema: VALID (%s)" % detail)
    elif status == "failed":
        print("Schema: INVALID - the portal would reject this file")
        print("     %s" % detail)
        warnings.append("the merged file does not satisfy the official XSD")
    else:
        print("Schema: not checked (%s)" % detail)

    if warnings:
        print()
        print("Notes (%d):" % len(warnings))
        for note in warnings:
            print("   ! %s" % note)
    else:
        print("No anomalies detected.")

    return out_path


def build_parser():
    parser = argparse.ArgumentParser(
        prog="merge_istat.py",
        description="Merge several monthly ISTAT/SPOT XML exports into one "
                    "file for the DMS Puglia portal.")
    parser.add_argument("input", nargs="*", default=None,
                        help="XML files, or a folder containing them "
                             "(default: ./input, then the script's folder)")
    parser.add_argument("-i", "--input-folder", dest="input_folder",
                        help="Folder containing the monthly XML files")
    parser.add_argument("-o", "--output", default=None,
                        help="Folder for the merged file (default: ./output)")
    parser.add_argument("-n", "--name", default=None,
                        help="Name of the merged file")
    parser.add_argument("--csv", action="store_true",
                        help="Also write a CSV summary of the daily totals")
    parser.add_argument("--strict", action="store_true",
                        help="Write nothing and exit with an error if anything "
                             "looks off")
    parser.add_argument("--quiet", action="store_true",
                        help="Skip the daily totals table")
    parser.add_argument("--prefix-codes", choices=["auto", "always", "never"],
                        default="auto",
                        help="how to keep guest codes unique across listings "
                             "(default: auto - prefix only once a clash is seen)")
    parser.add_argument("--no-marker", action="store_true",
                        help="Omit the 'produced by merge_istat.py' comment "
                             "from the output file")
    return parser


def resolve_defaults(args, base_dir):
    inputs = list(args.input or [])
    if args.input_folder:
        inputs.insert(0, args.input_folder)
    if not inputs:
        candidate = os.path.join(base_dir, "input")
        inputs = [candidate] if os.path.isdir(candidate) else [base_dir]
    args.input = inputs
    if args.output is None:
        args.output = os.path.join(base_dir, "output")
    return args


def main(argv=None):
    args = build_parser().parse_args(argv)
    args = resolve_defaults(args, os.path.dirname(os.path.abspath(__file__)))

    print("=" * 68)
    print("  ISTAT / SPOT monthly XML merger")
    print("=" * 68)
    try:
        run(args)
    except MergeError as exc:
        print()
        print("STOPPED: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - keep the message friendly
        print()
        print("UNEXPECTED ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
