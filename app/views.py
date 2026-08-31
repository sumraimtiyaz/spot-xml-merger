"""HTTP routes. The merge itself lives in app/engine/merge_istat.py."""

import csv
import io
import json
import smtplib
import time
from collections import defaultdict, deque
from email.message import EmailMessage
from urllib import request as urllib_request

from flask import (Blueprint, Response, current_app, jsonify, render_template, request)

from .accounts import Capability, can, current_actor
from .messages import localise_all
from .metrics import (
    metrics_payload,
    record_action,
    record_contact_submission,
    record_visitor,
)
from .engine.merge_istat import MergeError, validate_text_against_schema
from .generator import generate_xml_from_csv
from .regions import DEFAULT_REGION, PLANNED, enabled_regions, get_region

bp = Blueprint("main", __name__)

# Per-process sliding-window limiter. Enough for a single instance; swap for
# Redis if the service is ever load-balanced.
_HITS = defaultdict(deque)


def client_ip():
    if current_app.config.get("TRUST_PROXY_HEADER"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited():
    limit = current_app.config.get("RATE_LIMIT_PER_HOUR", 60)
    if limit <= 0:
        return False
    now = time.time()
    hits = _HITS[client_ip()]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def _excel_rows_to_csv_bytes(workbook):
    sheet = workbook.active
    headers = []
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if not headers:
            headers = [str(cell).strip() if cell is not None else "" for cell in row]
            continue
        item = {}
        for idx, header in enumerate(headers):
            value = row[idx] if idx < len(row) else ""
            item[header] = value
        if any(value is not None and str(value).strip() for value in item.values()):
            rows.append(item)
    if not rows:
        raise ValueError("The Excel file contains no data rows.")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: "" if row.get(header) is None else str(row.get(header)).strip() for header in headers})
    return output.getvalue().encode("utf-8")


def send_contact_email(name, email, category, message):
    api_key = (current_app.config.get("RESEND_API_KEY") or "").strip()
    if api_key:
        sender = (current_app.config.get("RESEND_FROM") or current_app.config.get("SMTP_FROM") or "").strip() or "noreply@example.com"
        recipients = [item.strip() for item in str(current_app.config.get("SMTP_TO") or "").split(",") if item.strip()]
        if not recipients:
            recipients = [sender]
        payload = {
            "from": sender,
            "to": recipients,
            "subject": f"[UnisciSPOT] {category or 'contact'} request",
            "text": "\n".join([
                f"Name: {name or 'anonymous'}",
                f"Email: {email or 'not provided'}",
                f"Category: {category or 'other'}",
                "",
                message,
            ]),
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            "https://api.resend.com/emails",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=15) as resp:
                status = getattr(resp, "status", resp.getcode())
                if status in {200, 201, 202}:
                    return True
                current_app.logger.warning("Resend email failed with status %s", status)
                return False
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Resend contact email notification failed")
            return False

    if not current_app.config.get("SMTP_ENABLED"):
        return False

    smtp_host = current_app.config.get("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(current_app.config.get("SMTP_PORT", 587) or 587)
    username = (current_app.config.get("SMTP_USERNAME") or "").strip()
    password = (current_app.config.get("SMTP_PASSWORD") or "").strip()
    sender = (current_app.config.get("SMTP_FROM") or "").strip() or username or "noreply@example.com"
    recipients = [item.strip() for item in str(current_app.config.get("SMTP_TO") or "").split(",") if item.strip()]
    if not recipients:
        recipients = [sender]

    if not username or not password:
        current_app.logger.warning("SMTP email not sent: missing SMTP_USERNAME or SMTP_PASSWORD")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[UnisciSPOT] {category or 'contact'} request"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "\n".join([
            f"Name: {name or 'anonymous'}",
            f"Email: {email or 'not provided'}",
            f"Category: {category or 'other'}",
            "",
            message,
        ])
    )

    try:
        if current_app.config.get("SMTP_USE_SSL"):
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if current_app.config.get("SMTP_USE_TLS", True):
                    server.starttls()
                server.login(username, password)
                server.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        current_app.logger.exception("contact email notification failed")
        return False


@bp.after_request
def no_store(response):
    # Nothing here should ever be cached by a proxy or a browser.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@bp.get("/")
def index():
    record_visitor()
    return render_template(
        "index.html",
        site_name=current_app.config["SITE_NAME"],
        regions=enabled_regions(),
        planned=PLANNED,
        default_region=DEFAULT_REGION,
        max_files=current_app.config["MAX_FILES"],
        max_bytes=current_app.config["MAX_CONTENT_LENGTH"],
        contact_email=current_app.config.get("CONTACT_EMAIL", ""),
        plausible_domain=current_app.config.get("PLAUSIBLE_DOMAIN", ""),
    )


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok")


@bp.get("/api/sample-csv")
def api_sample_csv():
    sample = """date,codiceclientesr,sesso,cittadinanza,comuneresidenza,occupazionepostoletto,dayuse,tipologiaalloggiato,eta,cameredisponibili,postilettodisponibili,camereoccupate
2026-06-01,ABC123,M,100000100,412058091,si,no,16,40,2,4,1
2026-06-02,ABC456,F,100000100,412058091,si,no,16,38,2,4,1
"""
    return Response(
        sample,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=spot-template.csv"},
    )


@bp.get("/api/metrics")
def api_metrics():
    return jsonify(metrics_payload())


@bp.post("/api/validate-xml")
def api_validate_xml():
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return jsonify(error="No XML files were uploaded."), 400

    max_files = current_app.config["MAX_FILES"]
    if len(uploads) > max_files:
        return jsonify(
            error="Too many files (%d). The limit is %d."
                  % (len(uploads), max_files)), 400

    results = []
    for item in uploads:
        name = item.filename or "upload.xml"
        if not name.lower().endswith(".xml"):
            results.append({
                "name": name,
                "status": "invalid",
                "detail": "%s is not an .xml file. Upload the XML exports from your booking system." % name,
            })
            continue

        raw = item.read()
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(raw)
        except Exception as exc:  # noqa: BLE001
            results.append({
                "name": name,
                "status": "invalid",
                "detail": "%s is not valid XML: %s" % (name, exc),
            })
            continue

        detail = validate_text_against_schema(raw.decode("utf-8", errors="strict"))
        status, message = detail
        results.append({
            "name": name,
            "status": status,
            "detail": message,
        })

    if any(item["status"] in {"failed", "invalid"} for item in results):
        return jsonify(
            ok=False,
            files=results,
            message="One or more XML files failed validation.",
        ), 422

    return jsonify(
        ok=True,
        files=results,
        message="All uploaded XML files passed validation against the official SPOT schema.",
    )


@bp.post("/api/generate-xml")
def api_generate_xml():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify(error="No CSV or Excel file was uploaded."), 400
    if len(files) > 1:
        return jsonify(error="Please upload only one CSV file at a time."), 400

    upload = files[0]
    name = upload.filename or "guest-data.csv"
    lower = name.lower()
    if not (lower.endswith(".csv") or lower.endswith(".xlsx") or lower.endswith(".xls")):
        return jsonify(error="Upload a CSV, XLSX or XLS file."), 400

    raw_field_map = (request.form.get("field_map") or "").strip()
    field_map = {}
    if raw_field_map:
        try:
            parsed_map = json.loads(raw_field_map)
            if isinstance(parsed_map, dict):
                field_map = {str(k): str(v) for k, v in parsed_map.items() if v is not None}
            else:
                raise ValueError("Field map must be an object.")
        except ValueError:
            return jsonify(error="Column mapping data is invalid."), 400

    try:
        if lower.endswith(".csv"):
            payload = generate_xml_from_csv(upload.read(), field_map=field_map)
        else:
            try:
                import openpyxl
            except ImportError:
                return jsonify(error="Excel files require openpyxl to be installed in the runtime."), 400
            workbook = openpyxl.load_workbook(upload, read_only=True, data_only=True)
            payload = generate_xml_from_csv(_excel_rows_to_csv_bytes(workbook), field_map=field_map)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("CSV/XML generation failed")
        return jsonify(error="Could not generate the XML file from the uploaded data: %s" % exc), 400

    if payload["schema_status"] != "ok":
        return jsonify(
            error="The generated XML does not match the official SPOT schema.",
            schema={"status": payload["schema_status"], "detail": payload["schema_detail"]},
        ), 422

    return jsonify(
        ok=True,
        xml=payload["xml"],
        rows=payload["rows"],
        schema={"status": payload["schema_status"], "detail": payload["schema_detail"]},
        message="XML generated successfully.",
    )


@bp.post("/api/merge")
def api_merge():
    """
    Merge the uploaded monthly exports.

    Multipart form:
        files          one or more XML exports (required)
        region         region key (default: puglia)
        listing_state  optional JSON from a previous run, so guest-code
                       prefixes stay stable from month to month. The browser
                       keeps this - the server stores nothing.

    Nothing is written to disk and nothing about the contents is logged.
    """
    actor = current_actor()
    if not can(actor, Capability.MERGE):
        return jsonify(error="Not permitted."), 403

    if rate_limited():
        return jsonify(
            error="Too many merges from this address in the last hour. "
                  "Try again shortly."), 429

    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return jsonify(error="No files were uploaded."), 400

    max_files = current_app.config["MAX_FILES"]
    if len(uploads) > max_files:
        return jsonify(
            error="Too many files (%d). The limit is %d."
                  % (len(uploads), max_files)), 400

    payload = []
    for item in uploads:
        name = item.filename or "upload.xml"
        if not name.lower().endswith(".xml"):
            return jsonify(
                error="%s is not an .xml file. Upload the XML exports from "
                      "your booking system." % name), 400
        payload.append((name, item.read()))

    region = get_region(request.form.get("region"))

    import json

    state = None
    raw_state = (request.form.get("listing_state") or "").strip()
    if raw_state:
        try:
            parsed = json.loads(raw_state)
            if isinstance(parsed, dict) and isinstance(parsed.get("prefixes"), dict):
                state = parsed
        except ValueError:
            state = None

    try:
        result = region.merge(payload, listing_state=state)
    except MergeError as exc:
        record_action(success=False)
        return jsonify(error=str(exc)), 422
    except Exception:  # noqa: BLE001
        current_app.logger.exception("merge failed")
        record_action(success=False)
        return jsonify(
            error="Something went wrong while merging. The files were not "
                  "stored; nothing to clean up."), 500

    record_action(success=True)
    return jsonify(
        region=region.key,
        filename=result["filename"],
        xml=result["xml"],
        csv=result["csv"],
        period={"first": result["first"], "last": result["last"]},
        days=len(result["days"]),
        sources=result["sources"],
        columns=result["columns"],
        rows=result["rows"],
        warnings=result["warnings"],
        notes=localise_all(result["warnings"]),
        recoded=result["recoded"],
        listing_state=result["listing_state"],
        schema={"status": result["schema_status"],
                "detail": result["schema_detail"],
                "name": region.schema_name},
    )


@bp.post("/api/feedback")
def api_feedback():
    payload = request.get_json(silent=True) or request.form or {}
    raw = payload.get("satisfied")
    if raw is None:
        return jsonify(error="The satisfied flag is required."), 400
    satisfied = str(raw).strip().lower() in {"1", "true", "yes", "y", "ok"}
    record_action(success=True, satisfied=satisfied)
    return jsonify(ok=True, satisfied=satisfied)


@bp.post("/api/contact")
def api_contact():
    payload = request.get_json(silent=True) or request.form or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    category = (payload.get("category") or "other").strip().lower()
    message = (payload.get("message") or "").strip()

    if not message:
        return jsonify(error="Please add a message before submitting the form."), 400

    record_contact_submission(category)
    if email:
        current_app.logger.info("contact form from %s <%s> (%s): %s",
                                name or "anonymous",
                                email,
                                category,
                                message[:200])
    else:
        current_app.logger.info("contact form from %s (%s): %s",
                                name or "anonymous",
                                category,
                                message[:200])

    if not send_contact_email(name, email, category, message):
        return jsonify(
            error="Your message was received, but the delivery service is not configured correctly. Please try again later or contact the site owner directly.",
            ok=False,
        ), 502

    return jsonify(
        ok=True,
        redirect="/",
        message="Thanks — your message has been received. We will review it soon.")


@bp.errorhandler(413)
def too_large(_):
    limit = current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify(error="The upload is larger than %d MB." % limit), 413
