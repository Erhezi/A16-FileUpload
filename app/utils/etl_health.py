"""
Writes the run history of the Wave load (InternalUpload.ETLHealth).

Filled the way the Medline jobs fill MedlineReport.ETLHealth: LastRunTime is when the run
started, Duration is whole seconds, and TaskStatus is SUCCESS or FAIL. Anything that does
not apply to a run stays NULL.
"""
import os

from flask import current_app

from app import db
from app.models import ETLHealth
from app.utils.timezone import now_eastern

PROCESS_NAME = 'Wave Refresh'
PROCESS_ID = 'WAVEREFRESH_W_DL'
PROCESS_FREQUENCY = 'Weekly'


def _error_text(receipt):
    return '\n'.join([receipt.get('message') or '', *receipt.get('details', [])]).strip()


def log_wave_refresh(receipt, started_at):
    """
    Record one Upload & Refresh attempt, whether it loaded or not.

    As with the activity log, a failed write never changes the response: a load that
    already reached PBI must still report its real result.
    """
    config = current_app.config
    database = config['WAVE_DB_NAME']
    succeeded = receipt['status'] == 'refreshed'
    try:
        db.session.add(ETLHealth(
            process_name=PROCESS_NAME,
            process_id=PROCESS_ID,
            source_file_path=receipt.get('archived_path'),
            # Stored as naive Eastern, as in the activity log.
            last_run_time=started_at.replace(tzinfo=None),
            target_table_name=f'[{database}].[SCSSaving].[Initiatives]',
            task_status='SUCCESS' if succeeded else 'FAIL',
            # A failed load rolls back, so nothing was loaded whatever the file held.
            row_count=receipt['rows'] if succeeded else 0,
            package_path=os.path.join(os.path.dirname(current_app.root_path), 'run.py'),
            log_file_path=config['LOG_FILE'] or None,
            stg_table_name=f'[{database}].[SCSSaving].[Initiatives_stg]',
            process_frequency=PROCESS_FREQUENCY,
            error=None if succeeded else _error_text(receipt),
            duration=int((now_eastern() - started_at).total_seconds()),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not record the Wave Refresh run in ETLHealth')
