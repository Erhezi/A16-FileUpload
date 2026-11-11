"""
HTML pages.
"""
import secrets

from flask import Blueprint, current_app, g, redirect, render_template, session, url_for

from app.api.wave_refresh import CSRF_SESSION_KEY
from app.models import WAVE_REFRESH
from app.utils.activity import ACCESS_DENIED, PAGE_VIEW, log_activity
from app.utils.decorators import login_required
from app.utils.wave_refresh import PowerBiSchedule, target_label

main_bp = Blueprint('main', __name__)


@main_bp.app_context_processor
def inject_template_globals():
    return {
        'template_current_user': g.get('current_user'),
        'template_environment': current_app.config['ENVIRONMENT'],
    }


@main_bp.route('/')
def index():
    return redirect(url_for('main.wave_refresh'))


@main_bp.route('/wave-refresh')
@login_required
def wave_refresh():
    """Serve the Wave File upload page that refreshes the PBI savings tables."""
    user = g.current_user
    if not user.can_use_module(WAVE_REFRESH):
        log_activity(ACCESS_DENIED, 'denied', message='No Wave refresh permission for the upload page')
        return render_template('access_denied.html'), 403

    log_activity(PAGE_VIEW, 'success')
    if CSRF_SESSION_KEY not in session:
        session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    config = current_app.config
    return render_template(
        'wave_refresh.html',
        csrf_token=session[CSRF_SESSION_KEY],
        target=target_label(config),
        max_age_days=config['WAVE_MAX_EXPORT_AGE_DAYS'],
        pbi_report_url=config['WAVE_PBI_REPORT_URL'],
        pbi_schedule_text=PowerBiSchedule.from_config(config).describe(),
        refresh_enabled=config['WAVE_REFRESH_ENABLED'],
    )
