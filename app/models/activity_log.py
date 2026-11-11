"""
User activity log, kept in SCSFileIngestor under the InternalUpload schema.

Created by migration/create_internal_upload_schema.sql. Column names follow the
PascalCase used by the other SCSFileIngestor tables.
"""
from app import db


class UserActivityLog(db.Model):
    __tablename__ = 'UserActivityLog'
    __table_args__ = {'schema': 'InternalUpload'}

    id = db.Column('ID', db.Integer, primary_key=True)
    activity_type = db.Column('ActivityType', db.String(30), nullable=False)
    upload_type = db.Column('UploadType', db.String(30), nullable=False)
    status = db.Column('Status', db.String(20), nullable=False)
    username = db.Column('Username', db.String(100), nullable=False)
    full_name = db.Column('FullName', db.String(255))
    role_name = db.Column('RoleName', db.String(50))
    file_name = db.Column('FileName', db.String(260))
    file_size_kb = db.Column('FileSizeKB', db.Integer)
    row_count = db.Column('RowCount', db.Integer)
    target_database = db.Column('TargetDatabase', db.String(255))
    archived_path = db.Column('ArchivedPath', db.String(500))
    message = db.Column('Message', db.String(1000))
    detail_json = db.Column('DetailJson', db.Text)
    ip_address = db.Column('IpAddress', db.String(50))
    user_agent = db.Column('UserAgent', db.String(500))
    environment = db.Column('Environment', db.String(20), nullable=False)
    started_at = db.Column('StartedAt', db.DateTime, nullable=False)
    ended_at = db.Column('EndedAt', db.DateTime)
    duration_seconds = db.Column('DurationSeconds', db.Float)

    def __repr__(self):
        return f'<UserActivityLog {self.activity_type} [{self.status}] by {self.username}>'
