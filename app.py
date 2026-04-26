from flask import Flask, jsonify, request, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os

app = Flask(__name__, template_folder='templates')
CORS(app)

# --------------------------------------------------
# Database Configuration
# --------------------------------------------------
DB_PATH = r"C:\HospitalDB\hospital.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --------------------------------------------------
# Models
# --------------------------------------------------
class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    fee = db.Column(db.Float, nullable=False)

class Doctor(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    password = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=False)
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
    illness = db.Column(db.String(200))
    fee = db.Column(db.Float)
    status = db.Column(db.String(50), default='waiting')  # waiting / called / consulted
    findings = db.Column(db.String(300))
    diagnosis = db.Column(db.String(300))
    prescriptions = db.Column(db.Text)
    doctor_name = db.Column(db.String(100))
    doctor_id = db.Column(db.String(50))

# --------------------------------------------------
# Initialize Database
# --------------------------------------------------
with app.app_context():
    db.create_all()
    print(f"✅ Database initialized at {DB_PATH}")

# --------------------------------------------------
# Manager login (password static)
# --------------------------------------------------
manager_password = "admin123"

@app.route('/')
@app.route('/manager')
@app.route('/doctor')
@app.route('/patient')
def index():
    return render_template('pdl_frontend_akshat_tikaria.html')

@app.route('/api/manager/login', methods=['POST'])
def manager_login():
    data = request.get_json()
    if data.get('password') == manager_password:
        print("👨‍💼 Manager logged in successfully")
        return jsonify(success=True)
    print("❌ Manager login failed")
    return jsonify(success=False, message='Invalid password'), 401

# --------------------------------------------------
# Departments
# --------------------------------------------------
@app.route('/api/departments', methods=['GET'])
def get_departments():
    departments = {d.name: d.fee for d in Department.query.all()}
    return jsonify(departments)

@app.route('/api/departments', methods=['POST'])
def add_department():
    data = request.get_json()
    name, fee = data.get('name'), data.get('fee')
    if not name or fee is None:
        return jsonify(success=False, message='Missing department name or fee'), 400

    if Department.query.filter_by(name=name).first():
        return jsonify(success=False, message='Department already exists'), 400

    new_dept = Department(name=name, fee=fee)
    db.session.add(new_dept)
    db.session.commit()
    print(f"💾 Added Department: {name} (₹{fee})")
    return jsonify(success=True)

@app.route('/api/departments/<dept>', methods=['DELETE'])
def delete_department(dept):
    department = Department.query.filter_by(name=dept).first()
    if not department:
        return jsonify(success=False, message='Department not found')

    doctors_in_dept = Doctor.query.filter_by(department=dept).count()
    waiting_patients_in_dept = Patient.query.filter_by(department=dept, status='waiting').count()

    if doctors_in_dept > 0 or waiting_patients_in_dept > 0:
        return jsonify(success=False,
                       message='Cannot delete department with assigned doctors or waiting patients')

    db.session.delete(department)
    db.session.commit()
    print(f"🗑️ Deleted Department: {dept}")
    return jsonify(success=True)

# --------------------------------------------------
# Doctors
# --------------------------------------------------
@app.route('/api/doctors', methods=['GET'])
def get_doctors():
    all_docs = {
        doc.id: {
            "password": doc.password,
            "name": doc.name,
            "department": doc.department,
            "address": doc.address,
            "contact": doc.contact
        }
        for doc in Doctor.query.all()
    }
    return jsonify(all_docs)

@app.route('/api/doctors', methods=['POST'])
def register_doctor():
    data = request.get_json()
    doc_id = data.get('id')
    if Doctor.query.filter_by(id=doc_id).first():
        return jsonify(success=False, message='Doctor already exists')
    new_doc = Doctor(
        id=doc_id,
        password=data.get('password'),
        name=data.get('name'),
        department=data.get('department'),
        address=data.get('address'),
        contact=data.get('contact')
    )
    db.session.add(new_doc)
    db.session.commit()
    print(f"💾 Registered Doctor: {new_doc.name} ({new_doc.department})")
    return jsonify(success=True)

@app.route('/api/doctors/login', methods=['POST'])
def doctor_login():
    data = request.get_json()
    doc = Doctor.query.filter_by(id=data.get('id'), password=data.get('password')).first()
    if not doc:
        return jsonify(success=False, message='Invalid ID or password')
    return jsonify(success=True, doctor={
        "name": doc.name,
        "department": doc.department,
        "address": doc.address,
        "contact": doc.contact
    })

@app.route('/api/doctors/<doc_id>', methods=['DELETE'])
def delete_doctor(doc_id):
    doc = Doctor.query.filter_by(id=doc_id).first()
    if not doc:
        return jsonify(success=False, message='Doctor not found')
    db.session.delete(doc)
    db.session.commit()
    print(f"🗑️ Deleted Doctor: {doc_id}")
    return jsonify(success=True)

# --------------------------------------------------
# Patients
# --------------------------------------------------
@app.route('/api/patients', methods=['GET'])
def get_patients():
    patients = [
        {
            "id": p.id,
            "tokenNumber": p.token_number,
            "name": p.name,
            "age": p.age,
            "gender": p.gender,
            "contactNumber": p.contact_number,
            "address": p.address,
            "department": p.department,
            "illness": p.illness,
            "fee": p.fee,
            "status": p.status,
            "findings": p.findings,
            "diagnosis": p.diagnosis,
            "prescriptions": p.prescriptions,
            "doctorName": p.doctor_name,
            "doctorId": p.doctor_id
        }
        for p in Patient.query.all()
    ]
    return jsonify(patients)

@app.route('/api/patients', methods=['POST'])
def register_patient():
    data = request.get_json()
    dept = data.get('department')
    department = Department.query.filter_by(name=dept).first()
    if not department:
        return jsonify(success=False, message='Department not found')

    last_patient = Patient.query.order_by(Patient.token_number.desc()).first()
    token_number = (last_patient.token_number + 1) if last_patient else 1

    new_patient = Patient(
        token_number=token_number,
        name=data.get('name'),
        age=data.get('age'),
        gender=data.get('gender'),
        contact_number=data.get('contactNumber'),
        address=data.get('address'),
        department=dept,
        illness=data.get('illness'),
        fee=department.fee,
        status='waiting'
    )
    db.session.add(new_patient)
    db.session.commit()
    print(f"🏥 Registered Patient: {new_patient.name} (Token #{token_number})")
    return jsonify(success=True, token=token_number, fee=department.fee)

@app.route('/api/patients/<int:token>/call', methods=['PUT'])
def call_patient(token):
    """Doctor signals that patient is being called for consultation."""
    patient = Patient.query.filter_by(token_number=token).first()
    if not patient:
        return jsonify(success=False, message='Patient not found')
    if patient.status != 'waiting':
        return jsonify(success=False, message='Patient not in waiting queue')

    patient.status = 'called'
    db.session.commit()
    print(f"📞 Patient Token #{token} has been called by doctor")
    return jsonify(success=True)

@app.route('/api/patients/<int:token>/consult', methods=['PUT'])
def consult_patient(token):
    data = request.get_json()
    patient = Patient.query.filter_by(token_number=token).first()
    if not patient:
        return jsonify(success=False, message='Patient not found')

    patient.findings = data.get('findings')
    patient.diagnosis = data.get('diagnosis')
    patient.prescriptions = str(data.get('prescriptions'))
    patient.doctor_name = data.get('doctorName')
    patient.doctor_id = data.get('doctorId')
    patient.status = 'consulted'
    db.session.commit()
    print(f"🩺 Consultation completed for Token #{token}")
    return jsonify(success=True)

# --------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
