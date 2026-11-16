"""Copy a legacy SQLite hospital database into an initialized PostgreSQL database.

Dry-run by default. The destination must be empty and initialized by the app.
No record contents are printed; only aggregate counts and migration status appear.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys

from sqlalchemy import MetaData, Table, create_engine, inspect, text
from werkzeug.security import generate_password_hash


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "HospitalDB" / "hospital.db"
BUSINESS_TABLES = ("department", "doctor", "patient", "appointment")
BATCH_SIZE = 500


def normalize_postgres_url(url):
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def source_rows(connection, table_name, batch_size=BATCH_SIZE):
    cursor = connection.execute(f'SELECT * FROM "{table_name}"')
    columns = [column[0] for column in cursor.description]
    while rows := cursor.fetchmany(batch_size):
        for values in rows:
            yield dict(zip(columns, values))


def source_counts(connection, table_names):
    return {
        name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        for name in table_names
    }


def inspect_source(connection):
    table_names = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    required = {"department", "doctor", "patient"}
    missing = required - table_names
    if missing:
        raise ValueError("Source database is missing required hospital tables")

    available = [name for name in BUSINESS_TABLES if name in table_names]
    counts = source_counts(connection, available)
    duplicate_tokens = connection.execute("""
        SELECT COUNT(*) FROM (
            SELECT token_number FROM patient GROUP BY token_number HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    department_rows = list(source_rows(connection, "department"))
    duplicate_department_names = sum(
        1 for total in Counter(str(row.get("name", "")).strip().casefold() for row in department_rows).values()
        if total > 1
    )
    department_names = {
        str(row.get("name", "")).strip().casefold() for row in department_rows
    }
    unlinked_doctors = sum(
        1 for row in source_rows(connection, "doctor")
        if str(row.get("department", "")).strip().casefold() not in department_names
    )
    unlinked_patients = sum(
        1 for row in source_rows(connection, "patient")
        if str(row.get("department", "")).strip().casefold() not in department_names
    )
    return available, counts, duplicate_tokens, duplicate_department_names, unlinked_doctors, unlinked_patients


def mapped_record(row, target_table, *, department_ids=None, patient_ids=None):
    target_columns = set(target_table.c.keys())
    record = {key: value for key, value in row.items() if key in target_columns}

    if "department_id" in target_columns:
        department_name = str(row.get("department", "")).strip().casefold()
        record["department_id"] = (department_ids or {}).get(department_name)

    if target_table.name == "doctor":
        password = str(row.get("password") or "")
        if not password.startswith(("scrypt:", "pbkdf2:")):
            record["password"] = generate_password_hash(password)

    if target_table.name == "appointment" and "scheduled_for" in record:
        scheduled_for = record["scheduled_for"]
        if isinstance(scheduled_for, str):
            scheduled_for = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
        record["scheduled_for"] = scheduled_for.astimezone(timezone.utc)

    if target_table.name == "appointment" and "patient_id" in target_columns:
        token = row.get("patient_token")
        existing_id = row.get("patient_id")
        record["patient_id"] = (patient_ids or {}).get(token, existing_id)

    return record


def insert_source_table(source, destination, source_name, target_table, **mapping_options):
    batch = []
    total = 0
    for source_record in source_rows(source, source_name):
        batch.append(mapped_record(source_record, target_table, **mapping_options))
        if len(batch) >= BATCH_SIZE:
            destination.execute(target_table.insert(), batch)
            total += len(batch)
            batch.clear()
    if batch:
        destination.execute(target_table.insert(), batch)
        total += len(batch)
    return total


def set_postgres_sequences(connection):
    for table_name in ("department", "patient", "appointment"):
        if table_name not in inspect(connection).get_table_names():
            continue
        connection.execute(text(f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                EXISTS (SELECT 1 FROM {table_name})
            )
        """))


def print_summary(counts, duplicate_tokens, duplicate_department_names, unlinked_doctors, unlinked_patients):
    print("SQLite records available:")
    for table_name, count in counts.items():
        print(f"  {table_name}: {count}")
    print(f"  duplicate patient-token groups: {duplicate_tokens}")
    print(f"  duplicate department-name groups: {duplicate_department_names}")
    print(f"  doctors without a department match: {unlinked_doctors}")
    print(f"  patients without a department match: {unlinked_patients}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Legacy SQLite database path")
    parser.add_argument("--apply", action="store_true", help="Copy records (default is a read-only dry run)")
    args = parser.parse_args()

    source_path = args.source.expanduser().resolve()
    if not source_path.is_file():
        parser.error(f"SQLite source file does not exist: {source_path}")
    destination_url = os.environ.get("DATABASE_URL")
    if not destination_url:
        parser.error("Set DATABASE_URL to the initialized PostgreSQL destination")
    destination_url = normalize_postgres_url(destination_url)

    source_uri = source_path.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            source.row_factory = sqlite3.Row
            source.execute("BEGIN")
            available, counts, duplicate_tokens, duplicate_department_names, unlinked_doctors, unlinked_patients = inspect_source(source)
            print_summary(counts, duplicate_tokens, duplicate_department_names, unlinked_doctors, unlinked_patients)

            if not args.apply:
                print("Dry run only. No destination rows were changed.")
                return 0
            if duplicate_tokens or duplicate_department_names:
                print("Import stopped: resolve duplicate patient tokens and department names first.", file=sys.stderr)
                return 2

            engine = create_engine(destination_url, pool_pre_ping=True)
            try:
                if engine.dialect.name != "postgresql":
                    raise ValueError("DATABASE_URL must point to PostgreSQL")
                with engine.begin() as destination:
                    target_names = set(inspect(destination).get_table_names())
                    required_target = set(BUSINESS_TABLES) | {"token_sequence"}
                    if not required_target.issubset(target_names):
                        raise ValueError("Destination schema is not initialized; start the app once with DATABASE_URL")
                    target_metadata = MetaData()
                    target_tables = {
                        name: Table(name, target_metadata, autoload_with=destination)
                        for name in required_target
                    }
                    existing = {
                        name: destination.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
                        for name in BUSINESS_TABLES
                    }
                    if any(existing.values()):
                        raise ValueError("Destination business tables must be empty before import")
                    sequence = destination.execute(text(
                        "SELECT COUNT(*) FROM token_sequence WHERE name='patient'"
                    )).scalar_one()
                    if sequence != 1:
                        raise ValueError("Destination patient token sequence is not initialized")

                    department_ids = {
                        str(row["name"]).strip().casefold(): row["id"]
                        for row in source_rows(source, "department")
                    }
                    copied_departments = insert_source_table(
                        source, destination, "department", target_tables["department"]
                    )
                    copied_doctors = insert_source_table(
                        source, destination, "doctor", target_tables["doctor"], department_ids=department_ids
                    )
                    copied_patients = insert_source_table(
                        source, destination, "patient", target_tables["patient"], department_ids=department_ids
                    )
                    patient_ids = {
                        row["token_number"]: row["id"]
                        for row in destination.execute(text("SELECT id, token_number FROM patient"))
                    }
                    copied_appointments = 0
                    if "appointment" in available:
                        copied_appointments = insert_source_table(
                            source,
                            destination,
                            "appointment",
                            target_tables["appointment"],
                            department_ids=department_ids,
                            patient_ids=patient_ids,
                        )

                    next_token = destination.execute(text(
                        "SELECT COALESCE(MAX(token_number), 0) + 1 FROM patient"
                    )).scalar_one()
                    destination.execute(text(
                        "UPDATE token_sequence SET next_value=:next_value WHERE name='patient'"
                    ), {"next_value": next_token})
                    set_postgres_sequences(destination)

                print("Import complete:")
                print(f"  departments: {copied_departments}")
                print(f"  doctors: {copied_doctors}")
                print(f"  patients: {copied_patients}")
                print(f"  appointments: {copied_appointments}")
            finally:
                engine.dispose()
    except (sqlite3.Error, ValueError) as exc:
        print(f"Import stopped: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"Import failed and the destination transaction was rolled back ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
