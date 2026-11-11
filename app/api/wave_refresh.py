"""
Wave File refresh API.

Users upload the weekly Wave export. It is validated, archived, loaded into
the PBI staging table and merged in one transaction, and the dashboard
savings totals are returned before and after. Every check and refresh is
recorded in InternalUpload.UserActivityLog, and each refresh attempt also in
InternalUpload.ETLHealth.
"""
import os
import secrets

import pyodbc
from flask import current_app, g, jsonify, request, session
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api import wave_refresh_bp
from app.models import WAVE_REFRESH, UserActivityLog
from app.utils.activity import ACCESS_DENIED, FILE_REFRESH, FILE_VALIDATE, log_activity
from app.utils.decorators import login_required
from app.utils.etl_health import log_wave_refresh
from app.utils.timezone import now_eastern
from app.utils.wave_refresh import (
    DEFAULT_MAX_EXPORT_AGE_DAYS,
    PowerBiSchedule,
    WaveRefreshError,
    apply_refresh,
    archive_export,
    compare_with_database,
    current_totals,
    parse_export,
    target_label,
)

CSRF_SESSION_KEY = 'wave_refresh_csrf'
HISTORY_LIMIT = 10


def _forbidden():
    log_activity(ACCESS_DENIED, 'denied', message=f'No Wave refresh permission for {request.path}')
    return jsonify({'error': 'You do not have access to the Wave File refresh.'}), 403


def _csrf_valid():
    token = session.get(CSRF_SESSION_KEY)
    supplied = request.form.get('csrf_token', '')
    return bool(token) and secrets.compare_digest(token.encode(), supplied.encode())


def _archive_folder():
    folder = current_app.config['WAVE_ARCHIVE_PATH']
    if folder.startswith('\\'):  # UNC path read from .env with extra backslashes
        folder = '\\\\' + folder.lstrip('\\')
    return os.path.normpath(folder)


def _database_message(exc):
    """Shorten a pyodbc error to the SQL Server message for display."""
    text = str(exc.args[1] if len(exc.args) > 1 else exc)
    return text.split('[SQL Server]')[-1].split(' (SQLExecDirectW)')[0].strip()[:400]


def _refreshes():
    """
    Refresh attempts against the configured target, newest first.

    Filtered by target rather than environment: a load from a development run
    still changed this database, so it belongs in this history.
    """
    return (
        UserActivityLog.query
        .filter_by(activity_type=FILE_REFRESH, target_database=target_label(current_app.config))
        .order_by(UserActivityLog.started_at.desc())
    )


def _history():
    return [
        {
            'started_at': row.started_at.isoformat() if row.started_at else None,
            'started_by': row.full_name or row.username,
            'status': row.status,
            'file_name': row.file_name,
            'rows': row.row_count,
            'message': row.message,
        }
        for row in _refreshes().limit(HISTORY_LIMIT).all()
    ]


def _last_refresh(schedule):
    """When the latest successful refresh from this page should show in Power BI."""
    latest = _refreshes().filter_by(status='success').first()
    if not latest or not latest.ended_at:
        return None
    return {**schedule.estimate(latest.ended_at), 'started_by': latest.full_name or latest.username}


@wave_refresh_bp.route('/status', methods=['GET'])
@login_required
def refresh_status():
    """Current dashboard totals from PBI plus recent refresh history."""
    if not g.current_user.can_use_module(WAVE_REFRESH):
        return _forbidden()

    schedule = PowerBiSchedule.from_config(current_app.config)
    payload = {
        'history': [],
        'last_refresh': None,
        'if_loaded_now': schedule.estimate(now_eastern()),
        'target': target_label(current_app.config),
    }
    try:
        payload.update(history=_history(), last_refresh=_last_refresh(schedule))
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Could not read the activity log')
        payload['history_error'] = 'Could not read the refresh history.'
    try:
        payload['totals'] = current_totals(current_app.config, now_eastern().year)
    except pyodbc.Error as exc:
        current_app.logger.warning('Wave totals query failed: %s', exc)
        payload['totals_error'] = f'Could not read the dashboard totals: {_database_message(exc)}'
    return jsonify(payload)


@wave_refresh_bp.route('/upload', methods=['POST'])
@login_required
def upload_export():
    """Validate an export (mode=validate) or validate and load it (mode=refresh)."""
    if not g.current_user.can_use_module(WAVE_REFRESH):
        return _forbidden()
    if not _csrf_valid():
        log_activity(ACCESS_DENIED, 'denied', message='Upload refused: missing or invalid session token')
        return jsonify({'error': 'Your session expired. Reload the page and try again.'}), 400

    config = current_app.config
    mode = request.form.get('mode', 'validate')
    if mode not in ('validate', 'refresh'):
        return jsonify({'error': 'Unknown upload mode.'}), 400
    if mode == 'refresh' and not config['WAVE_REFRESH_ENABLED']:
        return jsonify({'error': 'Loading is turned off in this environment. Nothing was changed.'}), 403
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'Choose the Wave export file to upload.'}), 400
    content = upload.read()

    started_at = now_eastern()
    year = started_at.year
    receipt = {
        'mode': mode,
        'status': 'failed',
        'database_updated': False,
        'file_name': upload.filename,
        'file_size_bytes': len(content),
        'realization_year': year,
        'target': target_label(config),
    }
    status_code = 200

    try:
        export = parse_export(
            upload.filename,
            content,
            today=started_at.date(),
            max_age_days=config.get('WAVE_MAX_EXPORT_AGE_DAYS', DEFAULT_MAX_EXPORT_AGE_DAYS),
        )
        receipt.update(export.summary())

        if mode == 'validate':
            try:
                receipt.update(compare_with_database(config, export))
            except pyodbc.Error as exc:
                receipt['warnings'].append(
                    f'Could not compare with the data already loaded: {_database_message(exc)}'
                )
            receipt.update(status='validated', message='The file passed every check. Nothing was loaded.')
        else:
            # Read first so bad schedule settings stop the upload, not fail it after the commit.
            schedule = PowerBiSchedule.from_config(config)
            try:
                receipt['archived_path'] = archive_export(_archive_folder(), export, started_at)
            except OSError as exc:
                current_app.logger.exception('Wave export archive failed')
                raise WaveRefreshError(
                    'Could not save a copy of the export to the shared drive, so nothing was loaded.',
                    details=[str(exc)],
                    status_code=500,
                ) from exc

            receipt.update(apply_refresh(config, export, year))
            receipt.update(
                status='refreshed',
                database_updated=True,
                message='The export was loaded and merged into the dashboard tables.',
                power_bi=schedule.estimate(now_eastern()),
            )
    except WaveRefreshError as exc:
        receipt.update(message=str(exc), details=exc.details)
        status_code = exc.status_code
    except pyodbc.Error as exc:
        current_app.logger.exception('Wave refresh database error')
        receipt.update(
            message='The PBI database rejected the refresh. Nothing was changed.',
            details=[_database_message(exc)],
        )
        status_code = 502
    except Exception:
        current_app.logger.exception('Unexpected Wave upload error')
        receipt.update(message='Something went wrong while processing the file. Contact the procurement data team.')
        status_code = 500

    log_activity(
        FILE_REFRESH if mode == 'refresh' else FILE_VALIDATE,
        'success' if receipt['status'] in ('validated', 'refreshed') else 'failed',
        receipt=receipt,
        started_at=started_at,
    )
    if mode == 'refresh':
        log_wave_refresh(receipt, started_at)
    return jsonify(receipt), status_code
