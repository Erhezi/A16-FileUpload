/* =============================================================================
   InternalUpload.UserActivityLog  (database: SCSFileIngestor)

   One row per user action in the File Upload app. Safe to re-run.

   ActivityType  PAGE_VIEW | ACCESS_DENIED | FILE_VALIDATE | FILE_REFRESH
   Status        success | failed | denied
   Username      Windows login. No foreign key to InternalUpload.Users, so refused
                 attempts by people who are not on the list are kept too.
   FileSizeKB    Rounded up to a whole KB (1 KB = 1024 bytes), as Windows Explorer
                 shows it.
   DetailJson    {"mode", "database_updated", "realization_year"} for uploads.
   Environment   production | development. SCSFileIngestor has no test copy, so
                 local runs write here too; filter on this column for real use.
   StartedAt     Naive US Eastern, the same convention as the Savings Tracker.
   ============================================================================= */
USE SCSFileIngestor;
GO

IF SCHEMA_ID('InternalUpload') IS NULL
    EXEC('CREATE SCHEMA InternalUpload AUTHORIZATION dbo');
GO

IF OBJECT_ID('InternalUpload.UserActivityLog', 'U') IS NULL
BEGIN
    CREATE TABLE InternalUpload.UserActivityLog (
        ID              INT            NOT NULL IDENTITY(1,1)
      , ActivityType    VARCHAR(30)    NOT NULL
      , UploadType      VARCHAR(30)    NOT NULL               -- WAVE
      , Status          VARCHAR(20)    NOT NULL
      , Username        VARCHAR(100)   NOT NULL
      , FullName        VARCHAR(255)   NULL
      , RoleName        VARCHAR(50)    NULL
      , FileName        NVARCHAR(260)  NULL
      , FileSizeKB      INT            NULL
      , [RowCount]      INT            NULL                   -- rows accepted from the file
      , TargetDatabase  VARCHAR(255)   NULL                   -- e.g. PBI on <server>
      , ArchivedPath    NVARCHAR(500)  NULL
      , Message         NVARCHAR(1000) NULL
      , DetailJson      NVARCHAR(MAX)  NULL
      , IpAddress       VARCHAR(50)    NULL
      , UserAgent       VARCHAR(500)   NULL
      , Environment     VARCHAR(20)    NOT NULL
      , StartedAt       DATETIME2      NOT NULL
      , EndedAt         DATETIME2      NULL
      , DurationSeconds FLOAT          NULL

      , CONSTRAINT PK_UserActivityLog PRIMARY KEY (ID)
    );

    CREATE INDEX IX_UserActivityLog_Type_StartedAt
        ON InternalUpload.UserActivityLog (ActivityType, StartedAt DESC);
    CREATE INDEX IX_UserActivityLog_Username_StartedAt
        ON InternalUpload.UserActivityLog (Username, StartedAt DESC);
END
GO

/* 2026-11-10: brings a table created before then up to date. FileSizeBytes becomes
   FileSizeKB in the same position, converting the rows already there, and FileSha256
   is dropped. Dynamic SQL because a missing column fails the whole batch at compile
   time, even inside an IF that is false. */
IF COL_LENGTH('InternalUpload.UserActivityLog', 'FileSizeBytes') IS NOT NULL
BEGIN
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;
    EXEC('UPDATE InternalUpload.UserActivityLog SET FileSizeBytes = CEILING(FileSizeBytes / 1024.0)');
    EXEC sp_rename 'InternalUpload.UserActivityLog.FileSizeBytes', 'FileSizeKB', 'COLUMN';
    EXEC('ALTER TABLE InternalUpload.UserActivityLog ALTER COLUMN FileSizeKB INT NULL');
    COMMIT;
END
GO

IF COL_LENGTH('InternalUpload.UserActivityLog', 'FileSha256') IS NOT NULL
    EXEC('ALTER TABLE InternalUpload.UserActivityLog DROP COLUMN FileSha256');
GO

/* =============================================================================
   InternalUpload.ETLHealth  (database: SCSFileIngestor)

   One row per Upload & Refresh attempt. Same columns as MedlineReport.ETLHealth,
   and filled the same way:

   LastRunTime   When the run started. Naive US Eastern.
   TaskStatus    SUCCESS | FAIL
   Duration      Whole seconds.
   ============================================================================= */
IF OBJECT_ID('InternalUpload.ETLHealth', 'U') IS NULL
BEGIN
    CREATE TABLE InternalUpload.ETLHealth (
        ID               INT          NOT NULL IDENTITY(1,1)
      , ProcessName      VARCHAR(100) NOT NULL
      , ProcessID        VARCHAR(50)  NOT NULL
      , SourceFilePath   VARCHAR(MAX) NULL
      , LastRunTime      DATETIME     NOT NULL
      , TargetTableName  VARCHAR(100) NULL
      , TaskStatus       VARCHAR(50)  NOT NULL
      , [RowCount]       INT          NULL
      , PackagePath      VARCHAR(MAX) NOT NULL
      , LogFilePath      VARCHAR(MAX) NULL
      , STGTableName     VARCHAR(MAX) NULL
      , ProcessFrequency VARCHAR(50)  NULL
      , [Error]          VARCHAR(MAX) NULL
      , Duration         INT          NULL

      , CONSTRAINT PK_ETLHealth PRIMARY KEY (ID)
    );
END
GO

/* =============================================================================
   InternalUpload.UserRoles and InternalUpload.Users  (database: SCSFileIngestor)

   This app's own sign-in tables. A role belongs to one module, and any role in a
   module can use it, so there is no list of permissions.

   The first users are copied from the Savings Tracker. Rows already here are left
   alone on a re-run, so a later role change survives it.
   ============================================================================= */
IF OBJECT_ID('InternalUpload.UserRoles', 'U') IS NULL
BEGIN
    CREATE TABLE InternalUpload.UserRoles (
        ID        INT         NOT NULL IDENTITY(1,1)
      , Name      VARCHAR(50) NOT NULL
      , Module    VARCHAR(50) NOT NULL
      , CreatedAt DATETIME2   NOT NULL CONSTRAINT DF_UserRoles_CreatedAt DEFAULT (GETDATE())
      , UpdatedAt DATETIME2   NOT NULL CONSTRAINT DF_UserRoles_UpdatedAt DEFAULT (GETDATE())

      , CONSTRAINT PK_UserRoles PRIMARY KEY (ID)
      , CONSTRAINT UQ_UserRoles_Name_Module UNIQUE (Name, Module)
    );
END
GO

IF OBJECT_ID('InternalUpload.Users', 'U') IS NULL
BEGIN
    CREATE TABLE InternalUpload.Users (
        ID        INT          NOT NULL IDENTITY(1,1)
      , Username  VARCHAR(100) NOT NULL               -- Windows login, without the domain
      , FullName  VARCHAR(255) NOT NULL
      , Email     VARCHAR(255) NOT NULL
      , RoleID    INT          NOT NULL
      , IsActive  BIT          NOT NULL CONSTRAINT DF_Users_IsActive DEFAULT (1)
      , CreatedAt DATETIME2    NOT NULL CONSTRAINT DF_Users_CreatedAt DEFAULT (GETDATE())
      , UpdatedAt DATETIME2    NOT NULL CONSTRAINT DF_Users_UpdatedAt DEFAULT (GETDATE())

      , CONSTRAINT PK_Users PRIMARY KEY (ID)
      , CONSTRAINT UQ_Users_Username UNIQUE (Username)
      , CONSTRAINT FK_Users_UserRoles FOREIGN KEY (RoleID) REFERENCES InternalUpload.UserRoles (ID)
    );
END
GO

INSERT INTO InternalUpload.UserRoles (Name, Module)
SELECT seed.Name, 'wave_refresh'
FROM (VALUES ('Admin'), ('User')) AS seed (Name)
WHERE NOT EXISTS (
    SELECT 1 FROM InternalUpload.UserRoles r WHERE r.Name = seed.Name AND r.Module = 'wave_refresh'
);
GO

INSERT INTO InternalUpload.Users (Username, FullName, Email, RoleID)
SELECT u.username, u.full_name, u.email, r.ID
FROM SavingsTracker_v2.dbo.users u
JOIN InternalUpload.UserRoles r
  ON r.Module = 'wave_refresh'
 AND r.Name = CASE WHEN u.username IN ('dli2', 'atong') THEN 'Admin' ELSE 'User' END
WHERE u.username IN ('dli2', 'atong', 'jennortiz', 'mcondoleo', 'abhashem')
  AND NOT EXISTS (SELECT 1 FROM InternalUpload.Users x WHERE x.Username = u.username);
GO
