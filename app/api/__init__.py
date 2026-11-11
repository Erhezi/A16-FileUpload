from flask import Blueprint

wave_refresh_bp = Blueprint('wave_refresh', __name__)

from app.api import wave_refresh  # noqa: E402,F401  (registers the routes)
