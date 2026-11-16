from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import math
import os
from pathlib import Path
import secrets
import sqlite3

from flask import Flask, jsonify, render_template, request, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Engine, and_, event, func, inspect, or_, text, update
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__, template_folder="templates", instance_relative_config=True)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)

project_database = Path(app.root_path) / "HospitalDB" / "hospital.db"
legacy_database = Path(r"C:\HospitalDB\hospital.db")
default_database = (
    project_database if project_database.exists()
    else legacy_database if legacy_database.exists()
    else Path(app.instance_path) / "hospital_data.sqlite3"
)
database_path = Path(os.environ.get("HOSPITAL_DB_PATH", default_database)).expanduser()
database_path.parent.mkdir(parents=True, exist_ok=True)
database_url = os.environ.get("DATABASE_URL")
if database_url:
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
else:
    database_url = f"sqlite:///{database_path.as_posix()}"

app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MANAGER_PASSWORD=os.environ.get("MANAGER_PASSWORD", "admin123"),
    RECEPTIONIST_PASSWORD=os.environ.get("RECEPTIONIST_PASSWORD", "reception123"),
)

if os.environ.get("APP_ENV", "development").lower() == "production":
    missing_settings = [
        name for name in ("FLASK_SECRET_KEY", "MANAGER_PASSWORD", "RECEPTIONIST_PASSWORD", "DATABASE_URL")
        if not os.environ.get(name)
    ]
    if missing_settings:
        raise RuntimeError("Set production secrets before starting the app: " + ", ".join(missing_settings))

db = SQLAlchemy(app)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(connection, _):
    if connection.__class__.__module__.startswith("sqlite3"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    fee = db.Column(db.Float, nullable=False)
    __table_args__ = (db.Index("uq_department_name_casefold", func.lower(name), unique=True),)


class Doctor(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    # Existing SQLite databases can keep this column; new hashes are longer than
    # the old plain-text passwords, so the declared limit is increased for other DBs.
    password = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), index=True)
    department_record = db.relationship("Department", foreign_keys=[department_id], lazy="joined")
    address = db.Column(db.String(200))
    contact = db.Column(db.String(50))


class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    contact_number = db.Column(db.String(50))
    address = db.Column(db.String(200))
    department = db.Column(db.String(100))
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), index=True)
    department_record = db.relationship("Department", foreign_keys=[department_id], lazy="joined")
    illness = db.Column(db.String(200))
    fee = db.Column(db.Float)
    status = db.Column(db.String(50), default="waiting", nullable=False)
    findings = db.Column(db.String(300))
    diagnosis = db.Column(db.String(300))
    prescriptions = db.Column(db.Text)
    doctor_name = db.Column(db.String(100))
    doctor_id = db.Column(db.String(50))
    __table_args__ = (
        db.Index("uq_patient_token_number", "token_number", unique=True),
        db.Index("ix_patient_status_token", "status", "token_number"),
        db.Index("ix_patient_department_status_token", "department_id", "status", "token_number"),
        db.Index("ix_patient_doctor_id", "doctor_id"),
    )


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    contact_number = db.Column(db.String(50), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), index=True)
    department_record = db.relationship("Department", foreign_keys=[department_id], lazy="joined")
    reason = db.Column(db.String(200))
    scheduled_for = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="scheduled")
    patient_token = db.Column(db.Integer)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), index=True)
    patient_record = db.relationship("Patient", foreign_keys=[patient_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_appointment_department_status_time", "department_id", "status", "scheduled_for"),
    )


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_role = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.String(50), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    entity_type = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)


class SchemaMigration(db.Model):
    version = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    applied_at = db.Column(db.DateTime(timezone=True), nullable=False)


class TokenSequence(db.Model):
    """Transaction-safe allocator for globally unique, human-facing queue tokens."""
    name = db.Column(db.String(40), primary_key=True)
    next_value = db.Column(db.Integer, nullable=False)


DATABASE_SCHEMA_VERSION = 2


def backup_before_schema_change():
    """Take a consistent SQLite snapshot before the first automatic migration."""
    if os.environ.get("DATABASE_URL") or not database_path.is_file():
        return
    source_uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "schema_migration" in tables:
                latest = source.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] or 0
                if latest >= DATABASE_SCHEMA_VERSION:
                    return
    except sqlite3.Error as exc:
        raise RuntimeError("Could not inspect the hospital database before migration") from exc

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = database_path.with_name(f"{database_path.name}.before-v{DATABASE_SCHEMA_VERSION}-{stamp}.bak")
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not create a database backup at {backup_path}") from exc
    app.logger.info("Created pre-migration database backup at %s", backup_path)


def _ensure_database_column(connection, table_name, column_name, declaration):
    columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
    if column_name in columns:
        return
    connection.exec_driver_sql(
        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {declaration}'
    )


def migrate_database():
    """Apply repeatable, additive migrations to imported and fresh databases."""
    with db.engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "schema_migration" not in tables:
            return
        dialect = connection.dialect.name
        if dialect == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(720241031)")
        latest = connection.execute(text("SELECT MAX(version) FROM schema_migration")).scalar() or 0
        if latest >= DATABASE_SCHEMA_VERSION:
            return

        if latest < 1:
            department_reference = "INTEGER REFERENCES department(id)"
            patient_reference = "INTEGER REFERENCES patient(id)"
            for table_name in ("doctor", "patient", "appointment"):
                _ensure_database_column(connection, table_name, "department_id", department_reference)
            _ensure_database_column(connection, "appointment", "patient_id", patient_reference)

            if dialect == "postgresql":
                connection.exec_driver_sql("ALTER TABLE doctor ALTER COLUMN password TYPE VARCHAR(255)")

            connection.execute(text("""
                UPDATE doctor
                SET department_id = (
                    SELECT department.id FROM department
                    WHERE lower(department.name) = lower(doctor.department)
                    ORDER BY department.id LIMIT 1
                )
                WHERE department_id IS NULL
            """))
            connection.execute(text("""
                UPDATE patient
                SET department_id = (
                    SELECT department.id FROM department
                    WHERE lower(department.name) = lower(patient.department)
                    ORDER BY department.id LIMIT 1
                )
                WHERE department_id IS NULL
            """))
            connection.execute(text("""
                UPDATE appointment
                SET department_id = (
                    SELECT department.id FROM department
                    WHERE lower(department.name) = lower(appointment.department)
                    ORDER BY department.id LIMIT 1
                )
                WHERE department_id IS NULL
            """))
            connection.execute(text("""
                UPDATE appointment
                SET patient_id = (
                    SELECT patient.id FROM patient
                    WHERE patient.token_number = appointment.patient_token
                    ORDER BY patient.id LIMIT 1
                )
                WHERE patient_id IS NULL AND patient_token IS NOT NULL
            """))

            # Hash imported plain-text doctor credentials without ever logging their values.
            existing_credentials = connection.execute(text("SELECT id, password FROM doctor")).all()
            for doctor_id, stored_password in existing_credentials:
                if not str(stored_password).startswith(("scrypt:", "pbkdf2:")):
                    connection.execute(
                        text("UPDATE doctor SET password = :password WHERE id = :doctor_id"),
                        {"password": generate_password_hash(stored_password or ""), "doctor_id": doctor_id},
                    )

            index_statements = [
                "CREATE INDEX IF NOT EXISTS ix_doctor_department_id ON doctor (department_id)",
                "CREATE INDEX IF NOT EXISTS ix_patient_department_id ON patient (department_id)",
                "CREATE INDEX IF NOT EXISTS ix_patient_status_token ON patient (status, token_number)",
                "CREATE INDEX IF NOT EXISTS ix_patient_department_status_token ON patient (department_id, status, token_number)",
                "CREATE INDEX IF NOT EXISTS ix_patient_doctor_id ON patient (doctor_id)",
                "CREATE INDEX IF NOT EXISTS ix_appointment_department_id ON appointment (department_id)",
                "CREATE INDEX IF NOT EXISTS ix_appointment_patient_id ON appointment (patient_id)",
                "CREATE INDEX IF NOT EXISTS ix_appointment_department_status_time ON appointment (department_id, status, scheduled_for)",
                "CREATE INDEX IF NOT EXISTS ix_appointment_scheduled_for ON appointment (scheduled_for)",
                "CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at)",
            ]
            for statement in index_statements:
                connection.exec_driver_sql(statement)

            duplicate_tokens = connection.execute(text("""
                SELECT 1 FROM patient GROUP BY token_number HAVING COUNT(*) > 1 LIMIT 1
            """)).first()
            if not duplicate_tokens:
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_token_number ON patient (token_number)"
                )
            else:
                app.logger.warning("Patient token numbers contain duplicates; uniqueness index was not added")

            duplicate_departments = connection.execute(text("""
                SELECT 1 FROM department GROUP BY lower(name) HAVING COUNT(*) > 1 LIMIT 1
            """)).first()
            if not duplicate_departments:
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_department_name_casefold ON department (lower(name))"
                )
            else:
                app.logger.warning("Department names differ only by case; normalized uniqueness index was not added")

            connection.execute(
                text("INSERT INTO schema_migration (version, description, applied_at) VALUES (:version, :description, :applied_at)"),
                {
                    "version": 1,
                    "description": "Normalize department and appointment links; hash imported passwords; add operational indexes",
                    "applied_at": datetime.now(timezone.utc),
                },
            )

        if latest < 2:
            next_token = connection.execute(text(
                "SELECT COALESCE(MAX(token_number), 0) + 1 FROM patient"
            )).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO token_sequence (name, next_value)
                    VALUES ('patient', :next_value)
                    ON CONFLICT (name) DO NOTHING
                """),
                {"next_value": next_token},
            )
            connection.execute(
                text("INSERT INTO schema_migration (version, description, applied_at) VALUES (2, :description, :applied_at)"),
                {
                    "description": "Add a transaction-safe queue token allocator for shared database use",
                    "applied_at": datetime.now(timezone.utc),
                },
            )


with app.app_context():
    backup_before_schema_change()
    db.create_all()
    migrate_database()
    app.logger.info("Database initialized at %s", db.engine.url.database)


def json_body():
    """Return a JSON object, or an empty object for malformed/non-object input."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_value(data, key, *, required=False, max_length=200):
    value = data.get(key)
    if value is None:
        return None if not required else ""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if required and not value:
        return ""
    return value[:max_length]


def require_roles(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            role = session.get("role")
            if role not in allowed_roles:
                status = 401 if not role else 403
                return jsonify(success=False, message="Please sign in to continue"), status
            if role == "doctor":
                doctor = db.session.get(Doctor, session.get("doctor_id"))
                if doctor is None:
                    session.clear()
                    return jsonify(success=False, message="Doctor account is no longer active"), 401
                session["doctor_department_id"] = doctor.department_id
                session["doctor_department"] = department_label(doctor)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def add_audit_log(action, entity_type, entity_id, details=None):
    role = session.get("role", "anonymous")
    actor_id = session.get("doctor_id") if role == "doctor" else role
    db.session.add(AuditLog(
        actor_role=role,
        actor_id=str(actor_id or role),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=details,
        created_at=datetime.now(timezone.utc),
    ))


def department_label(record):
    if record.department_record is not None:
        return record.department_record.name
    return record.department


def allocate_patient_token():
    """Increment the shared token counter inside the caller's transaction."""
    result = db.session.execute(
        update(TokenSequence)
        .where(TokenSequence.name == "patient")
        .values(next_value=TokenSequence.next_value + 1)
    )
    if result.rowcount != 1:
        raise RuntimeError("Patient token allocator is not initialized")
    return db.session.query(TokenSequence.next_value).filter_by(name="patient").scalar() - 1


def doctor_owns_department(record):
    doctor_department_id = session.get("doctor_department_id")
    if record.department_id is not None or doctor_department_id is not None:
        return record.department_id is not None and record.department_id == doctor_department_id
    return department_label(record).casefold() == str(session.get("doctor_department", "")).casefold()


def patient_json(patient):
    return {
        "id": patient.id,
        "tokenNumber": patient.token_number,
        "name": patient.name,
        "age": patient.age,
        "gender": patient.gender,
        "contactNumber": patient.contact_number,
        "address": patient.address,
        "department": department_label(patient),
        "illness": patient.illness,
        "fee": patient.fee,
        "status": patient.status,
        "findings": patient.findings,
        "diagnosis": patient.diagnosis,
        "prescriptions": patient.prescriptions,
        "doctorName": patient.doctor_name,
        "doctorId": patient.doctor_id,
    }


def appointment_json(appointment):
    scheduled_for = appointment.scheduled_for
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    else:
        scheduled_for = scheduled_for.astimezone(timezone.utc)
    return {
        "id": appointment.id,
        "name": appointment.name,
        "age": appointment.age,
        "gender": appointment.gender,
        "contactNumber": appointment.contact_number,
        "department": department_label(appointment),
        "reason": appointment.reason,
        "scheduledFor": scheduled_for.isoformat(),
        "status": appointment.status,
        "patientToken": appointment.patient_token,
    }


@app.get("/")
@app.get("/manager")
@app.get("/doctor")
@app.get("/receptionist")
@app.get("/patient")
def index():
    return render_template("pdl_frontend_akshat_tikaria.html")


@app.get("/api/auth/session")
def current_session():
    role = session.get("role")
    if role == "doctor":
        doctor = db.session.get(Doctor, session.get("doctor_id"))
        if doctor is None:
            session.clear()
            return jsonify(role=None)
        return jsonify(role="doctor", doctor={
            "id": doctor.id,
            "name": doctor.name,
            "department": department_label(doctor),
            "address": doctor.address,
            "contact": doctor.contact,
        })
    return jsonify(role=role if role in {"manager", "receptionist"} else None)


@app.post("/api/auth/logout")
def logout():
    if session.get("role"):
        add_audit_log("sign out", "session", session.get("doctor_id") or session.get("role"))
        db.session.commit()
    session.clear()
    return jsonify(success=True)


@app.post("/api/manager/login")
def manager_login():
    password = json_body().get("password")
    if isinstance(password, str) and hmac.compare_digest(password, app.config["MANAGER_PASSWORD"]):
        session.clear()
        session.permanent = True
        session["role"] = "manager"
        add_audit_log("sign in", "session", "manager")
        db.session.commit()
        return jsonify(success=True)
    return jsonify(success=False, message="Invalid password"), 401


@app.post("/api/receptionist/login")
def receptionist_login():
    password = json_body().get("password")
    if isinstance(password, str) and hmac.compare_digest(password, app.config["RECEPTIONIST_PASSWORD"]):
        session.clear()
        session.permanent = True
        session["role"] = "receptionist"
        add_audit_log("sign in", "session", "receptionist")
        db.session.commit()
        return jsonify(success=True)
    return jsonify(success=False, message="Invalid password"), 401


@app.get("/api/departments")
def get_departments():
    departments = {department.name: department.fee for department in Department.query.order_by(Department.name).all()}
    return jsonify(departments)


@app.get("/api/audit")
@require_roles("manager")
def get_audit_log():
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 250))
    except ValueError:
        limit = 100
    entries = AuditLog.query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).all()
    return jsonify([{
        "id": entry.id,
        "createdAt": (entry.created_at.replace(tzinfo=timezone.utc)
                      if entry.created_at.tzinfo is None else entry.created_at.astimezone(timezone.utc)).isoformat(),
        "actorRole": entry.actor_role,
        "actorId": entry.actor_id,
        "action": entry.action,
        "entityType": entry.entity_type,
        "entityId": entry.entity_id,
        "details": entry.details,
    } for entry in entries])


@app.get("/api/manager/database-health")
@require_roles("manager")
def database_health():
    doctor_count = Doctor.query.count()
    patient_count = Patient.query.count()
    appointment_count = Appointment.query.count()
    department_count = Department.query.count()
    unlinked_doctors = Doctor.query.filter(Doctor.department_id.is_(None)).count()
    unlinked_patients = Patient.query.filter(Patient.department_id.is_(None)).count()
    unlinked_appointments = Appointment.query.filter(Appointment.department_id.is_(None)).count()
    unlinked_checked_in_appointments = Appointment.query.filter(
        Appointment.status == "checked_in", Appointment.patient_id.is_(None)
    ).count()
    missing_token_allocator = int(db.session.get(TokenSequence, "patient") is None)
    invalid_patient_ages = Patient.query.filter(or_(Patient.age < 1, Patient.age > 120)).count()
    invalid_appointment_ages = Appointment.query.filter(or_(Appointment.age < 1, Appointment.age > 120)).count()
    unknown_doctor_references = Patient.query.outerjoin(
        Doctor, func.lower(Doctor.id) == func.lower(Patient.doctor_id)
    ).filter(
        Patient.doctor_id.isnot(None), Doctor.id.is_(None)
    ).count()
    incomplete_consultations = Patient.query.filter(
        Patient.status == "consulted",
        or_(Patient.findings.is_(None), func.trim(Patient.findings) == "",
            Patient.diagnosis.is_(None), func.trim(Patient.diagnosis) == ""),
    ).count()
    duplicate_token_groups = db.session.query(Patient.token_number).group_by(
        Patient.token_number
    ).having(func.count(Patient.id) > 1).count()
    unprotected_doctor_credentials = Doctor.query.filter(
        and_(~Doctor.password.like("scrypt:%"), ~Doctor.password.like("pbkdf2:%"))
    ).count()
    bad_patient_statuses = Patient.query.filter(or_(
        Patient.status.is_(None), Patient.status.notin_(["waiting", "called", "consulted"])
    )).count()
    duplicate_department_names = db.session.query(func.lower(Department.name)).group_by(
        func.lower(Department.name)
    ).having(func.count(Department.id) > 1).count()

    foreign_key_violations = 0
    if db.engine.dialect.name == "sqlite":
        with db.engine.connect() as connection:
            foreign_key_violations = len(connection.exec_driver_sql("PRAGMA foreign_key_check").all())

    checks = [
        ("unlinked_doctors", "Doctors without a department link", unlinked_doctors, "error"),
        ("unlinked_patients", "Patients without a department link", unlinked_patients, "error"),
        ("unlinked_appointments", "Appointments without a department link", unlinked_appointments, "error"),
        ("unlinked_checked_in_appointments", "Checked-in appointments without a patient link", unlinked_checked_in_appointments, "error"),
        ("missing_token_allocator", "Shared queue token allocator missing", missing_token_allocator, "error"),
        ("duplicate_tokens", "Duplicate patient tokens", duplicate_token_groups, "error"),
        ("duplicate_departments", "Department names that differ only by case", duplicate_department_names, "error"),
        ("foreign_key_violations", "Foreign key violations", foreign_key_violations, "error"),
        ("unprotected_credentials", "Doctor credentials not hashed", unprotected_doctor_credentials, "error"),
        ("invalid_patient_ages", "Patient ages outside 1–120", invalid_patient_ages, "warning"),
        ("invalid_appointment_ages", "Appointment ages outside 1–120", invalid_appointment_ages, "warning"),
        ("unknown_doctor_references", "Historical records with unknown doctor IDs", unknown_doctor_references, "warning"),
        ("incomplete_consultations", "Consultations missing findings or diagnosis", incomplete_consultations, "warning"),
        ("invalid_patient_statuses", "Patients with an unknown status", bad_patient_statuses, "error"),
    ]
    check_payload = [
        {"key": key, "label": label, "count": count, "severity": severity}
        for key, label, count, severity in checks
    ]
    has_issues = any(check["count"] for check in check_payload)
    return jsonify(
        status="attention" if has_issues else "healthy",
        generatedAt=datetime.now(timezone.utc).isoformat(),
        totals={
            "departments": department_count,
            "doctors": doctor_count,
            "patients": patient_count,
            "appointments": appointment_count,
        },
        checks=check_payload,
    )


@app.post("/api/departments")
@require_roles("manager")
def add_department():
    data = json_body()
    name = text_value(data, "name", required=True, max_length=100)
    if not name:
        return jsonify(success=False, message="Enter a department name"), 400
    try:
        fee = float(data.get("fee"))
    except (TypeError, ValueError):
        return jsonify(success=False, message="Enter a valid fee"), 400
    if not math.isfinite(fee) or fee < 0:
        return jsonify(success=False, message="Fee must be a non-negative number"), 400
    if Department.query.filter(func.lower(Department.name) == name.lower()).first():
        return jsonify(success=False, message="Department already exists"), 409

    db.session.add(Department(name=name, fee=fee))
    add_audit_log("created", "department", name, f"fee ₹{fee:g}")
    db.session.commit()
    return jsonify(success=True), 201


@app.delete("/api/departments/<path:dept>")
@require_roles("manager")
def delete_department(dept):
    department = Department.query.filter_by(name=dept).first()
    if not department:
        return jsonify(success=False, message="Department not found"), 404

    doctors_in_dept = Doctor.query.filter(or_(
        Doctor.department_id == department.id,
        func.lower(Doctor.department) == department.name.lower(),
    )).count()
    patient_records = Patient.query.filter(or_(
        Patient.department_id == department.id,
        func.lower(Patient.department) == department.name.lower(),
    )).count()
    appointment_records = Appointment.query.filter(or_(
        Appointment.department_id == department.id,
        func.lower(Appointment.department) == department.name.lower(),
    )).count()
    if doctors_in_dept or patient_records or appointment_records:
        return jsonify(
            success=False,
            message="Cannot delete a department that is referenced by doctors, patient records, or appointments",
        ), 409

    db.session.delete(department)
    add_audit_log("deleted", "department", dept)
    db.session.commit()
    return jsonify(success=True)


@app.get("/api/doctors")
@require_roles("manager")
def get_doctors():
    doctors = Doctor.query.order_by(Doctor.name).all()
    return jsonify({
        doctor.id: {
            "name": doctor.name,
            "department": department_label(doctor),
            "address": doctor.address,
            "contact": doctor.contact,
            "credentialsProtected": doctor.password.startswith(("scrypt:", "pbkdf2:")),
        }
        for doctor in doctors
    })


@app.post("/api/doctors")
@require_roles("manager")
def register_doctor():
    data = json_body()
    doctor_id = text_value(data, "id", required=True, max_length=50).upper()
    password = data.get("password")
    name = text_value(data, "name", required=True, max_length=100)
    department_name = text_value(data, "department", required=True, max_length=100)
    department = Department.query.filter_by(name=department_name).first()

    if not all((doctor_id, name, department_name)) or not isinstance(password, str):
        return jsonify(success=False, message="Complete all required doctor fields"), 400
    if len(password) < 8:
        return jsonify(success=False, message="Doctor passwords must be at least 8 characters"), 400
    if not department:
        return jsonify(success=False, message="Department not found"), 400
    if Doctor.query.filter_by(id=doctor_id).first():
        return jsonify(success=False, message="Doctor already exists"), 409

    doctor = Doctor(
        id=doctor_id,
        password=generate_password_hash(password),
        name=name,
        department=department.name,
        department_id=department.id,
        address=text_value(data, "address", max_length=200),
        contact=text_value(data, "contact", max_length=50),
    )
    db.session.add(doctor)
    add_audit_log("created", "doctor account", doctor.id, f"department {department.name}")
    db.session.commit()
    return jsonify(success=True), 201


@app.post("/api/doctors/login")
def doctor_login():
    data = json_body()
    doctor_id = text_value(data, "id", required=True, max_length=50).upper()
    password = data.get("password")
    doctor = db.session.get(Doctor, doctor_id) if doctor_id else None
    if not doctor or not isinstance(password, str):
        return jsonify(success=False, message="Invalid ID or password"), 401

    stored_password = doctor.password or ""
    if stored_password.startswith(("scrypt:", "pbkdf2:")):
        try:
            password_matches = check_password_hash(stored_password, password)
        except (ValueError, TypeError):
            password_matches = False
    else:
        # Upgrade older plaintext records in place after their first valid login.
        password_matches = hmac.compare_digest(stored_password, password)
        if password_matches:
            doctor.password = generate_password_hash(password)
            db.session.commit()
    if not password_matches:
        return jsonify(success=False, message="Invalid ID or password"), 401

    session.clear()
    session.permanent = True
    session["role"] = "doctor"
    session["doctor_id"] = doctor.id
    session["doctor_department_id"] = doctor.department_id
    session["doctor_department"] = department_label(doctor)
    add_audit_log("sign in", "session", doctor.id)
    db.session.commit()
    return jsonify(success=True, doctor={
        "name": doctor.name,
        "department": department_label(doctor),
        "address": doctor.address,
        "contact": doctor.contact,
    })


@app.delete("/api/doctors/<path:doctor_id>")
@require_roles("manager")
def delete_doctor(doctor_id):
    doctor = db.session.get(Doctor, doctor_id)
    if not doctor:
        return jsonify(success=False, message="Doctor not found"), 404
    db.session.delete(doctor)
    add_audit_log("deleted", "doctor account", doctor_id)
    db.session.commit()
    return jsonify(success=True)


@app.get("/api/appointments")
@require_roles("manager", "receptionist", "doctor")
def get_appointments():
    query = Appointment.query
    if session.get("role") == "doctor":
        doctor_department_id = session.get("doctor_department_id")
        legacy_department_name = str(session.get("doctor_department", "")).lower()
        if doctor_department_id is not None:
            query = query.filter(or_(
                Appointment.department_id == doctor_department_id,
                and_(Appointment.department_id.is_(None), func.lower(Appointment.department) == legacy_department_name),
            ))
        else:
            query = query.filter(func.lower(Appointment.department) == legacy_department_name)
    appointments = query.order_by(Appointment.scheduled_for, Appointment.id).all()
    return jsonify([appointment_json(appointment) for appointment in appointments])


@app.post("/api/appointments")
@require_roles("manager", "receptionist")
def create_appointment():
    data = json_body()
    name = text_value(data, "name", required=True, max_length=100)
    department_name = text_value(data, "department", required=True, max_length=100)
    gender = text_value(data, "gender", required=True, max_length=20)
    contact_number = text_value(data, "contactNumber", required=True, max_length=50)
    reason = text_value(data, "reason", max_length=200)
    department = Department.query.filter_by(name=department_name).first()
    try:
        age = int(data.get("age"))
        scheduled_for = datetime.fromisoformat(str(data.get("scheduledFor", "")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return jsonify(success=False, message="Enter a valid age and appointment date"), 400

    if not name or not contact_number or not department:
        return jsonify(success=False, message="Complete the required appointment details"), 400
    if not 1 <= age <= 120:
        return jsonify(success=False, message="Age must be between 1 and 120"), 400
    if gender.lower() not in {"male", "female", "other"}:
        return jsonify(success=False, message="Select a valid gender"), 400
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.astimezone()
    scheduled_for = scheduled_for.astimezone(timezone.utc)
    if scheduled_for < datetime.now(timezone.utc):
        return jsonify(success=False, message="Appointment time must be in the future"), 400

    appointment = Appointment(
        name=name,
        age=age,
        gender=gender.title(),
        contact_number=contact_number,
        department=department.name,
        department_id=department.id,
        reason=reason,
        scheduled_for=scheduled_for,
        status="scheduled",
    )
    db.session.add(appointment)
    db.session.flush()
    add_audit_log("scheduled", "appointment", appointment.id, f"department {appointment.department}")
    db.session.commit()
    return jsonify(success=True, appointment=appointment_json(appointment)), 201


@app.put("/api/appointments/<int:appointment_id>/check-in")
@require_roles("manager", "receptionist")
def check_in_appointment(appointment_id):
    result = db.session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id, Appointment.status == "scheduled")
        .values(status="checked_in")
    )
    if result.rowcount != 1:
        db.session.rollback()
        return jsonify(success=False, message="Appointment is no longer available for check-in"), 409

    appointment = db.session.get(Appointment, appointment_id)
    department = appointment.department_record or Department.query.filter(
        func.lower(Department.name) == str(appointment.department or "").lower()
    ).first()
    if department is None:
        db.session.rollback()
        return jsonify(success=False, message="Appointment department is no longer configured"), 409
    scheduled_for = appointment.scheduled_for
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    if scheduled_for > datetime.now(timezone.utc) + timedelta(minutes=30):
        db.session.rollback()
        return jsonify(success=False, message="Check-in opens 30 minutes before the appointment"), 409
    patient = Patient(
        token_number=allocate_patient_token(),
        name=appointment.name,
        age=appointment.age,
        gender=appointment.gender,
        contact_number=appointment.contact_number,
        department=department.name,
        department_id=department.id,
        illness=appointment.reason,
        fee=department.fee,
        status="waiting",
    )
    db.session.add(patient)
    db.session.flush()
    appointment.patient_token = patient.token_number
    appointment.patient_id = patient.id
    add_audit_log("checked in", "appointment", appointment.id, f"queue token {patient.token_number}")
    db.session.commit()
    return jsonify(success=True, token=patient.token_number)


@app.put("/api/appointments/<int:appointment_id>/cancel")
@require_roles("manager", "receptionist")
def cancel_appointment(appointment_id):
    result = db.session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id, Appointment.status == "scheduled")
        .values(status="cancelled")
    )
    if result.rowcount != 1:
        db.session.rollback()
        return jsonify(success=False, message="Only scheduled appointments can be cancelled"), 409
    add_audit_log("cancelled", "appointment", appointment_id)
    db.session.commit()
    return jsonify(success=True)


@app.get("/api/patients")
def get_patients():
    role = session.get("role")
    if role in {"manager", "receptionist"}:
        patients = Patient.query.order_by(Patient.token_number).all()
        return jsonify([patient_json(patient) for patient in patients])
    if role == "doctor":
        doctor_department_id = session.get("doctor_department_id")
        legacy_department_name = str(session.get("doctor_department", "")).lower()
        if doctor_department_id is not None:
            patient_query = Patient.query.filter(or_(
                Patient.department_id == doctor_department_id,
                and_(Patient.department_id.is_(None), func.lower(Patient.department) == legacy_department_name),
            ))
        else:
            patient_query = Patient.query.filter(func.lower(Patient.department) == legacy_department_name)
        patients = patient_query.order_by(Patient.token_number).all()
        return jsonify([patient_json(patient) for patient in patients])

    # Public queue view contains only operational details, never patient identity or health data.
    active_patients = Patient.query.filter(
        Patient.status.in_(["waiting", "called"])
    ).order_by(Patient.token_number).all()
    return jsonify([
        {"tokenNumber": patient.token_number, "department": department_label(patient), "status": patient.status}
        for patient in active_patients
    ])


@app.post("/api/patients")
@require_roles("receptionist", "manager")
def register_patient():
    data = json_body()
    name = text_value(data, "name", required=True, max_length=100)
    department_name = text_value(data, "department", required=True, max_length=100)
    gender = text_value(data, "gender", required=True, max_length=20)
    department = Department.query.filter_by(name=department_name).first()
    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        return jsonify(success=False, message="Enter a valid age"), 400

    if not name or not gender:
        return jsonify(success=False, message="Name and gender are required"), 400
    if age < 1 or age > 120:
        return jsonify(success=False, message="Age must be between 1 and 120"), 400
    if gender.lower() not in {"male", "female", "other"}:
        return jsonify(success=False, message="Select a valid gender"), 400
    if not department:
        return jsonify(success=False, message="Department not found"), 400

    patient = Patient(
        token_number=allocate_patient_token(),
        name=name,
        age=age,
        gender=gender.title(),
        contact_number=text_value(data, "contactNumber", max_length=50),
        address=text_value(data, "address", max_length=200),
        department=department.name,
        department_id=department.id,
        illness=text_value(data, "illness", max_length=200),
        fee=department.fee,
        status="waiting",
    )
    db.session.add(patient)
    db.session.flush()
    add_audit_log("registered", "patient", patient.token_number, f"department {patient.department}")
    db.session.commit()
    return jsonify(success=True, token=patient.token_number, fee=department.fee), 201


@app.put("/api/patients/<int:token>/call")
@require_roles("doctor")
def call_patient(token):
    patient = Patient.query.filter_by(token_number=token).first()
    if not patient:
        return jsonify(success=False, message="Patient not found"), 404
    if not doctor_owns_department(patient):
        return jsonify(success=False, message="Patient is outside your department"), 403
    if patient.status != "waiting":
        return jsonify(success=False, message="Patient is not in the waiting queue"), 409

    result = db.session.execute(
        update(Patient)
        .where(Patient.id == patient.id, Patient.status == "waiting")
        .values(status="called")
    )
    if result.rowcount != 1:
        db.session.rollback()
        return jsonify(success=False, message="Patient is no longer waiting in the queue"), 409
    add_audit_log("called", "patient", patient.token_number, f"department {patient.department}")
    db.session.commit()
    return jsonify(success=True)


@app.put("/api/patients/<int:token>/consult")
@require_roles("doctor")
def consult_patient(token):
    patient = Patient.query.filter_by(token_number=token).first()
    if not patient:
        return jsonify(success=False, message="Patient not found"), 404
    if not doctor_owns_department(patient):
        return jsonify(success=False, message="Patient is outside your department"), 403
    if patient.status != "called":
        return jsonify(success=False, message="Call the patient before completing a consultation"), 409

    data = json_body()
    findings = text_value(data, "findings", required=True, max_length=300)
    diagnosis = text_value(data, "diagnosis", required=True, max_length=300)
    if not findings or not diagnosis:
        return jsonify(success=False, message="Findings and diagnosis are required"), 400

    doctor_id = session.get("doctor_id")
    doctor = db.session.get(Doctor, doctor_id)
    if doctor is None:
        db.session.rollback()
        session.clear()
        return jsonify(success=False, message="Doctor account is no longer active"), 401
    result = db.session.execute(
        update(Patient)
        .where(Patient.id == patient.id, Patient.status == "called")
        .values(
            findings=findings,
            diagnosis=diagnosis,
            prescriptions=text_value(data, "prescriptions", max_length=4000),
            doctor_name=doctor.name,
            doctor_id=doctor.id,
            status="consulted",
        )
    )
    if result.rowcount != 1:
        db.session.rollback()
        return jsonify(success=False, message="Patient consultation was already completed"), 409
    add_audit_log("consultation saved", "patient", patient.token_number, f"doctor {doctor_id}")
    db.session.commit()
    return jsonify(success=True)


if __name__ == "__main__":
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    if os.environ.get("APP_ENV", "development").lower() == "production":
        from waitress import serve

        serve(app, host=os.environ.get("APP_HOST", "0.0.0.0"), port=port)
    else:
        app.run(host=host, port=port, debug=os.environ.get("APP_DEBUG", "false").lower() == "true")
