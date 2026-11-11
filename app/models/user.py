"""
This app's own users and roles, kept in SCSFileIngestor under the InternalUpload schema.

Created by migration/create_internal_upload_schema.sql. A role belongs to one module, and
any role in a module can use it, so there is no finer list of permissions.
"""
from app import db
from app.utils.timezone import now_eastern

WAVE_REFRESH = 'wave_refresh'


class UserRole(db.Model):
    __tablename__ = 'UserRoles'
    __table_args__ = (
        db.UniqueConstraint('Name', 'Module', name='UQ_UserRoles_Name_Module'),
        {'schema': 'InternalUpload'},
    )

    id = db.Column('ID', db.Integer, primary_key=True)
    name = db.Column('Name', db.String(50), nullable=False)
    module = db.Column('Module', db.String(50), nullable=False)
    created_at = db.Column('CreatedAt', db.DateTime, default=now_eastern, nullable=False)
    updated_at = db.Column('UpdatedAt', db.DateTime, default=now_eastern, onupdate=now_eastern, nullable=False)

    def __repr__(self):
        return f'<UserRole {self.name} on {self.module}>'


class User(db.Model):
    __tablename__ = 'Users'
    __table_args__ = {'schema': 'InternalUpload'}

    id = db.Column('ID', db.Integer, primary_key=True)
    username = db.Column('Username', db.String(100), unique=True, nullable=False)
    full_name = db.Column('FullName', db.String(255), nullable=False)
    email = db.Column('Email', db.String(255), nullable=False)
    role_id = db.Column('RoleID', db.Integer, db.ForeignKey('InternalUpload.UserRoles.ID'), nullable=False)
    is_active = db.Column('IsActive', db.Boolean, default=True, nullable=False)
    created_at = db.Column('CreatedAt', db.DateTime, default=now_eastern, nullable=False)
    updated_at = db.Column('UpdatedAt', db.DateTime, default=now_eastern, onupdate=now_eastern, nullable=False)

    role = db.relationship('UserRole')

    def __repr__(self):
        return f'<User {self.username}>'

    def can_use_module(self, module):
        return bool(self.is_active and self.role and self.role.module == module)
