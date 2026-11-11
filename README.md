# File Upload

Internal Flask app that lets procurement users load the weekly Wave export into
the PBI tables behind the SCS Savings Power BI dashboard, without sending it to
the data team. It was moved out of the Savings Tracker so it can run on its own.

## How it fits together

| Concern | Where | Notes |
| --- | --- | --- |
| Sign-in | IIS Windows Authentication | Same method as the Savings Tracker |
| Users and roles | `SCSFileIngestor.InternalUpload` (`Users`, `UserRoles`) | This app's own, separate from the tracker |
| Activity log | `SCSFileIngestor.InternalUpload.UserActivityLog` | This app's own data |
| Run history | `SCSFileIngestor.InternalUpload.ETLHealth` | Same columns as `MedlineReport.ETLHealth` |
| Wave load target | `PBI` (`SCSSaving` schema) | Production. There is no test copy |
| Archived exports | `\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\WaveExport` | This app's own folder since 2026-11-11. Earlier copies are in `...\SavingsTrackerV2\Wave_Exports` |

Only active people listed in `InternalUpload.Users` get in, and nobody is added
automatically. Anyone else sees a Request Access page, and the attempt goes in the activity log.
A role belongs to one module. The `wave_refresh` module has Admin and User, and both can
do everything in it today.

There is no user management page. To add someone, run:

```sql
INSERT INTO SCSFileIngestor.InternalUpload.Users (Username, FullName, Email, RoleID)
SELECT 'jdoe', 'Jane Doe', 'jdoe@montefiore.org', ID
FROM SCSFileIngestor.InternalUpload.UserRoles
WHERE Name = 'User' AND Module = 'wave_refresh';
```

`Username` is the Windows login without the domain. Set `IsActive` to 0 to turn someone off.

## What a refresh does

The **Wave File** page (`/wave-refresh`) offers **Check File Only**, which changes
nothing, and **Upload & Refresh**:

1. Validates the workbook: `initiative` tab, required columns, L0-L5 rows (submitted
   L1-L5 stages become `SL1`-`SL5`), unique whole-number IDs, staging column lengths,
   and a creation date that is not in the future, not older than
   `FILEUPLOAD_WAVE_MAX_EXPORT_AGE_DAYS` (default 10) and not older than the data already loaded.
2. Archives the exact upload to `FILEUPLOAD_WAVE_ARCHIVE_PATH` as
   `<upload time>_created_<native creation time>_<original name>`, both in Eastern time.
3. In one transaction on `PBI`: clears and reloads `SCSSaving.Initiatives_stg`, runs
   `SCSSaving.sp_MergeInitiativesFromStage`, and reads savings by channel from
   `SCSSaving.vw_MainSavings` before and after. Any failure rolls back. The
   `WaveL5.refresh` application lock keeps it from overlapping `refresh_wave.py`.
4. Records the attempt in the activity log and in `InternalUpload.ETLHealth`.
5. Estimates when Power BI will show the load, from the `FILEUPLOAD_WAVE_PBI_*` schedule settings.

### Which date counts

Every date check uses the file's native creation time, read from inside the file. The
date in the filename is only a label and is never read, so renaming a file changes nothing.

| Export | Where the creation time comes from |
| --- | --- |
| Saved by Excel (`Weekly Full Export_*`) | The `dcterms:created` property |
| Wave's own download (`Full export ... - Wave - *`) | Its zip entry times, which Wave stamps in UTC. It has no `created` property |

A file with neither is refused. The date, in Eastern time, is also written as `loaded stamp`,
which the dashboard views report as `Last Update Date`.

`app/utils/wave_refresh.py` started as a copy of the tracker's module. Its business rules
must stay in step with the WaveL5 `fileprocess.ipynb`, where `native_created_at` is the
source of the date rule. The tracker carries an identical copy of this module. Keep the two
identical until one of the apps is retired.

## Activity log

Each row in `InternalUpload.UserActivityLog` is one user action:

| ActivityType | Written when |
| --- | --- |
| `PAGE_VIEW` | The upload page is opened |
| `ACCESS_DENIED` | Someone without access reaches a page or API, or an upload fails the session token check |
| `FILE_VALIDATE` | A file goes through Check File Only, whether it passes or not |
| `FILE_REFRESH` | A load is attempted. `Status` is `success` only if PBI was updated |

For uploads, `DetailJson` holds only `mode`, `database_updated` and `realization_year`.
`FileSizeKB` is rounded up to a whole KB, as Windows Explorer shows it. A failed log write never changes what
the user sees, so a load that reached PBI still reports its real result; the failure goes
to `app.log` instead (see [Logs](#logs)). That also means **a missing log table does not stop the app**,
so create the table before deploying.

`SCSFileIngestor` has no test copy, so local runs write to the same table. Use
`Environment` to separate them:

```sql
SELECT TOP 50 StartedAt, Username, ActivityType, Status, FileName, [RowCount], Message
FROM SCSFileIngestor.InternalUpload.UserActivityLog
WHERE Environment = 'production'
ORDER BY StartedAt DESC;
```

## ETL health

Each **Upload & Refresh** attempt adds one row to `InternalUpload.ETLHealth`, which has the
same columns as `MedlineReport.ETLHealth`. **Check File Only** adds none.

| Column | Value |
| --- | --- |
| `ProcessName`, `ProcessID` | `Wave Refresh`, `WAVEREFRESH_W_DL` |
| `SourceFilePath` | The archived copy. Empty if the attempt stopped before archiving |
| `LastRunTime` | When the attempt started, in Eastern time |
| `TargetTableName`, `STGTableName` | `[PBI].[SCSSaving].[Initiatives]`, `[PBI].[SCSSaving].[Initiatives_stg]` |
| `TaskStatus` | `SUCCESS`, or `FAIL` for anything else, including a file the checks refused |
| `RowCount` | Rows loaded. 0 on `FAIL`, because a failed load rolls back |
| `PackagePath`, `LogFilePath` | `run.py` in the app folder, and `app.log` (see [Logs](#logs)) |
| `ProcessFrequency` | `Weekly` |
| `Error` | What the user was told. Empty on `SUCCESS` |
| `Duration` | Whole seconds |

As with the activity log, a failed write here never changes what the user sees. It goes to
`app.log` instead.

## Run locally

```powershell
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # FILEUPLOAD_ENVIRONMENT=development
.venv\Scripts\python run.py
```

Open <http://127.0.0.1:5016/fileupload/>. You are signed in as your Windows login, which must
be listed in `InternalUpload.Users` because local runs use the same table as the server. The
server only listens on this machine.

**Upload & Refresh is off in development** because `PBI` is production. Check File Only
works and is read-only. To load from a local run on purpose, set
`FILEUPLOAD_WAVE_REFRESH_ENABLED=true` in `.env`.

## Tests

```powershell
.venv\Scripts\python -m pytest tests
```

The tests use in-memory SQLite and never touch SQL Server.

## Deploy to IIS

The app gets its own IIS site on port 8070 of `ynbbstvwp03`, separate from PLM on 5080:
<http://ynbbstvwp03:8070/fileupload/>. The `/fileupload` prefix lives in the app's Python code,
the way `/plm` does in PLM, so the site's root is the app folder and `/` redirects to the prefix.

The app can sit in any folder. `D:\FileUpload\A16-FileUpload` is only the expected location.
`web.config` follows the folder IIS points at, except `scriptProcessor`, which IIS needs as a
fixed path. Step 3 fills that in.

The app's tables already exist in `SCSFileIngestor`. On a new database, run
`migration/create_internal_upload_schema.sql` first. It is safe to re-run.

1. Copy the app to the server. Do not copy `.venv`, `.env` or `logs`.
2. In that folder, create the virtual environment:
   `python -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`.
   Use a Python installed for all users, because the app pool account has to run it.
3. In that folder, run `.venv\Scripts\python set_server_path.py`. It writes the real path into
   `web.config`, creates `logs`, and prints the path.
4. As an administrator, run `.venv\Scripts\wfastcgi-enable`. It must show the same paths as
   step 3, or IIS answers 500 with "The <handler> scriptProcessor could not be found in
   <fastCGI> application configuration".
5. Create `.env` containing only `FILEUPLOAD_SECRET_KEY`. The app refuses to start without it.
   Use a new key, not PLM's. `web.config` sets `FILEUPLOAD_ENVIRONMENT` and `FILEUPLOAD_URL_PREFIX`.
6. Create an app pool for this app with **No Managed Code**. Run it as a domain account in
   `DM_MONTYNT\APP_MMC_ProcurementServer_Users`. That group is `db_owner` on
   `SCSFileIngestor` and `PBI`, so no new grants are needed.
7. Give the pool account Read on the app folder and Modify on `logs`. Without Modify, every
   request answers 500. It also needs write access to the `WaveExport` and `app_logs` folders
   on the shared drive, which `DM_MONTYNT\MonteFs_Data_Procurement PMO_Modify` has.
8. Create an IIS site with the app folder as its physical path, that pool, and an http binding
   on port 8070. Allow port 8070 through the server's firewall. If IIS answers 500.19 saying the
   `handlers` section is locked, run
   `%windir%\system32\inetsrv\appcmd unlock config -section:system.webServer/handlers`.
9. On the site, enable Windows Authentication and disable Anonymous Authentication. The Windows
   Authentication role service must be installed on the server. PLM does not use it, so it may
   not be.
10. Open <http://ynbbstvwp03:8070/fileupload/>, run **Check File Only** on a current export, and
    read the new `FILE_VALIDATE` row in the activity log. A 200 proves nothing here: a failed log
    write never changes the response. `app.log` in `app_logs` should show a `Started` line
    with `environment=production`.

To update the app later, copy the code without `web.config`, `.env`, `.venv` and `logs`, then
recycle this app's pool. If `web.config` does get replaced, run step 3 again.

A refresh takes about 6 seconds today, within the IIS FastCGI default `activityTimeout` of
30 seconds. Raise that timeout if the merge procedure grows much slower.

### Logs

| File | Holds |
| --- | --- |
| `app.log` | Errors and warnings the app handles itself, including failed activity log writes, plus a `Started` line for each process. On the server it is in `\\montefiore.org\centralfiles\data\Procurement PMO\dli2\FileUpload\app_logs`. Local runs use `logs\app.log` in the app folder |
| `logs\wfastcgi.log` | Failures before the app is running, such as a missing `FILEUPLOAD_SECRET_KEY`. Always in the app folder on the server |

Neither file rotates. If the share is unreachable when an IIS process starts, that process
runs without `app.log` until the pool recycles. Uploads are not affected. Set `FILEUPLOAD_LOG_FILE` in
`.env` to log somewhere else.

## Settings

Every setting is an environment variable starting with `FILEUPLOAD_`, such as
`FILEUPLOAD_DB_SERVER`. The prefix means a variable another app sets on the same server, such
as `DB_SERVER` or `SECRET_KEY`, never changes this app. Highest first, a value comes from:

1. `<appSettings>` in `web.config` on the server
2. Windows environment variables
3. `.env` in the app folder, which never replaces a value already set
4. The default in `app/config.py`

Settings are read when a process starts. A `web.config` change restarts the app by itself. A
`.env` change needs an app pool recycle, or a restart of `run.py` locally. See `.env.example`
for what is usually set and `app/config.py` for everything else.
