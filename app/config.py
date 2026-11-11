"""
Configuration for each environment.

Two SQL Server databases are involved:
  * the app's own - SCSFileIngestor, schema InternalUpload: users, roles, activity log, ETL health
  * Wave target   - PBI, reached with raw pyodbc in app/utils/wave_refresh.py

Every setting is read from an environment variable named FILEUPLOAD_<setting>, for example
FILEUPLOAD_DB_SERVER for DB_SERVER. The prefix keeps a variable that another app sets on the
same server, such as DB_SERVER or SECRET_KEY, from changing this one. Highest first, a value
comes from web.config <appSettings> (wfastcgi copies them into the environment), Windows
environment variables, .env (which never replaces a variable already set), then the default here.
"""
import os

from sqlalchemy.engine import URL

PREFIX = 'FILEUPLOAD_'
DEFAULT_SERVER = r'YNBBSTVWP02\PROCDATASRVPROD'


def setting(name, default):
    """The FILEUPLOAD_<name> environment variable, or the default when it is not set."""
    return os.getenv(PREFIX + name, default)


def _enabled(name, default):
    return setting(name, default).strip().lower() == 'true'


def build_database_url(server, database, driver, trusted_connection, user, password):
    """SQL Server URL; URL.create handles the escaping of the instance backslash and password."""
    if trusted_connection.lower() == 'yes':
        credentials = 'Trusted_Connection=yes;'
    else:
        credentials = f'UID={user};PWD={password};'
    connection_string = f'Driver={{{driver}}};Server={server};Database={database};{credentials}'
    return URL.create('mssql+pyodbc', query={'odbc_connect': connection_string})


class Config:
    """Production settings; the other environments override these."""

    SECRET_KEY = setting('SECRET_KEY', '')
    # Own cookie name: on the same host the tracker's session cookie must not collide with this one.
    SESSION_COOKIE_NAME = 'file_upload_session'
    SESSION_COOKIE_SAMESITE = 'Lax'

    # On the shared drive, so it can be read without signing in to the server. If the share is
    # down when a process starts, that process runs without a file log. Empty turns it off.
    LOG_FILE = setting(
        'LOG_FILE', r'\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\app_logs\app.log')

    MAX_UPLOAD_MB = int(setting('MAX_UPLOAD_MB', '50'))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024

    # Shared connection settings
    DB_SERVER = setting('DB_SERVER', DEFAULT_SERVER)
    DB_DRIVER = setting('DB_DRIVER', 'ODBC Driver 17 for SQL Server')
    DB_TRUSTED_CONNECTION = setting('DB_TRUSTED_CONNECTION', 'yes')
    DB_USER = setting('DB_USER', '')
    DB_PASSWORD = setting('DB_PASSWORD', '')
    _connection = (DB_DRIVER, DB_TRUSTED_CONNECTION, DB_USER, DB_PASSWORD)

    # The app's own tables
    LOG_DB_SERVER = setting('LOG_DB_SERVER', DB_SERVER)
    LOG_DB_NAME = setting('LOG_DB_NAME', 'SCSFileIngestor')
    SQLALCHEMY_DATABASE_URI = build_database_url(LOG_DB_SERVER, LOG_DB_NAME, *_connection)

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 1800}

    # Wave File refresh target: PBI database behind the SCS Savings Power BI dashboard.
    WAVE_DB_SERVER = setting('WAVE_DB_SERVER', DB_SERVER)
    WAVE_DB_NAME = setting('WAVE_DB_NAME', 'PBI')
    WAVE_DB_DRIVER = setting('WAVE_DB_DRIVER', DB_DRIVER)
    WAVE_DB_TRUSTED_CONNECTION = setting('WAVE_DB_TRUSTED_CONNECTION', DB_TRUSTED_CONNECTION)
    WAVE_DB_USER = setting('WAVE_DB_USER', DB_USER)
    WAVE_DB_PASSWORD = setting('WAVE_DB_PASSWORD', DB_PASSWORD)
    WAVE_DB_TIMEOUT = int(setting('WAVE_DB_TIMEOUT', '8'))
    WAVE_MAX_EXPORT_AGE_DAYS = int(setting('WAVE_MAX_EXPORT_AGE_DAYS', '10'))
    WAVE_REFRESH_ENABLED = _enabled('WAVE_REFRESH_ENABLED', 'true')
    # This app's own folder. Exports archived before 2026-11-11, by this app or the Savings
    # Tracker page, are in ...\Procurement PMO\SavingsTrackerV2\Wave_Exports.
    WAVE_ARCHIVE_PATH = setting(
        'WAVE_ARCHIVE_PATH',
        r'\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\WaveExport',
    )
    WAVE_PBI_REPORT_URL = setting(
        'WAVE_PBI_REPORT_URL',
        'https://app.powerbi.com/Redirect?action=OpenReport&appId=3b498838-9c61-46f0-9809-45b8787d4d66'
        '&reportObjectId=fb26ed47-bcdd-4154-a7c5-c0c146ac0206&ctid=9c01f0fd-65e0-40c0-89a8-2dfd51e62025'
        '&reportPage=9f7a0e64d04fa4d23cda&pbi_source=appShareLink',
    )
    # Power BI dataset refresh schedule (Eastern), used to estimate when a Wave load shows on the report.
    WAVE_PBI_FIRST_REFRESH = setting('WAVE_PBI_FIRST_REFRESH', '09:00')
    WAVE_PBI_LAST_REFRESH = setting('WAVE_PBI_LAST_REFRESH', '17:00')
    WAVE_PBI_REFRESH_INTERVAL_MINUTES = int(setting('WAVE_PBI_REFRESH_INTERVAL_MINUTES', '60'))
    WAVE_PBI_PUBLISH_MINUTES = int(setting('WAVE_PBI_PUBLISH_MINUTES', '15'))
    WAVE_PBI_LEAD_MINUTES = int(setting('WAVE_PBI_LEAD_MINUTES', '15'))


class ProductionConfig(Config):
    DEBUG = False


class DevelopmentConfig(Config):
    """
    Local runs. WAVE_DB_* still points at the production PBI database because
    there is no test copy, so loading stays off unless it is switched on.
    """
    DEBUG = True
    SECRET_KEY = Config.SECRET_KEY or 'local-development-only'
    # Kept in the app folder, so local runs never write to the production log.
    LOG_FILE = setting('LOG_FILE', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'app.log'))
    # Uses the OS login name so the app runs without IIS locally.
    DEV_BYPASS_AUTH = _enabled('DEV_BYPASS_AUTH', 'true')
    WAVE_REFRESH_ENABLED = _enabled('WAVE_REFRESH_ENABLED', 'false')


class TestingConfig(Config):
    """Automated tests: in-memory SQLite, with the InternalUpload schema mapped away."""
    TESTING = True
    SECRET_KEY = 'testing-only'
    LOG_FILE = ''
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    SQLALCHEMY_ENGINE_OPTIONS = {'execution_options': {'schema_translate_map': {'InternalUpload': None}}}
    WAVE_DB_SERVER = 'test-server'
    WAVE_ARCHIVE_PATH = 'unused-in-tests'


config = {
    'production': ProductionConfig,
    'development': DevelopmentConfig,
    'testing': TestingConfig,
}
