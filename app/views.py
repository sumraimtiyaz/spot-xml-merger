"""HTTP routes. The merge itself lives in app/engine/merge_istat.py."""

import smtplib
import time
from collections import defaultdict, deque
from email.message import EmailMessage

from flask import (Blueprint, current_app, jsonify, render_template, request)

from .accounts import Capability, can, current_actor
from .messages import localise_all
from .metrics import (
    metrics_payload,
    record_action,
    record_contact_submission,
    record_visitor,
)
from .engine.merge_istat import MergeError
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


def send_contact_email(name, email, category, message):
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


@bp.get("/api/metrics")
def api_metrics():
    return jsonify(metrics_payload())


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

    send_contact_email(name, email, category, message)

    return jsonify(
        ok=True,
        redirect="/",
        message="Thanks — your message has been received. We will review it soon.")


@bp.errorhandler(413)
def too_large(_):
    limit = current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify(error="The upload is larger than %d MB." % limit), 413
