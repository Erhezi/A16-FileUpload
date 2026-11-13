"""
Auth, activity log and upload flow, run against in-memory SQLite.

No test touches SQL Server: the PBI calls and the archive are replaced, while
parse_export runs for real on a generated workbook.
"""
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import openpyxl
import pyodbc
import pytest

from app import create_app, db
from app.api import wave_refresh as wave_api
from app.config import DEFAULT_SERVER, DevelopmentConfig, ProductionConfig, TestingConfig
from app.models import WAVE_REFRESH, ETLHealth, User, UserActivityLog, UserRole
from app.utils import activity, etl_health
from app.utils.log_file import AppendFileHandler
from app.utils.timezone import now_eastern
from app.utils.wave_refresh import (
    SOURCE_TO_STAGING,
    PowerBiSchedule,
    WaveRefreshError,
    archive_export,
    check_not_older,
    native_created_at,
    parse_export,
)
from set_server_path import script_processor, update_web_config

ROOT =os.path.dirname(os.path.dirname(__file__))
EXCEL_PLACEHOLDER = (1980, 1, 1, 0, 0, 0)
CORE_NO_CREATED = (
    b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    b'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    b'<dcterms:modified xsi:type="dcterms:W3CDTF">2016-04-08T11:28:09Z</dcterms:modified></cp:coreProperties>'
)
PAGE = '/wave-refresh'
STATUS = '/api/wave-refresh/status'
UPLOAD = '/api/wave-refresh/upload'


def windows_user(name):
    return {'REMOTE_USER': f'DM_MONTYNT\\{name}'}


def export_bytes():
    """A minimal valid Wave export with two L5 initiatives."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'initiative'
    columns = [source for source, _ in SOURCE_TO_STAGING]
    sheet.append(columns)
    for number in (101, 102):
        row = dict.fromkeys(columns)
        row.update({
            '#': number,
            'Accountable workstream': 'Supply Chain',
            'Name': f'Initiative {number}',
            'Weekly status': 'Active',
            'Stage': 'L5',
            'Net recurring benefits (latest estimate, annualized)': 1.5,
        })
        sheet.append([row[column] for column in columns])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_name():
    return f'Weekly Full Export_{date.today():%m%d%Y}.xlsx'


def repackaged(core=None, entry_time=EXCEL_PLACEHOLDER):
    """A valid export with its package metadata replaced, as Wave and Excel each write it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(export_bytes())) as source, zipfile.ZipFile(buffer, 'w') as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == 'docProps/core.xml' and core is not None:
                content = core
            target.writestr(zipfile.ZipInfo(item.filename, date_time=entry_time), content)
    return buffer.getvalue()


def excel_saved(created):
    """Excel's shape: a dcterms:created property and placeholder 1980 zip entry times."""
    stamp = created.strftime('%Y-%m-%dT%H:%M:%SZ').encode()
    return repackaged(CORE_NO_CREATED.replace(
        b'<dcterms:modified', b'<dcterms:created xsi:type="dcterms:W3CDTF">' + stamp + b'</dcterms:created><dcterms:modified', 1,
    ))


def wave_native(utc_time):
    """Wave's own download: no dcterms:created, zip entries stamped in UTC."""
    return repackaged(CORE_NO_CREATED, entry_time=utc_time.timetuple()[:6])


@pytest.fixture
def file_log(tmp_path, monkeypatch):
    """Point the log file at a temp folder. List it before `client` so the app picks it up."""
    path = tmp_path / 'logs' / 'app.log'
    monkeypatch.setattr(TestingConfig, 'LOG_FILE', str(path))
    yield path
    logger = logging.getLogger('app')
    for handler in logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)


@pytest.fixture
def app():
    app = create_app('testing')
    app.config['WAVE_REFRESH_ENABLED'] = True
    with app.app_context():
        db.create_all()
        roles = {name: UserRole(name=name, module=WAVE_REFRESH) for name in ('Admin', 'User')}
        other_module = UserRole(name='User', module='another_module')
        db.session.add_all([*roles.values(), other_module])
        db.session.flush()
        db.session.add_all([
            User(username='uploader', full_name='Una Loader', email='', role_id=roles['User'].id, is_active=True),
            User(username='boss', full_name='Bo Ss', email='', role_id=roles['Admin'].id, is_active=True),
            User(username='outsider', full_name='Out Sider', email='', role_id=other_module.id, is_active=True),
            User(username='gone', full_name='Gone User', email='', role_id=roles['User'].id, is_active=False),
        ])
        db.session.commit()
        yield app
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def pbi(monkeypatch):
    """Replace every PBI and archive call, recording what ran."""
    calls = []

    def record(name, result):
        def fake(*args, **kwargs):
            calls.append(name)
            return result
        return fake

    monkeypatch.setattr(wave_api, 'current_totals', record('totals', {'realization_year': 2026, 'savings': []}))
    monkeypatch.setattr(wave_api, 'compare_with_database', record('compare', {'previous_staging_rows': 9}))
    monkeypatch.setattr(wave_api, 'archive_export', record('archive', 'archive/copy.xlsx'))
    monkeypatch.setattr(wave_api, 'apply_refresh', record('refresh', {'savings': []}))
    return calls


def csrf_token(client, username='uploader'):
    html = client.get(PAGE, environ_overrides=windows_user(username)).get_data(as_text=True)
    return re.search(r'WAVE_CSRF_TOKEN = "([^"]+)"', html).group(1)


def post_upload(client, mode, token, username='uploader', content=None, file_name=None):
    return client.post(
        UPLOAD,
        data={
            'mode': mode,
            'csrf_token': token,
            'file': (io.BytesIO(export_bytes() if content is None else content), file_name or export_name()),
        },
        content_type='multipart/form-data',
        environ_overrides=windows_user(username),
    )


def log_rows(activity_type=None):
    query = UserActivityLog.query.order_by(UserActivityLog.id)
    return (query.filter_by(activity_type=activity_type) if activity_type else query).all()


# --- Authentication -----------------------------------------------------------

def test_no_windows_identity_is_refused(client):
    assert client.get(PAGE).status_code == 401


def test_x_remote_user_header_is_not_trusted(client):
    assert client.get(PAGE, headers={'X-Remote-User': 'uploader'}).status_code == 401


def test_domain_prefix_is_removed(client, pbi):
    assert client.get(PAGE, environ_overrides=windows_user('uploader')).status_code == 200


def test_auth_user_is_accepted_like_remote_user(client, pbi):
    assert client.get(PAGE, environ_overrides={'AUTH_USER': 'uploader'}).status_code == 200


def test_person_not_on_the_list_is_denied_and_never_added(client):
    response = client.get(PAGE, environ_overrides=windows_user('newperson'))
    assert response.status_code == 403
    page = response.get_data(as_text=True)
    assert 'Request Access' in page
    assert 'newperson' in page
    assert User.query.filter_by(username='newperson').count() == 0
    row = log_rows(wave_api.ACCESS_DENIED)[0]
    assert (row.username, row.role_name) == ('newperson', None)


@pytest.mark.parametrize('username', ['uploader', 'boss'])
def test_admin_and_user_can_both_use_the_module(client, pbi, username):
    env = windows_user(username)
    assert client.get(PAGE, environ_overrides=env).status_code == 200
    assert client.get(STATUS, environ_overrides=env).status_code == 200
    response = post_upload(client, 'refresh', csrf_token(client, username), username=username)
    assert response.json['status'] == 'refreshed'


def test_inactive_user_cannot_use_a_module_even_with_a_role():
    role = UserRole(name='User', module=WAVE_REFRESH)
    assert User(is_active=True, role=role).can_use_module(WAVE_REFRESH)
    assert not User(is_active=False, role=role).can_use_module(WAVE_REFRESH)


def test_migration_seeds_the_module_the_app_checks():
    script = open(os.path.join(ROOT, 'migration', 'create_internal_upload_schema.sql'), encoding='utf-8').read()
    assert f"r.Module = '{WAVE_REFRESH}'" in script


@pytest.mark.parametrize('username', ['newperson', 'outsider'])
def test_anyone_without_a_wave_refresh_role_is_denied_everywhere(client, pbi, username):
    env = windows_user(username)
    assert client.get(PAGE, environ_overrides=env).status_code == 403
    assert client.get(STATUS, environ_overrides=env).status_code == 403
    assert post_upload(client, 'refresh', 'x', username=username).status_code == 403
    assert 'refresh' not in pbi
    assert len(log_rows(wave_api.ACCESS_DENIED)) == 3


def test_inactive_user_is_denied_and_logged(client):
    assert client.get(PAGE, environ_overrides=windows_user('gone')).status_code == 403
    assert log_rows(wave_api.ACCESS_DENIED)[0].username == 'gone'


# --- Activity log -------------------------------------------------------------

def test_page_view_records_who_and_where(client):
    client.get(PAGE, environ_overrides=windows_user('uploader'), headers={'User-Agent': 'pytest-agent'})
    row = log_rows('PAGE_VIEW')[0]
    assert (row.username, row.full_name, row.role_name) == ('uploader', 'Una Loader', 'User')
    assert (row.upload_type, row.status, row.environment) == ('WAVE', 'success', 'testing')
    assert row.user_agent == 'pytest-agent'
    assert row.started_at is not None


def test_validate_success_is_logged_and_never_loads(client, pbi):
    content = export_bytes()
    response = post_upload(client, 'validate', csrf_token(client), content=content)
    assert response.status_code == 200
    assert response.json['status'] == 'validated'
    assert 'refresh' not in pbi and 'archive' not in pbi
    row = log_rows('FILE_VALIDATE')[0]
    assert row.status == 'success'
    assert row.file_name == export_name()
    assert row.row_count == 2
    assert row.file_size_kb == -(-len(content) // 1024)
    assert row.target_database == 'PBI on test-server'
    assert json.loads(row.detail_json) == {
        'mode': 'validate', 'database_updated': False, 'realization_year': now_eastern().year}


@pytest.mark.parametrize('size_bytes, kb', [(None, None), (0, 0), (1, 1), (1024, 1), (1025, 2), (4041617, 3947)])
def test_file_size_is_whole_kb_rounded_up_like_explorer(size_bytes, kb):
    assert activity._kb(size_bytes) == kb


def test_detail_json_keeps_only_three_fields(client, pbi):
    post_upload(client, 'refresh', csrf_token(client))
    assert json.loads(log_rows('FILE_REFRESH')[0].detail_json) == {
        'mode': 'refresh', 'database_updated': True, 'realization_year': now_eastern().year}
    assert log_rows('PAGE_VIEW')[0].detail_json is None


def test_rejected_file_still_gets_the_three_fields(client, pbi):
    post_upload(client, 'validate', csrf_token(client), content=b'not a workbook')
    row = log_rows('FILE_VALIDATE')[0]
    assert json.loads(row.detail_json) == {
        'mode': 'validate', 'database_updated': False, 'realization_year': now_eastern().year}
    assert row.file_size_kb == 1


def test_rejected_file_is_logged_as_failed(client, pbi):
    response = post_upload(client, 'validate', csrf_token(client), content=b'not a workbook')
    assert response.status_code == 400
    row = log_rows('FILE_VALIDATE')[0]
    assert row.status == 'failed'
    assert 'could not be opened' in row.message


def test_refresh_success_is_logged_and_shown_in_history(client, pbi):
    response = post_upload(client, 'refresh', csrf_token(client))
    assert response.status_code == 200
    assert response.json['status'] == 'refreshed'
    assert pbi == ['archive', 'refresh']
    row = log_rows('FILE_REFRESH')[0]
    assert row.status == 'success'
    assert row.archived_path == 'archive/copy.xlsx'

    status = client.get(STATUS, environ_overrides=windows_user('uploader')).json
    assert status['history'][0]['started_by'] == 'Una Loader'
    assert status['history'][0]['status'] == 'success'
    assert status['last_refresh']['started_by'] == 'Una Loader'


def test_history_is_limited_to_the_configured_target(app, client, pbi):
    post_upload(client, 'refresh', csrf_token(client))
    app.config['WAVE_DB_SERVER'] = 'another-server'
    status = client.get(STATUS, environ_overrides=windows_user('uploader')).json
    assert status['history'] == []
    assert status['last_refresh'] is None


def test_refresh_database_error_is_reported_and_logged(client, pbi, monkeypatch):
    def fail(*args):
        raise pyodbc.Error('42000', '[42000] [SQL Server]Merge failed (SQLExecDirectW)')
    monkeypatch.setattr(wave_api, 'apply_refresh', fail)
    response = post_upload(client, 'refresh', csrf_token(client))
    assert response.status_code == 502
    assert response.json['details'] == ['Merge failed']
    row = log_rows('FILE_REFRESH')[0]
    assert row.status == 'failed'
    assert 'rejected the refresh' in row.message


def test_refresh_guard_error_keeps_its_status_code(client, pbi, monkeypatch):
    def busy(*args):
        raise WaveRefreshError('Another Wave refresh is running.', status_code=409)
    monkeypatch.setattr(wave_api, 'apply_refresh', busy)
    assert post_upload(client, 'refresh', csrf_token(client)).status_code == 409
    assert log_rows('FILE_REFRESH')[0].status == 'failed'


def test_unexpected_error_is_still_logged(client, pbi, monkeypatch):
    def crash(*args):
        raise ValueError('boom')
    monkeypatch.setattr(wave_api, 'apply_refresh', crash)
    assert post_upload(client, 'refresh', csrf_token(client)).status_code == 500
    assert log_rows('FILE_REFRESH')[0].status == 'failed'


def test_archive_failure_stops_the_load(client, pbi, monkeypatch):
    def no_share(*args):
        raise OSError('share unavailable')
    monkeypatch.setattr(wave_api, 'archive_export', no_share)
    response = post_upload(client, 'refresh', csrf_token(client))
    assert response.status_code == 500
    assert 'refresh' not in pbi


def test_bad_schedule_settings_stop_the_load_before_it_starts(app, client, pbi):
    app.config['WAVE_PBI_FIRST_REFRESH'] = 'not-a-time'  # The page itself cannot render with this.
    with client.session_transaction() as session:
        session[wave_api.CSRF_SESSION_KEY] = 'known-token'
    assert post_upload(client, 'refresh', 'known-token').status_code == 500
    assert pbi == []


def test_log_failure_does_not_change_the_refresh_result(client, pbi, monkeypatch):
    token = csrf_token(client)

    def broken_commit():
        raise RuntimeError('log database offline')
    monkeypatch.setattr(db.session, 'commit', broken_commit)
    response = post_upload(client, 'refresh', token)
    assert response.status_code == 200
    assert response.json['status'] == 'refreshed'


def test_status_survives_an_unreadable_log(client, pbi, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def unreadable():
        raise SQLAlchemyError('no table')
    monkeypatch.setattr(wave_api, '_history', unreadable)
    response = client.get(STATUS, environ_overrides=windows_user('uploader'))
    assert response.status_code == 200
    assert response.json['history_error']
    assert 'totals' in response.json


# --- ETL health ---------------------------------------------------------------

def test_refresh_writes_one_etl_health_row(client, pbi):
    before = now_eastern().replace(tzinfo=None)
    post_upload(client, 'refresh', csrf_token(client))
    (row,) = ETLHealth.query.all()
    assert row.process_name == 'Wave Refresh'
    assert row.process_id == 'WAVEREFRESH_W_DL'
    assert row.source_file_path == 'archive/copy.xlsx'
    assert before <= row.last_run_time <= now_eastern().replace(tzinfo=None)
    assert row.target_table_name == '[PBI].[SCSSaving].[Initiatives]'
    assert row.task_status == 'SUCCESS'
    assert row.row_count == 2
    assert os.path.normcase(row.package_path) == os.path.normcase(os.path.join(ROOT, 'run.py'))
    assert row.log_file_path is None
    assert row.stg_table_name == '[PBI].[SCSSaving].[Initiatives_stg]'
    assert row.process_frequency == 'Weekly'
    assert row.error is None
    assert row.duration is not None


def test_etl_health_points_at_the_app_log(file_log, client, pbi):
    post_upload(client, 'refresh', csrf_token(client))
    assert ETLHealth.query.one().log_file_path == str(file_log)


def test_etl_health_times_the_run_from_its_start_in_whole_seconds(app, monkeypatch):
    started = now_eastern()
    monkeypatch.setattr(etl_health, 'now_eastern', lambda: started + timedelta(seconds=6, milliseconds=900))
    etl_health.log_wave_refresh({'status': 'refreshed', 'rows': 2}, started)
    row = ETLHealth.query.one()
    assert row.duration == 6
    assert row.last_run_time == started.replace(tzinfo=None)


def test_check_file_only_writes_no_etl_health_row(client, pbi):
    post_upload(client, 'validate', csrf_token(client))
    assert ETLHealth.query.count() == 0


def test_failed_load_is_recorded_as_fail_with_no_rows(client, pbi, monkeypatch):
    def rejected(*args):
        raise pyodbc.Error('42000', '[42000] [SQL Server]Merge failed (SQLExecDirectW)')
    monkeypatch.setattr(wave_api, 'apply_refresh', rejected)
    post_upload(client, 'refresh', csrf_token(client))
    row = ETLHealth.query.one()
    assert row.task_status == 'FAIL'
    assert row.row_count == 0
    assert row.source_file_path == 'archive/copy.xlsx'
    assert row.error == 'The PBI database rejected the refresh. Nothing was changed.\nMerge failed'


def test_refused_file_is_recorded_as_fail_before_archiving(client, pbi):
    post_upload(client, 'refresh', csrf_token(client), content=b'not a workbook')
    row = ETLHealth.query.one()
    assert row.task_status == 'FAIL'
    assert row.row_count == 0
    assert row.source_file_path is None
    assert row.error
    assert 'archive' not in pbi


def test_attempts_that_never_start_write_no_etl_health_row(client, app, pbi):
    token = csrf_token(client)
    post_upload(client, 'refresh', 'wrong-token')
    app.config['WAVE_REFRESH_ENABLED'] = False
    post_upload(client, 'refresh', token)
    assert ETLHealth.query.count() == 0


def test_etl_health_failure_changes_nothing_else(file_log, client, pbi, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError('etl table offline')
    monkeypatch.setattr(etl_health, 'ETLHealth', broken)
    response = post_upload(client, 'refresh', csrf_token(client))
    assert response.status_code == 200
    assert response.json['status'] == 'refreshed'
    assert log_rows('FILE_REFRESH')[0].status == 'success'
    text = file_log.read_text(encoding='utf-8')
    assert 'Could not record the Wave Refresh run in ETLHealth' in text
    assert 'RuntimeError: etl table offline' in text


@pytest.mark.parametrize('model', [ETLHealth, User, UserRole, UserActivityLog])
def test_model_matches_the_migration(model):
    script = open(os.path.join(ROOT, 'migration', 'create_internal_upload_schema.sql'), encoding='utf-8').read()
    block = script[script.index(f'CREATE TABLE InternalUpload.{model.__tablename__} ('):]
    block = block[:re.search(r'^\s*, CONSTRAINT', block, re.M).start()]
    columns = re.findall(r'^\s+,?\s*\[?(\w+)\]?\s+\w+(?:\(\w+\))?\s+(NOT NULL|NULL)', block, re.M)
    assert [(name, nullable == 'NULL') for name, nullable in columns] == [
        (column.name, column.nullable) for column in model.__table__.columns
    ]


def test_failed_log_write_is_kept_in_the_log_file(file_log, client, pbi, monkeypatch):
    token = csrf_token(client)

    def broken_commit():
        raise RuntimeError('log database offline')
    monkeypatch.setattr(db.session, 'commit', broken_commit)
    assert post_upload(client, 'refresh', token).status_code == 200
    text = file_log.read_text(encoding='utf-8')
    assert 'Could not record FILE_REFRESH activity for uploader' in text
    assert 'RuntimeError: log database offline' in text


def test_unhandled_error_is_kept_in_the_log_file(file_log, client, monkeypatch):
    monkeypatch.setattr(wave_api, 'parse_export', lambda *args, **kwargs: 1 / 0)
    token = csrf_token(client)
    post_upload(client, 'validate', token)
    assert 'ZeroDivisionError' in file_log.read_text(encoding='utf-8')


def test_log_file_records_each_start_with_its_environment(file_log):
    create_app('testing')
    assert 'INFO' in file_log.read_text(encoding='utf-8')
    assert 'Started, environment=testing' in file_log.read_text(encoding='utf-8')


def test_app_starts_when_the_log_file_cannot_be_opened(tmp_path, monkeypatch):
    blocker = tmp_path / 'not-a-folder'
    blocker.write_text('')
    monkeypatch.setattr(TestingConfig, 'LOG_FILE', str(blocker / 'app.log'))
    assert create_app('testing').test_client().get('/static/css/app.css').status_code == 200


def test_app_log_uses_the_append_handler(file_log):
    app = create_app('testing')
    assert any(isinstance(handler, AppendFileHandler) for handler in app.logger.handlers)


LOG_WRITER = '''
import logging, sys
sys.path.insert(0, sys.argv[1])
from log_file import AppendFileHandler
logger = logging.getLogger('writer')
logger.addHandler(AppendFileHandler(sys.argv[2], encoding='utf-8'))
for i in range(1000):
    logger.warning('%s %03d %s', sys.argv[3], i, 'x' * 60)
'''


def test_processes_writing_one_log_at_once_lose_no_lines(tmp_path):
    # Several IIS processes share app.log. With plain 'a' mode this loses lines.
    path = tmp_path / 'app.log'
    utils = os.path.join(ROOT, 'app', 'utils')
    writers = [subprocess.Popen([sys.executable, '-c', LOG_WRITER, utils, str(path), name]) for name in 'ABCD']
    assert [writer.wait(timeout=60) for writer in writers] == [0, 0, 0, 0]
    lines = path.read_text(encoding='utf-8').splitlines()
    assert all(line.endswith('x' * 60) for line in lines)
    assert Counter(line.split()[0] for line in lines) == {'A': 1000, 'B': 1000, 'C': 1000, 'D': 1000}


def test_log_file_keeps_earlier_lines_and_stays_readable(tmp_path):
    path = tmp_path / 'app.log'
    path.write_text('earlier line\n', encoding='utf-8')
    handler = AppendFileHandler(str(path), encoding='utf-8')
    try:
        handler.emit(logging.makeLogRecord({'msg': 'new line', 'levelno': logging.INFO}))
        assert path.read_text(encoding='utf-8').splitlines() == ['earlier line', 'new line']
    finally:
        handler.close()


def test_production_logs_to_the_shared_drive():
    assert ProductionConfig.LOG_FILE == (
        r'\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\app_logs\app.log')


def test_local_runs_log_in_the_app_folder():
    # Compared without abspath: a path relative to the working folder must not pass.
    expected = os.path.join(ROOT, 'logs', 'app.log')
    assert os.path.normcase(DevelopmentConfig.LOG_FILE) == os.path.normcase(expected)


# --- Native creation date -----------------------------------------------------

def test_wave_native_file_is_dated_by_its_zip_entries_in_eastern_time():
    created = native_created_at(wave_native(datetime(2026, 11, 2, 21, 5, 46)))
    assert created.strftime('%Y-%m-%d %H:%M:%S') == '2026-11-02 16:05:46'


def test_excel_file_is_dated_by_its_created_property():
    created = native_created_at(excel_saved(datetime(2026, 9, 10, 21, 35, 11)))
    assert created.strftime('%Y-%m-%d %H:%M:%S') == '2026-09-10 17:35:11'


def test_evening_creation_uses_the_eastern_date_not_utc():
    assert native_created_at(excel_saved(datetime(2026, 10, 17, 2, 0, 0))).date() == date(2026, 10, 16)


def test_created_property_wins_over_zip_entries():
    content = repackaged(
        CORE_NO_CREATED.replace(
            b'<dcterms:modified', b'<dcterms:created xsi:type="dcterms:W3CDTF">2026-10-09T14:00:00Z</dcterms:created><dcterms:modified', 1,
        ),
        entry_time=(2026, 10, 16, 14, 0, 0),
    )
    assert native_created_at(content).date() == date(2026, 10, 9)


def test_file_with_no_native_date_is_refused_not_dated_today():
    content = repackaged(CORE_NO_CREATED)
    assert native_created_at(content) is None
    with pytest.raises(WaveRefreshError, match='Could not determine'):
        parse_export('Weekly Full Export_11042026.xlsx', content, today=date(2026, 11, 4))


def test_rename_cannot_make_a_stale_file_pass():
    today = date(2026, 11, 4)
    content = excel_saved(datetime(2026, 10, 2, 14, 0, 0))
    with pytest.raises(WaveRefreshError, match='days old'):
        parse_export(f'Weekly Full Export_{today:%m%d%Y}.xlsx', content, today=today)


def test_old_label_does_not_fail_a_fresh_file():
    today = date(2026, 11, 4)
    export = parse_export('Weekly Full Export_01012020.xlsx', excel_saved(datetime(2026, 11, 3, 14, 0, 0)), today=today)
    assert export.workbook_date == date(2026, 11, 3)


def test_future_creation_date_is_refused():
    with pytest.raises(WaveRefreshError, match='in the future'):
        parse_export('export.xlsx', excel_saved(datetime(2026, 11, 5, 14, 0, 0)), today=date(2026, 11, 4))


def test_loaded_stamp_is_the_native_creation_date():
    export = parse_export('export.xlsx', wave_native(datetime(2026, 11, 2, 21, 5, 46)), today=date(2026, 11, 4))
    assert {row[-1] for row in export.staging_rows()} == {date(2026, 11, 2)}


def test_database_guard_compares_only_the_native_date():
    today = date(2026, 11, 4)
    export = parse_export('Weekly Full Export_01012020.xlsx', excel_saved(datetime(2026, 11, 3, 14, 0, 0)), today=today)
    check_not_older(export, date(2026, 11, 3))
    with pytest.raises(WaveRefreshError, match='older than the data already loaded'):
        check_not_older(export, date(2026, 11, 4))


def test_archive_name_has_upload_and_creation_timestamps(tmp_path):
    export = parse_export(
        'Full export (for admin use only) - Wave - 20261102 (16h05m).xlsx',
        wave_native(datetime(2026, 11, 2, 21, 5, 46)),
        today=date(2026, 11, 4),
    )
    uploaded = datetime(2026, 11, 4, 16, 30, 27, 123456)
    path = archive_export(str(tmp_path), export, uploaded)
    assert os.path.basename(path) == (
        '20261104_163027_123456_created_20261102_160546_'
        'Full_export_for_admin_use_only_-_Wave_-_20261102_16h05m.xlsx'
    )
    assert open(path, 'rb').read() == export.content


def test_upload_receipt_reports_the_native_creation_time(client, pbi):
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    response = post_upload(client, 'validate', csrf_token(client), content=excel_saved(created))
    assert response.status_code == 200
    expected = created.astimezone(now_eastern().tzinfo)
    assert response.json['created_at'] == expected.isoformat()
    assert response.json['workbook_date'] == expected.date().isoformat()
    assert 'export_date' not in response.json


# --- Upload guards ------------------------------------------------------------

def test_wrong_csrf_token_is_refused_and_logged(client, pbi):
    csrf_token(client)
    assert post_upload(client, 'refresh', 'wrong').status_code == 400
    assert 'refresh' not in pbi
    assert 'session token' in log_rows(wave_api.ACCESS_DENIED)[0].message


def test_non_ascii_csrf_token_is_refused_cleanly(client, pbi):
    csrf_token(client)
    assert post_upload(client, 'refresh', 'tökén').status_code == 400


def test_refresh_is_refused_when_loading_is_off(app, client, pbi):
    app.config['WAVE_REFRESH_ENABLED'] = False
    token = csrf_token(client)
    response = post_upload(client, 'refresh', token)
    assert response.status_code == 403
    assert pbi == []
    assert post_upload(client, 'validate', token).status_code == 200


def test_page_disables_loading_when_it_is_off(app, client):
    app.config['WAVE_REFRESH_ENABLED'] = False
    html = client.get(PAGE, environ_overrides=windows_user('uploader')).get_data(as_text=True)
    assert 'WAVE_REFRESH_ENABLED = false' in html
    assert 'Loading is turned off' in html


def test_oversized_upload_gets_a_readable_error(app, client):
    token = csrf_token(client)
    app.config['MAX_CONTENT_LENGTH'] = 1024
    response = post_upload(client, 'validate', token, content=b'x' * 4096)
    assert response.status_code == 413
    assert 'larger than' in response.json['error']


def test_root_redirects_to_the_upload_page(client):
    response = client.get('/')
    assert response.status_code == 302
    assert response.location.endswith(PAGE)


# --- Startup safety -----------------------------------------------------------

def run_python(code, **env):
    """Run in a fresh process so config reads a clean environment (no .env, no FILEUPLOAD_ values)."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith('FILEUPLOAD_')}
    return subprocess.run(
        [sys.executable, '-c', f'import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n{code}'],
        cwd=ROOT, env={**clean, 'FILEUPLOAD_LOG_FILE': '', **env}, capture_output=True, text=True, timeout=60,
    )


def test_production_refuses_to_start_without_a_secret_key():
    result = run_python("from app import create_app; create_app('production')")
    assert result.returncode != 0
    assert 'Set FILEUPLOAD_SECRET_KEY' in result.stderr


def test_unset_environment_means_production():
    result = run_python('import run', FILEUPLOAD_SECRET_KEY='x')
    assert result.returncode == 0, result.stderr
    check = run_python("import run; print(run.ENV, run.app.config.get('DEV_BYPASS_AUTH'))",
                       FILEUPLOAD_SECRET_KEY='x')
    assert check.stdout.split() == ['production', 'None']


SHOW_SETTINGS = (
    "import run; c = run.app.config; "
    "print(run.ENV, c['DB_SERVER'], c['WAVE_DB_SERVER'], c['DB_DRIVER'], c['SECRET_KEY'], c['WAVE_ARCHIVE_PATH'], sep='|')"
)


def test_variables_set_for_other_apps_change_nothing():
    result = run_python(
        SHOW_SETTINGS, FILEUPLOAD_SECRET_KEY='ours', ENVIRONMENT='development', DB_SERVER='someone-elses',
        DB_DRIVER='Other Driver', SECRET_KEY='theirs', WAVE_ARCHIVE_PATH='C:\\elsewhere')
    assert result.stdout.strip().split('|') == [
        'production', DEFAULT_SERVER, DEFAULT_SERVER, 'ODBC Driver 17 for SQL Server', 'ours',
        r'\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\WaveExport'], result.stderr


def test_prefixed_variables_are_used():
    result = run_python(
        SHOW_SETTINGS, FILEUPLOAD_SECRET_KEY='ours', FILEUPLOAD_DB_SERVER='other-server',
        FILEUPLOAD_DB_DRIVER='ODBC Driver 18 for SQL Server', FILEUPLOAD_WAVE_ARCHIVE_PATH='C:\\archive')
    assert result.stdout.strip().split('|') == [
        'production', 'other-server', 'other-server', 'ODBC Driver 18 for SQL Server', 'ours', 'C:\\archive'
    ], result.stderr


@pytest.mark.parametrize('name', ['app/config.py', 'run.py'])
def test_settings_are_only_read_through_the_prefix(name):
    source = open(os.path.join(ROOT, name), encoding='utf-8').read()
    reads = re.findall(r'os\.(?:getenv|environ)\b.*', source)
    assert reads == (['os.getenv(PREFIX + name, default)'] if name == 'app/config.py' else [])


def test_web_config_pins_production_and_the_prefix():
    root = ET.parse(os.path.join(ROOT, 'web.config')).getroot()
    settings = {add.get('key'): add.get('value') for add in root.find('appSettings')}
    assert settings['FILEUPLOAD_ENVIRONMENT'] == 'production'
    assert settings['FILEUPLOAD_URL_PREFIX'] == '/fileupload'
    assert settings['WSGI_HANDLER'] == 'run.app'


def test_web_config_drops_handlers_inherited_from_the_server():
    handlers = ET.parse(os.path.join(ROOT, 'web.config')).getroot().find('system.webServer/handlers')
    assert [child.tag for child in handlers] == ['clear', 'add']


def test_web_config_names_a_folder_only_in_script_processor():
    root = ET.parse(os.path.join(ROOT, 'web.config')).getroot()
    settings = {add.get('key'): add.get('value') for add in root.find('appSettings')}
    assert 'PYTHONPATH' not in settings
    assert settings['WSGI_LOG'] == 'logs\\wfastcgi.log'
    assert not any(re.match(r'[A-Za-z]:', value) for value in settings.values())
    processor = root.find('system.webServer/handlers/add').get('scriptProcessor')
    assert processor.startswith('D:\\FileUpload\\A16-FileUpload\\.venv\\')


@pytest.mark.parametrize('folder', ['E:\\Apps\\Upload', 'D:\\File Upload\\R&D "copy"'])
def test_set_server_path_writes_what_wfastcgi_enable_registers(tmp_path, folder):
    web_config = tmp_path / 'web.config'
    shutil.copy(os.path.join(ROOT, 'web.config'), web_config)
    python = f'{folder}\\.venv\\Scripts\\python.exe'
    wfastcgi = f'{folder}\\.venv\\Lib\\site-packages\\wfastcgi.py'
    value = script_processor(python, wfastcgi)
    quote = '"' if ' ' in folder else ''
    assert value == f'{quote}{python}{quote}|{quote}{wfastcgi}{quote}'

    update_web_config(str(web_config), value)
    update_web_config(str(web_config), value)
    root = ET.parse(web_config).getroot()
    assert root.find('system.webServer/handlers/add').get('scriptProcessor') == value
    assert 'The only fixed path in this file' in web_config.read_text(encoding='utf-8')
    original = open(os.path.join(ROOT, 'web.config'), encoding='utf-8').read()
    assert len(web_config.read_text(encoding='utf-8').splitlines()) == len(original.splitlines())


def test_set_server_path_refuses_any_python_but_the_apps_own(tmp_path):
    # Runs a copy: from the project's own .venv, the real script would rewrite the real web.config.
    for name in ('set_server_path.py', 'web.config'):
        shutil.copy(os.path.join(ROOT, name), tmp_path)
    before = (tmp_path / 'web.config').read_bytes()
    result = subprocess.run(
        [sys.executable, str(tmp_path / 'set_server_path.py')],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "the app's own Python" in result.stderr
    assert (tmp_path / 'web.config').read_bytes() == before


def test_web_config_environment_beats_a_copied_env_file(tmp_path):
    copied = tmp_path / '.env'
    copied.write_text('FILEUPLOAD_ENVIRONMENT=development\nFILEUPLOAD_SECRET_KEY=x\n')
    code = (
        'import dotenv, functools\n'
        f'dotenv.load_dotenv = functools.partial(dotenv.load_dotenv, {str(copied)!r})\n'
        "import run; print(run.ENV, run.app.config.get('DEV_BYPASS_AUTH'))"
    )
    clean = {key: value for key, value in os.environ.items() if not key.startswith('FILEUPLOAD_')}
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT,
        env={**clean, 'FILEUPLOAD_ENVIRONMENT': 'production', 'FILEUPLOAD_LOG_FILE': ''},
        capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.split() == ['production', 'None'], result.stderr


def test_prefix_handles_the_paths_wfastcgi_passes_in():
    # wfastcgi clears SCRIPT_NAME and sends the full URL as PATH_INFO, also under an IIS alias.
    code = (
        'import run\n'
        'from werkzeug.test import Client\n'
        'client = Client(run.app)\n'
        "for path in ('/fileupload', '/FileUpload/', '/', '/fileupload/wave-refresh',"
        " '/fileupload/static/css/app.css'):\n"
        '    response = client.get(path)\n'
        "    print(path, response.status_code, response.headers.get('Location'))\n"
    )
    result = run_python(code, FILEUPLOAD_SECRET_KEY='x')
    assert result.stdout.splitlines() == [
        '/fileupload 302 /fileupload/wave-refresh',
        '/FileUpload/ 302 /fileupload/wave-refresh',
        '/ 308 /fileupload/',
        '/fileupload/wave-refresh 401 None',
        '/fileupload/static/css/app.css 200 None',
    ], result.stderr


def test_development_keeps_loading_off_by_default():
    result = run_python(
        "from app.config import config; print(config['development'].WAVE_REFRESH_ENABLED)"
    )
    assert result.stdout.split() == ['False']


# ---------------------------------------------------------------------------
# Power BI estimate
# ---------------------------------------------------------------------------

def server_schedule():
    names = [name for name in dir(ProductionConfig) if name.startswith('WAVE_PBI_')]
    return PowerBiSchedule.from_config({name: getattr(ProductionConfig, name) for name in names})


@pytest.mark.parametrize('finished, shows', [
    ('2026-11-13 10:50:13', '2026-11-13 11:15'),  # The load that showed 12:15 by mistake.
    ('2026-11-13 10:54:59', '2026-11-13 11:15'),
    ('2026-11-13 10:55:01', '2026-11-13 12:15'),
    ('2026-11-13 10:00:00', '2026-11-13 11:15'),  # From the user's first request for this estimate.
    ('2026-11-13 09:55:20', '2026-11-13 11:15'),  # Same request: 9:55 misses the 10:00 refresh.
    ('2026-11-12 17:30:00', '2026-11-13 09:15'),  # After the last refresh, the next morning.
])
def test_power_bi_estimate_matches_the_users_examples(finished, shows):
    estimate = server_schedule().estimate(datetime.strptime(finished, '%Y-%m-%d %H:%M:%S'))
    assert datetime.fromisoformat(estimate['visible_at']).strftime('%Y-%m-%d %H:%M') == shows
