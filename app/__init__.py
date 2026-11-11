"""
Flask application factory.
"""
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

from app.utils.log_file import AppendFileHandler

load_dotenv()

db = SQLAlchemy()


def start_file_log(app):
    """Keep errors the app handles itself: under wfastcgi they otherwise reach no file at all."""
    path = app.config['LOG_FILE']
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Not a rotating log: IIS can run several processes, and Windows cannot rename a file
        # that another process has open. Each write is a true append, so none overwrites another.
        handler = AppendFileHandler(path, encoding='utf-8')
    except OSError:
        # As with the activity log, a logging problem must never stop an upload.
        app.logger.exception('Could not open %s', path)
        return
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(process)d] %(message)s'))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Started, environment=%s', app.config['ENVIRONMENT'])


def create_app(config_name='production'):
    app = Flask(__name__)

    from app.config import config
    app.config.from_object(config[config_name])
    app.config['ENVIRONMENT'] = config_name
    start_file_log(app)
    if not app.config['SECRET_KEY']:
        # The session carries the CSRF token, so a guessable key would defeat that check.
        raise RuntimeError('Set FILEUPLOAD_SECRET_KEY in .env before starting the app.')

    db.init_app(app)

    from app.api import wave_refresh_bp
    from app.views import main_bp
    app.register_blueprint(wave_refresh_bp, url_prefix='/api/wave-refresh')
    app.register_blueprint(main_bp)

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(413)
    def too_large(error):
        return jsonify({'error': f"The file is larger than {app.config['MAX_UPLOAD_MB']} MB."}), 413

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return jsonify({'error': 'Internal server error'}), 500

    return app
