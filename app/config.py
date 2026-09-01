"""
Settings for the SPOT merge service.

Everything is read from the environment so the same image runs in dev and in
production. The feature flags at the bottom are the seams where accounts and
billing plug in later, without touching the merge path.
"""

import os


def _flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    # --- service ----------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-a-secret")
    SITE_NAME = os.environ.get("SITE_NAME", "UnisciSPOT")
    PLAUSIBLE_DOMAIN = os.environ.get("PLAUSIBLE_DOMAIN", "")

    # --- API security ------------------------------------------------------
    API_AUTH_REQUIRED = _flag("API_AUTH_REQUIRED", True)
    API_AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN", "")
    CORS_ALLOWED_ORIGINS = [
        item.strip() for item in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if item.strip()
    ]

    # --- email notifications ----------------------------------------------
    SMTP_ENABLED = _flag("SMTP_ENABLED", False)
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME or "")
    SMTP_TO = os.environ.get("SMTP_TO", SMTP_USERNAME or "")
    SMTP_USE_TLS = _flag("SMTP_USE_TLS", True)
    SMTP_USE_SSL = _flag("SMTP_USE_SSL", False)

    # --- upload limits ----------------------------------------------------
    # A month of daily blocks for one listing is a few hundred KB even with
    # guests, so these are generous.
    MAX_CONTENT_LENGTH = _int("MAX_UPLOAD_BYTES", 12 * 1024 * 1024)
    MAX_FILES = _int("MAX_FILES", 30)

    # --- abuse control ----------------------------------------------------
    RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 60)
    TRUST_PROXY_HEADER = _flag("TRUST_PROXY_HEADER", True)

    # --- privacy ----------------------------------------------------------
    # The service never writes uploads to disk and never logs their contents.
    # This flag exists so the promise is auditable in one place: if it is ever
    # turned off, the landing page copy changes with it.
    STORE_NOTHING = True

    # --- future work ------------------------------------------------------
    # Flip these on when accounts arrive. See app/accounts.py.
    FEATURE_ACCOUNTS = _flag("FEATURE_ACCOUNTS", False)
    FEATURE_BILLING = _flag("FEATURE_BILLING", False)
    FEATURE_HISTORY = _flag("FEATURE_HISTORY", False)


class DevConfig(Config):
    DEBUG = True


class ProdConfig(Config):
    DEBUG = False


def get_config():
    default_env = "production" if os.environ.get("VERCEL") else "dev"
    env = os.environ.get("APP_ENV", default_env).lower()
    return ProdConfig if env in {"prod", "production"} else DevConfig
