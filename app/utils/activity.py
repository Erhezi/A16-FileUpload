"""
Writes the user activity log (InternalUpload.UserActivityLog).
"""
import json
import math

from flask import current_app, g, request

from app import db
from app.models import UserActivityLog
from app.utils.timezone import now_eastern

PAGE_VIEW = 'PAGE_VIEW'
ACCESS_DENIED = 'ACCESS_DENIED'
FILE_VALIDATE = 'FILE_VALIDATE'
FILE_REFRESH = 'FILE_REFRESH'

UPLOAD_TYPE_WAVE = 'WAVE'

# The only receipt fields kept in DetailJson.
DETAIL_FIELDS = ('mode', 'database_updated', 'realization_year')


def _clip(value, limit):
    return value[:limit] if value else value


def _kb(size_bytes):
    """Whole KB rounded up, the way Windows Explorer shows file sizes."""
    return None if size_bytes is None else math.ceil(size_bytes / 1024)


def _detail_json(receipt):
    detail = {key: receipt[key] for key in DETAIL_FIELDS if key in receipt}
    return json.dumps(detail) if detail else None


def log_activity(activity_type, status, message=None, receipt=None, started_at=None):
    """
    Record one action by the signed-in user.

    Logging failures never change the response: a refresh that already reached
    the database must still report its real result to the user.
    """
    user = g.current_user
    receipt = receipt or {}
    ended_at = now_eastern()
    started_at = started_at or ended_at
    try:
        db.session.add(UserActivityLog(
            activity_type=activity_type,
            upload_type=UPLOAD_TYPE_WAVE,
            status=status,
            username=user.username,
            full_name=user.full_name,
            role_name=user.role.name if user.role else None,
            file_name=_clip(receipt.get('file_name'), 260),
            file_size_kb=_kb(receipt.get('file_size_bytes')),
            row_count=receipt.get('rows'),
            target_database=receipt.get('target'),
            archived_path=_clip(receipt.get('archived_path'), 500),
            message=_clip(message or receipt.get('message'), 1000),
            detail_json=_detail_json(receipt),
            ip_address=request.remote_addr,
            user_agent=_clip(request.user_agent.string, 500),
            environment=current_app.config['ENVIRONMENT'],
            # Stored as naive Eastern, the same convention as the Savings Tracker.
            started_at=started_at.replace(tzinfo=None),
            ended_at=ended_at.replace(tzinfo=None),
            duration_seconds=(ended_at - started_at).total_seconds(),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not record %s activity for %s', activity_type, user.username)
