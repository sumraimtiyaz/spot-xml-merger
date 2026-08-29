"""HTTP routes. The merge itself lives in app/engine/merge_istat.py."""

import time
from collections import defaultdict, deque

from flask import (Blueprint, current_app, jsonify, render_template, request)

from .accounts import Capability, can, current_actor
from .messages import localise_all
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
        return jsonify(error=str(exc)), 422
    except Exception:  # noqa: BLE001
        current_app.logger.exception("merge failed")
        return jsonify(
            error="Something went wrong while merging. The files were not "
                  "stored; nothing to clean up."), 500

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


@bp.errorhandler(413)
def too_large(_):
    limit = current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify(error="The upload is larger than %d MB." % limit), 413
