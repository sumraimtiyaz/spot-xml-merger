"""Application factory."""

from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from .config import get_config


def _origin_allowed(app, origin):
    if not origin:
        return True
    allowed = set(app.config.get("CORS_ALLOWED_ORIGINS", []))
    if not allowed:
        return request.host == urlsplit(origin).netloc
    return origin in allowed or request.host == urlsplit(origin).netloc


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        if not origin:
            return response
        if _origin_allowed(app, origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        else:
            response.headers.pop("Access-Control-Allow-Origin", None)
            response.headers.pop("Access-Control-Allow-Methods", None)
            response.headers.pop("Access-Control-Allow-Headers", None)
            response.headers.pop("Access-Control-Allow-Credentials", None)
        return response

    @app.before_request
    def enforce_api_security():
        if request.method == "OPTIONS":
            return None

        protected = {
            "/api/merge",
            "/api/validate-xml",
            "/api/generate-xml",
        }
        if request.path not in protected:
            return None

        if not app.config.get("API_AUTH_REQUIRED", False):
            return None

        header = request.headers.get("Authorization", "")
        token = (header or "").strip()
        if token.startswith("Bearer "):
            token = token[7:].strip()
        expected = (app.config.get("API_AUTH_TOKEN") or "").strip()
        if expected and token == expected:
            return None

        origin = request.headers.get("Origin")
        if origin and _origin_allowed(app, origin):
            return None

        return jsonify(error="Authentication required for this API endpoint."), 401

    from .views import bp
    app.register_blueprint(bp)

    return app
