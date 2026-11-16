# Hospital Management System

A small Flask and SQLite app for patient registration, department setup, doctor sign-in, queue management, and consultation records.

## Run locally

1. Create and activate a Python virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Start the app with `python app.py`.
4. Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

The app uses `HospitalDB/hospital.db` when that imported file exists, then falls back to `C:\HospitalDB\hospital.db`, then to `instance/hospital_data.sqlite3`. `HOSPITAL_DB_PATH` overrides the local SQLite path. Database files and backups are excluded from Git.

Before changing an existing SQLite database, the app takes a consistent timestamped backup and then applies its versioned schema migration. The migration adds normalized department and appointment links, backfills links case-insensitively, hashes legacy doctor passwords, adds query indexes and safe uniqueness rules, and creates a transaction-safe shared queue-token allocator. Patient registration and appointment check-in allocate tokens inside the database transaction, so concurrent staff do not race for a token. Queue state changes also use conditional updates to prevent two doctors from acting on the same patient simultaneously. The original text fields and historical doctor IDs remain available. The manager's **Data Health** tab reports aggregate anomalies; it does not guess corrections to imported records. Before upgrading a PostgreSQL deployment, take a provider-level database snapshot.

For multiple app instances or staff on different computers, configure a shared PostgreSQL database with `DATABASE_URL`, for example `postgresql+psycopg://user:password@db-host:5432/hospital`. Keep that value in the deployment environment, never in source control. To copy an existing SQLite database into an empty PostgreSQL destination, set `DATABASE_URL`, start the app once so it creates and migrates the destination schema, then stop it. Run `python scripts/import_sqlite_database.py` for a read-only summary, then add `--apply` to perform the import. The importer refuses duplicate tokens, duplicate department names, or a non-empty destination, preserves record IDs and appointment links, and hashes any remaining plain-text doctor passwords. The Psycopg binary package is included in `requirements.txt` for PostgreSQL connections. In production mode, the app uses Waitress and reads `APP_HOST` and `PORT`; point staff browsers at the server's reachable address and use HTTPS through your deployment's proxy.

## Demo sign-ins

- Manager password: `admin123`
- Receptionist password: `reception123`
- Doctor sign-in: create a doctor account from the manager page.

For local use, the manager and receptionist demo passwords can be changed with `MANAGER_PASSWORD` and `RECEPTIONIST_PASSWORD`. Before deployment, set `APP_ENV=production`, `FLASK_SECRET_KEY`, `MANAGER_PASSWORD`, `RECEPTIONIST_PASSWORD`, and `DATABASE_URL` to deployment-specific values. For HTTPS deployments, also set `SESSION_COOKIE_SECURE=true`.

Doctor passwords are stored as hashes. Legacy plain-text credentials in the imported SQLite database are hashed during its first schema migration. Manager activity logs record sign-ins and operational changes without storing patient names or clinical notes. The public queue view exposes only token, department, and queue status; staff views require a role sign-in.

Manager and receptionist sign-ins use shared role passwords in this version, so their activity is attributed to the role rather than to an individual staff account. Doctor actions are attributed to the doctor ID.

## Current roles

- **Manager:** configure departments, manage doctor accounts, review consultation records, oversee appointments, and review the activity log.
- **Receptionist:** register patients, schedule or cancel appointments, check patients into the queue, and track visits.
- **Doctor:** review scheduled appointments, call patients in their department, and save consultation notes.
- **Public queue:** view token, department, and waiting/called status without patient identifiers.
