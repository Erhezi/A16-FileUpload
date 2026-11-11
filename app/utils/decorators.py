"""
Authentication: IIS Windows Authentication identifies the user, as in the
Savings Tracker, and InternalUpload.Users decides who gets in.

Deliberate differences from the tracker's decorators.py:
  * The X-Remote-User header is not trusted. Any client can send it, and this
    app writes to the production dashboard tables.
  * No "view as role" preview, since it only served the tracker's admin screens.
  * Nobody is added on first login. Only listed users get in.
"""
import os
from functools import wraps

from flask import current_app, g, jsonify, request

from app.models import User
from app.utils.activity import ACCESS_DENIED, log_activity


def get_authenticated_user():
    """Return the user IIS authenticated, or None when there is no authenticated user."""
    username = request.environ.get('REMOTE_USER') or request.environ.get('AUTH_USER')
    if not username and current_app.config.get('DEV_BYPASS_AUTH'):
        username = os.environ.get('USERNAME') or os.environ.get('USER') or 'devuser'
    if not username:
        return None

    username = username.split('\\')[-1]  # DOMAIN\username -> username
    # Never saved: this only carries an unlisted person's name to the Request Access page and
    # the activity log. With no role, can_use_module refuses them.
    return User.query.filter_by(username=username).first() or User(
        username=username, full_name=username, email='', is_active=True)


def login_required(f):
    """Require an active, authenticated user and expose it as g.current_user."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if user is None:
            return jsonify({'error': 'Authentication required'}), 401
        g.current_user = user
        if not user.is_active:
            log_activity(ACCESS_DENIED, 'denied', message=f'Inactive account requested {request.path}')
            return jsonify({'error': 'User account is inactive'}), 403
        return f(*args, **kwargs)
    return decorated_function
