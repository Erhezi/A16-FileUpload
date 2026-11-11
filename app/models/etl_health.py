"""
Run history of the Wave load, kept in SCSFileIngestor under the InternalUpload schema.

Same columns as MedlineReport.ETLHealth. Created by migration/create_internal_upload_schema.sql.
"""
from app import db


class ETLHealth(db.Model):
    __tablename__ = 'ETLHealth'
    __table_args__ = {'schema': 'InternalUpload'}

    id = db.Column('ID', db.Integer, primary_key=True)
    process_name = db.Column('ProcessName', db.String(100), nullable=False)
    process_id = db.Column('ProcessID', db.String(50), nullable=False)
    source_file_path = db.Column('SourceFilePath', db.Text)
    last_run_time = db.Column('LastRunTime', db.DateTime, nullable=False)
    target_table_name = db.Column('TargetTableName', db.String(100))
    task_status = db.Column('TaskStatus', db.String(50), nullable=False)
    row_count = db.Column('RowCount', db.Integer)
    package_path = db.Column('PackagePath', db.Text, nullable=False)
    log_file_path = db.Column('LogFilePath', db.Text)
    stg_table_name = db.Column('STGTableName', db.Text)
    process_frequency = db.Column('ProcessFrequency', db.String(50))
    error = db.Column('Error', db.Text)
    duration = db.Column('Duration', db.Integer)

    def __repr__(self):
        return f'<ETLHealth {self.process_name} [{self.task_status}] at {self.last_run_time}>'
