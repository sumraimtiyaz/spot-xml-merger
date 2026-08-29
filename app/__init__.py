"""Application factory."""

from flask import Flask

from .config import get_config


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    from .views import bp
    app.register_blueprint(bp)

    return app
