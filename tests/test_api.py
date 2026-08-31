"""End-to-end tests for the web service."""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import metrics as metrics_module  # noqa: E402
from app.config import DevConfig  # noqa: E402

HEAD = ('<?xml version="1.0" encoding="utf-8"?>\n<movimenti '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="movimentogiornaliero-0.6.xsd" '
        'vendor="Chekin">\n')
TAIL = "</movimenti>\n"


def ds(rooms, beds, busy):
    return ("<datistruttura><cameredisponibili>%d</cameredisponibili>"
            "<postilettodisponibili>%d</postilettodisponibili>"
            "<camereoccupate>%d</camereoccupate></datistruttura>"
            % (rooms, beds, busy))


def arrivo(code):
    return ("<arrivo><codiceclientesr>%s</codiceclientesr><sesso>M</sesso>"
            "<cittadinanza>100000100</cittadinanza>"
            "<comuneresidenza>412058091</comuneresidenza>"
            "<occupazionepostoletto>si</occupazionepostoletto>"
            "<dayuse>no</dayuse><tipologiaalloggiato>16</tipologiaalloggiato>"
            "<eta>40</eta></arrivo>" % code)


def month(beds, code=None, days=5):
    out = HEAD
    for d in range(1, days + 1):
        date = "2026-06-%02d" % d
        if code and d == 2:
            out += ('\t<movimento type="MP" data="%s"><arrivi>%s</arrivi>%s'
                    '</movimento>\n' % (date, arrivo(code), ds(1, beds, 1)))
        else:
            out += ('\t<movimento type="NM" data="%s">%s</movimento>\n'
                    % (date, ds(1, beds, 0)))
    return out + TAIL


class Client:
    def __init__(self):
        cfg = type("T", (DevConfig,), {"TESTING": True, "RATE_LIMIT_PER_HOUR": 0})
        self.app = create_app(cfg)
        self.c = self.app.test_client()

    def merge(self, files, **extra):
        data = {"files": [(io.BytesIO(body.encode()), name)
                          for name, body in files]}
        data.update(extra)
        return self.c.post("/api/merge", data=data,
                           content_type="multipart/form-data")


RESULTS = []


def check(name, fn):
    try:
        problems = fn()
    except Exception as exc:  # noqa: BLE001
        problems = ["raised %s: %s" % (type(exc).__name__, exc)]
    RESULTS.append((name, problems))
    print("[%s] %s" % ("PASS" if not problems else "FAIL", name))
    for p in problems:
        print("        -> %s" % p)
    return not problems


def test_landing():
    c = Client()
    r = c.c.get("/")
    p = []
    if r.status_code != 200:
        p.append("status %s" % r.status_code)
    body = r.get_data(as_text=True)
    for needle in ["DMS Puglia", "movimentogiornaliero-0.6.xsd", "id=\"drop\""]:
        if needle not in body:
            p.append("landing page missing %r" % needle)
    if "no-store" not in r.headers.get("Cache-Control", ""):
        p.append("responses are cacheable")
    return p


def test_health():
    c = Client()
    r = c.c.get("/healthz")
    return [] if r.status_code == 200 and r.get_json()["status"] == "ok" else ["unhealthy"]


def test_contact_form_email_notification_is_called():
    c = Client()
    with patch("app.views.send_contact_email", return_value=True) as mocked:
        r = c.c.post("/api/contact", json={
            "name": "Mario",
            "email": "mario@example.com",
            "category": "issue",
            "message": "Please review this issue.",
        })
    if r.status_code != 200:
        return ["contact endpoint returned %s" % r.status_code]
    if not mocked.called:
        return ["email helper was not invoked"]
    return []


def test_validate_xml_ok():
    c = Client()
    xml = month(2, code="1")
    r = c.c.post("/api/validate-xml", data={"files": [(io.BytesIO(xml.encode()), "valid.xml")]},
                 content_type="multipart/form-data")
    if r.status_code != 200:
        return ["expected 200, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    payload = r.get_json()
    if payload.get("ok") is not True:
        return ["validation unexpectedly failed: %s" % payload]
    return []


def test_validate_xml_rejects_invalid_xml():
    c = Client()
    r = c.c.post("/api/validate-xml", data={"files": [(io.BytesIO(b"<movimenti>broken"), "bad.xml")]},
                 content_type="multipart/form-data")
    if r.status_code != 422:
        return ["expected 422, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    payload = r.get_json()
    if not payload.get("files"):
        return ["missing file validation results"]
    if payload["files"][0].get("status") not in {"invalid", "failed"}:
        return ["invalid file was not flagged as invalid"]
    return []


def test_contact_form_reports_delivery_failure():
    c = Client()
    with patch("app.views.send_contact_email", return_value=False):
        r = c.c.post("/api/contact", json={
            "name": "Mario",
            "email": "mario@example.com",
            "category": "issue",
            "message": "Please review this issue.",
        })
    if r.status_code != 502:
        return ["expected 502 when delivery fails, got %s" % r.status_code]
    if "delivery service" not in (r.get_json() or {}).get("error", ""):
        return ["missing delivery failure message"]
    return []


def test_generate_xml_from_csv():
    c = Client()
    csv_data = (
        "date,codiceclientesr,sesso,cittadinanza,comuneresidenza,occupazionepostoletto,dayuse,tipologiaalloggiato,eta,cameredisponibili,postilettodisponibili,camereoccupate\n"
        "2026-06-01,ABC1,M,100000100,412058091,si,no,16,40,2,4,1\n"
        "2026-06-02,ABC2,F,100000100,412058091,si,no,16,38,2,4,1\n"
    )
    r = c.c.post("/api/generate-xml", data={"files": [(io.BytesIO(csv_data.encode()), "guests.csv")]},
                 content_type="multipart/form-data")
    if r.status_code != 200:
        return ["expected 200, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    payload = r.get_json()
    if payload.get("ok") is not True:
        return ["generation unexpectedly failed: %s" % payload]
    if "<movimenti" not in payload["xml"]:
        return ["generated XML missing root element"]
    if payload["schema"]["status"] != "ok":
        return ["generated XML failed schema validation: %s" % payload["schema"].get("detail")]
    return []


def test_generate_xml_rejects_missing_required_fields():
    c = Client()
    csv_data = "date,codiceclientesr,sesso,cittadinanza\n2026-06-01,ABC1,M,100000100\n"
    r = c.c.post("/api/generate-xml", data={"files": [(io.BytesIO(csv_data.encode()), "bad.csv")]},
                 content_type="multipart/form-data")
    if r.status_code != 400:
        return ["expected 400, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    if "Missing required field" not in (r.get_json() or {}).get("error", ""):
        return ["missing required-field validation message"]
    return []


def test_generate_xml_from_excel():
    import openpyxl

    c = Client()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    headers = [
        "date", "codiceclientesr", "sesso", "cittadinanza", "comuneresidenza",
        "occupazionepostoletto", "dayuse", "tipologiaalloggiato", "eta",
        "cameredisponibili", "postilettodisponibili", "camereoccupate",
    ]
    sheet.append(headers)
    sheet.append(["2026-06-01", "ABC1", "M", "100000100", "412058091", "si", "no", "16", "40", 2, 4, 1])
    sheet.append(["2026-06-02", "ABC2", "F", "100000100", "412058091", "si", "no", "16", "38", 2, 4, 1])

    payload = io.BytesIO()
    workbook.save(payload)
    payload.seek(0)

    r = c.c.post("/api/generate-xml", data={"files": [(payload, "guests.xlsx")]},
                 content_type="multipart/form-data")
    if r.status_code != 200:
        return ["expected 200 for Excel upload, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    json_payload = r.get_json()
    if json_payload.get("ok") is not True:
        return ["Excel generation unexpectedly failed: %s" % json_payload]
    if json_payload["schema"]["status"] != "ok":
        return ["Excel-generated XML failed schema validation: %s" % json_payload["schema"].get("detail")]
    return []


def test_generate_xml_accepts_custom_mapping():
    c = Client()
    csv_data = (
        "guest_date,guest_code,sex,nationality,residence,occupied_bed,day_use,guest_type,age\n"
        "2026-06-01,ABC1,M,100000100,412058091,si,no,16,40\n"
        "2026-06-02,ABC2,F,100000100,412058091,si,no,16,38\n"
    )
    field_map = {
        "date": "guest_date",
        "codiceclientesr": "guest_code",
        "sesso": "sex",
        "cittadinanza": "nationality",
        "comuneresidenza": "residence",
        "occupazionepostoletto": "occupied_bed",
        "dayuse": "day_use",
        "tipologiaalloggiato": "guest_type",
        "eta": "age",
    }
    r = c.c.post("/api/generate-xml", data={
        "files": [(io.BytesIO(csv_data.encode()), "mapped.csv")],
        "field_map": json.dumps(field_map),
    }, content_type="multipart/form-data")
    if r.status_code != 200:
        return ["expected 200 with custom mapping, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    payload = r.get_json()
    if payload.get("ok") is not True:
        return ["custom mapping unexpectedly failed: %s" % payload]
    if payload["schema"]["status"] != "ok":
        return ["custom mapping generated invalid XML: %s" % payload["schema"].get("detail")]
    return []


def test_sample_csv_template_download():
    c = Client()
    r = c.c.get("/api/sample-csv")
    if r.status_code != 200:
        return ["expected 200, got %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    text = r.get_data(as_text=True)
    if "date,codiceclientesr" not in text:
        return ["template is missing the expected CSV header"]
    if "attachment; filename=spot-template.csv" not in r.headers.get("Content-Disposition", ""):
        return ["sample CSV download header is missing"]
    return []


def test_metrics_fallback_when_disk_writes_fail():
    original_path = metrics_module._metrics_path
    original_write = metrics_module._write_metrics_file
    try:
        metrics_module._MEMORY_METRICS.clear()
        metrics_module._metrics_path = lambda: Path("/nonexistent") / "site_metrics.xml"

        def boom(_path, _values):
            raise PermissionError("read-only filesystem")

        metrics_module._write_metrics_file = boom
        metrics = metrics_module.record_visitor()
        payload = metrics_module.metrics_payload()
        if metrics.get("total_visitors") != 1:
            return ["visitor count not tracked in memory"]
        if payload.get("total_visitors") != 1:
            return ["payload lost the in-memory visitor count"]
        return []
    finally:
        metrics_module._metrics_path = original_path
        metrics_module._write_metrics_file = original_write
        metrics_module._MEMORY_METRICS.clear()


def test_merge_happy():
    c = Client()
    r = c.merge([("20260601_20260630.xml", month(2)),
                 ("20260601_20260630 1.xml", month(4)),
                 ("20260601_20260630 2.xml", month(2))])
    p = []
    if r.status_code != 200:
        return ["status %s: %s" % (r.status_code, r.get_data(as_text=True)[:200])]
    d = r.get_json()
    if d["schema"]["status"] != "ok":
        p.append("not schema-valid: %s" % d["schema"]["detail"])
    if d["days"] != 5:
        p.append("expected 5 days, got %s" % d["days"])
    if "<cameredisponibili>3</cameredisponibili>" not in d["xml"]:
        p.append("rooms not summed to 3")
    if "<postilettodisponibili>8</postilettodisponibili>" not in d["xml"]:
        p.append("beds not summed to 8")
    if d["xml"].count('data="2026-06-01"') != 1:
        p.append("duplicate date blocks in the output")
    if not d["csv"].startswith("date,"):
        p.append("csv missing or malformed")
    if d["filename"] != "20260601_20260605_MERGED.xml":
        p.append("unexpected filename %r" % d["filename"])
    return p


def test_guest_code_collision():
    c = Client()
    r = c.merge([("20260601_20260630.xml", month(2, code="1")),
                 ("20260601_20260630 1.xml", month(4, code="1"))])
    d = r.get_json()
    p = []
    if r.status_code != 200:
        return ["status %s" % r.status_code]
    if d["recoded"] < 2:
        p.append("colliding codes were not re-coded (recoded=%s)" % d["recoded"])
    if d["xml"].count("<codiceclientesr>") != 2:
        p.append("expected two guest records")
    if "<codiceclientesr>1</codiceclientesr>" in d["xml"]:
        p.append("a colliding code survived unprefixed")
    if d["schema"]["status"] != "ok":
        p.append("re-coded output is not schema-valid")
    if not d["listing_state"].get("prefixes"):
        p.append("listing state not returned to the browser")
    return p


def test_listing_state_roundtrip():
    c = Client()
    first = c.merge([("20260601_20260630.xml", month(2, code="1")),
                     ("20260601_20260630 1.xml", month(4, code="1"))]).get_json()
    state = json.dumps(first["listing_state"])
    second = c.merge([("20260701_20260731.xml", month(2, code="1")),
                      ("20260701_20260731 1.xml", month(4, code="1"))],
                     listing_state=state).get_json()
    a = sorted(l.split("<codiceclientesr>")[1].split("<")[0]
               for l in first["xml"].split("\n") if "<codiceclientesr>" in l)
    b = sorted(l.split("<codiceclientesr>")[1].split("<")[0]
               for l in second["xml"].split("\n") if "<codiceclientesr>" in l)
    return [] if a == b else ["codes changed between months: %s vs %s" % (a, b)]


def test_rejects_non_xml():
    c = Client()
    r = c.merge([("notes.txt", "hello")])
    p = []
    if r.status_code != 400:
        p.append("expected 400, got %s" % r.status_code)
    if "not an .xml" not in (r.get_json() or {}).get("error", ""):
        p.append("unclear error message")
    return p


def test_rejects_broken_xml():
    c = Client()
    r = c.merge([("a.xml", HEAD + "<movimento")])
    p = []
    if r.status_code != 422:
        p.append("expected 422, got %s" % r.status_code)
    if "not valid XML" not in (r.get_json() or {}).get("error", ""):
        p.append("error does not explain the problem")
    return p


def test_rejects_empty_request():
    c = Client()
    r = c.c.post("/api/merge", data={}, content_type="multipart/form-data")
    return [] if r.status_code == 400 else ["expected 400, got %s" % r.status_code]


def test_date_gap_surfaces():
    c = Client()
    body = (HEAD + '\t<movimento type="NM" data="2026-06-01">%s</movimento>\n'
                   '\t<movimento type="NM" data="2026-06-05">%s</movimento>\n'
            % (ds(1, 2, 0), ds(1, 2, 0)) + TAIL)
    d = c.merge([("a.xml", body)]).get_json()
    joined = " ".join(d["warnings"])
    return ([] if "NOT CONTINUOUS" in joined
            else ["date gap not reported: %s" % joined])


def test_too_many_files():
    c = Client()
    files = [("f%d.xml" % i, month(1)) for i in range(31)]
    r = c.merge(files)
    return [] if r.status_code == 400 else ["expected 400, got %s" % r.status_code]


def test_nothing_written_to_disk():
    """The merge path must not create files anywhere under the project."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    before = set()
    for base, _, names in os.walk(root):
        if "__pycache__" in base or "/." in base:
            continue
        for n in names:
            before.add(os.path.join(base, n))
    c = Client()
    c.merge([("20260601_20260630.xml", month(2, code="1")),
             ("20260601_20260630 1.xml", month(4, code="1"))])
    after = set()
    for base, _, names in os.walk(root):
        if "__pycache__" in base or "/." in base:
            continue
        for n in names:
            after.add(os.path.join(base, n))
    new = after - before
    return [] if not new else ["files appeared on disk: %s" % sorted(new)[:5]]


def test_matches_desktop_tool():
    """The web service and the shipped desktop script must agree exactly."""
    import subprocess
    import tempfile
    desktop = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app", "engine", "merge_istat.py")
    files = [("20260601_20260630.xml", month(2)),
             ("20260601_20260630 1.xml", month(4))]
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "in"))
    for name, body in files:
        with open(os.path.join(tmp, "in", name), "w", encoding="utf-8") as fh:
            fh.write(body)
    subprocess.run([sys.executable, desktop, os.path.join(tmp, "in"),
                    "-o", os.path.join(tmp, "out"), "--quiet"],
                   capture_output=True, text=True)
    produced = [f for f in os.listdir(os.path.join(tmp, "out"))
                if f.endswith(".xml")]
    with open(os.path.join(tmp, "out", produced[0]), encoding="utf-8") as fh:
        cli_xml = fh.read()
    web_xml = Client().merge(files).get_json()["xml"]
    return ([] if cli_xml == web_xml
            else ["desktop and web output differ (%d vs %d bytes)"
                  % (len(cli_xml), len(web_xml))])


if __name__ == "__main__":
    print("SPOT merge service — API tests\n")
    tests = [
        ("Landing page renders with no-store headers", test_landing),
        ("Health endpoint", test_health),
        ("Contact form email notification is called", test_contact_form_email_notification_is_called),
        ("Validation accepts well-formed XML", test_validate_xml_ok),
        ("Validation rejects malformed XML", test_validate_xml_rejects_invalid_xml),
        ("CSV XML generation succeeds", test_generate_xml_from_csv),
        ("CSV XML generation rejects missing required fields", test_generate_xml_rejects_missing_required_fields),
        ("Excel XML generation succeeds", test_generate_xml_from_excel),
        ("Custom mapping accepts spreadsheet headers", test_generate_xml_accepts_custom_mapping),
        ("Sample CSV template download works", test_sample_csv_template_download),
        ("Contact form reports delivery failure", test_contact_form_reports_delivery_failure),
        ("Metrics fallback handles write failures", test_metrics_fallback_when_disk_writes_fail),
        ("Three listings merge, sum and validate", test_merge_happy),
        ("Colliding guest codes are re-coded", test_guest_code_collision),
        ("Guest codes stay stable across months", test_listing_state_roundtrip),
        ("Non-XML upload rejected clearly", test_rejects_non_xml),
        ("Broken XML rejected clearly", test_rejects_broken_xml),
        ("Empty request rejected", test_rejects_empty_request),
        ("Date gap surfaces in the response", test_date_gap_surfaces),
        ("Too many files rejected", test_too_many_files),
        ("Nothing is written to disk", test_nothing_written_to_disk),
        ("Web output identical to the desktop tool", test_matches_desktop_tool),
    ]
    ok = sum(1 for name, fn in tests if check(name, fn))
    print("\n%d/%d passed" % (ok, len(tests)))
    sys.exit(0 if ok == len(tests) else 1)
