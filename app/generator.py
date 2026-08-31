import csv
import io
import xml.etree.ElementTree as ET

from .engine.merge_istat import serialise_xml, validate_text_against_schema

XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _norm(value):
    return (value or "").strip().lower().replace("-", "").replace(" ", "")


def _pick(row, *names):
    for name in names:
        target = _norm(name)
        for key, value in row.items():
            if _norm(key) == target:
                return value
    return ""


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _apply_field_map(row, field_map=None):
    if not field_map:
        return row
    mapped = dict(row)
    for canonical_name, actual_name in field_map.items():
        actual_text = str(actual_name or "").strip()
        if not actual_text:
            continue
        match = None
        for key, value in row.items():
            if _norm(key) == _norm(actual_text):
                match = value
                break
        if match is not None:
            mapped[canonical_name] = match
    return mapped


def _build_row(row):
    date = _pick(row, "date", "data")
    if not date:
        raise ValueError("Missing required date value.")

    required = {
        "codiceclientesr": (_pick(row, "codiceclientesr", "customer_code", "guest_code", "code"), "Missing guest code."),
        "sesso": (_pick(row, "sesso", "sex", "gender"), "Missing guest sex."),
        "cittadinanza": (_pick(row, "cittadinanza", "citizenship", "nationality"), "Missing citizenship code."),
        "comuneresidenza": (_pick(row, "comuneresidenza", "residence", "city_code"), "Missing residence code."),
        "occupazionepostoletto": (_pick(row, "occupazionepostoletto", "occupancy", "occupied_bed"), "Missing occupied-bed flag."),
        "dayuse": (_pick(row, "dayuse", "day_use"), "Missing day-use flag."),
        "tipologiaalloggiato": (_pick(row, "tipologiaalloggiato", "guest_type", "guest_category"), "Missing guest type."),
        "eta": (_pick(row, "eta", "age"), "Missing age."),
    }

    missing = [name for name, (value, message) in required.items() if not str(value).strip()]
    if missing:
        raise ValueError("Missing required field(s): %s" % ", ".join(missing))

    return {
        "date": date.strip(),
        "codiceclientesr": str(required["codiceclientesr"][0]).strip(),
        "sesso": str(required["sesso"][0]).strip(),
        "cittadinanza": str(required["cittadinanza"][0]).strip(),
        "comuneresidenza": str(required["comuneresidenza"][0]).strip(),
        "occupazionepostoletto": str(required["occupazionepostoletto"][0]).strip(),
        "dayuse": str(required["dayuse"][0]).strip(),
        "tipologiaalloggiato": str(required["tipologiaalloggiato"][0]).strip(),
        "eta": str(required["eta"][0]).strip(),
        "cameredisponibili": _as_int(_pick(row, "cameredisponibili", "rooms_available", "available_rooms"), 0),
        "postilettodisponibili": _as_int(_pick(row, "postilettodisponibili", "beds_available", "available_beds"), 0),
        "camereoccupate": _as_int(_pick(row, "camereoccupate", "occupied_rooms", "rooms_occupied"), 0),
    }


def parse_csv_rows(raw_bytes, field_map=None):
    text = raw_bytes.decode("utf-8-sig")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ValueError("The CSV file is empty or missing a header row.")
    rows = []
    for index, row in enumerate(reader, start=2):
        clean = {key.strip(): (value.strip() if value is not None else "") for key, value in row.items()}
        clean = _apply_field_map(clean, field_map)
        try:
            rows.append(_build_row(clean))
        except ValueError as exc:
            raise ValueError("Row %d: %s" % (index, exc)) from exc
    if not rows:
        raise ValueError("The CSV file contains no data rows.")
    return rows


def build_xml_from_rows(rows):
    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    ET.register_namespace("xsi", XSI)
    root = ET.Element("movimenti")
    root.set("xmlns:xsi", XSI)
    root.set("xsi:noNamespaceSchemaLocation", "movimentogiornaliero-0.6.xsd")
    root.set("vendor", "CSVGenerator")

    for date in sorted(by_date):
        movement = ET.SubElement(root, "movimento")
        movement.set("type", "MP")
        movement.set("data", date)

        arrivi = ET.SubElement(movement, "arrivi")
        for row in by_date[date]:
            arrivo = ET.SubElement(arrivi, "arrivo")
            for tag_name, value in [
                ("codiceclientesr", row["codiceclientesr"]),
                ("sesso", row["sesso"]),
                ("cittadinanza", row["cittadinanza"]),
                ("comuneresidenza", row["comuneresidenza"]),
                ("occupazionepostoletto", row["occupazionepostoletto"]),
                ("dayuse", row["dayuse"]),
                ("tipologiaalloggiato", row["tipologiaalloggiato"]),
                ("eta", row["eta"]),
            ]:
                elem = ET.SubElement(arrivo, tag_name)
                elem.text = value

        structure = ET.SubElement(movement, "datistruttura")
        total_rooms = sum(item["cameredisponibili"] for item in by_date[date])
        total_beds = sum(item["postilettodisponibili"] for item in by_date[date])
        total_occupied = sum(item["camereoccupate"] for item in by_date[date])

        for tag_name, value in [
            ("cameredisponibili", str(total_rooms)),
            ("postilettodisponibili", str(total_beds)),
            ("camereoccupate", str(total_occupied)),
        ]:
            elem = ET.SubElement(structure, tag_name)
            elem.text = value

    return serialise_xml(root)


def generate_xml_from_csv(raw_bytes, field_map=None):
    rows = parse_csv_rows(raw_bytes, field_map=field_map)
    xml = build_xml_from_rows(rows)
    status, detail = validate_text_against_schema(xml)
    return {
        "xml": xml,
        "rows": rows,
        "schema_status": status,
        "schema_detail": detail,
    }
