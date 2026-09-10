from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
import io
import re
import os

# Document parsing libraries
try:
    from docx import Document
except ImportError:
    Document = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

app = FastAPI(title="City Public School Secure CBT Backend", version="6.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Safe Supabase Initialization (Prevents Serverless Startup Crashes)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase = None
try:
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        from supabase import create_client, Client
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
except Exception as e:
    print(f"Supabase initialization error: {e}")

def get_db():
    if not supabase:
        raise HTTPException(
            status_code=500, 
            detail="Supabase client is not initialized. Please ensure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are correctly configured in Vercel Environment Variables and redeploy."
        )
    return supabase

# Pydantic Schemas
class LoginRequest(BaseModel):
    username: str
    password: str
    role: str
    class_name: Optional[str] = None
    roll: Optional[str] = None

class RegisterRequest(BaseModel):
    name: str
    username: str
    password: str
    role: str
    subject: Optional[str] = "All"
    class_name: Optional[str] = "Class 9th - Section A"
    roll: Optional[str] = "1"

class SubmissionRequest(BaseModel):
    username: str
    exam_id: str
    answers: Dict[str, Any]

class GradeRequest(BaseModel):
    submission_id: int
    question_id: int
    grade: str


# Helper Function: Robust Text & Document Question Parser
def parse_text_to_questions(text_content: str, is_html: bool = False) -> List[Dict[str, Any]]:
    questions = []
    q_id = 1
    current_case_study = ""
    target_range_start = None
    target_range_end = None

    def process_line(raw_text: str, raw_html: str):
        nonlocal q_id, current_case_study, target_range_start, target_range_end
        text = raw_text.strip()
        if not text:
            return

        case_study_match = re.search(r"case\s*study|read\s*the\s*following|passage", text, re.IGNORECASE)
        range_match = re.search(r"questions?\s*(\d+)\s*(?:to|-)\s*(\d+)", text, re.IGNORECASE)

        is_question = bool(re.match(r"^(Q\.?\s*\d+|\d+[\.\)]\s*)", text, re.IGNORECASE))
        is_option = bool(re.match(r"^([A-Da-d][\.\)]|\([A-Da-d]\))\s*", text))

        if case_study_match and not is_question and not is_option:
            current_case_study = raw_html or text
            target_range_start, target_range_end = None, None
            if range_match:
                target_range_start = int(range_match.group(1))
                target_range_end = int(range_match.group(2))
            return

        if is_question:
            q_num_match = re.search(r"^(?:Q\.?\s*)?(\d+)", text, re.IGNORECASE)
            q_num = int(q_num_match.group(1)) if q_num_match else q_id

            active_cs = current_case_study
            if target_range_start is not None and target_range_end is not None:
                if q_num < target_range_start or q_num > target_range_end:
                    active_cs = ""

            questions.append({
                "id": q_id,
                "type": "descriptive",
                "q": raw_html or text,
                "options": [],
                "correct": 0,
                "caseStudy": active_cs if active_cs else None
            })
            q_id += 1
        elif is_option and len(questions) > 0:
            last_q = questions[-1]
            if last_q["type"] == "descriptive":
                last_q["type"] = "mcq"
                last_q["options"] = []
            last_q["options"].append(raw_html or text)
        elif len(questions) > 0:
            last_q = questions[-1]
            if len(last_q["options"]) > 0 and not is_option:
                last_q["q"] += "<br>" + (raw_html or text)
            else:
                last_q["q"] += "<br>" + (raw_html or text)
        elif current_case_study:
            current_case_study += "<br>" + (raw_html or text)

    if is_html:
        for line in text_content.split('\n'):
            process_line(line, line)
    else:
        for line in text_content.split('\n'):
            process_line(line, line)

    if not questions:
        questions.append({
            "id": 1,
            "type": "descriptive",
            "q": text_content[:400],
            "options": [],
            "correct": 0
        })

    return questions


# API Endpoints

@app.get("/")
def read_root():
    return RedirectResponse(url="/docs")


@app.post("/api/login")
def login_user(data: LoginRequest):
    db = get_db()
    username = data.username.strip().lower()
    
    if username == "admin" and data.password == "Admin@2511":
        res = db.table("users").select("*").eq("role", "Admin").execute()
        if res.data:
            return {"success": True, "user": res.data[0]}
        return {"success": True, "user": { "id": 1, "role": "Admin", "class": "N/A", "subject": "All", "name": "ADMINISTRATOR", "username": "admin", "status": "approved" }}

    res = db.table("users").select("*").eq("username", username).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    
    user = res.data[0]
    if user["password"] != data.password:
        raise HTTPException(status_code=401, detail="Incorrect password.")

    if user["status"] == "locked":
        raise HTTPException(status_code=403, detail="Account is locked due to security violations or multi-device login.")

    if user["role"] == "Student":
        session_res = db.table("active_sessions").select("*").eq("username", username).execute()
        if session_res.data:
            db.table("users").update({"status": "locked"}).eq("username", username).execute()
            raise HTTPException(status_code=403, detail="Multiple session detected. Account locked.")
        
        db.table("active_sessions").upsert({"username": username, "active": True}).execute()
        if data.class_name:
            db.table("users").update({"class": data.class_name}).eq("username", username).execute()
            user["class"] = data.class_name

    return {"success": True, "user": user}


@app.post("/api/register")
def register_user(data: RegisterRequest):
    db = get_db()
    existing = db.table("users").select("username").eq("username", data.username.lower()).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Username already exists.")

    status_val = "pending" if data.role == "Teacher" else "approved"
    new_user_data = {
        "role": data.role,
        "class": data.class_name if data.role == "Student" else "N/A",
        "subject": data.subject if data.role == "Teacher" else "All",
        "name": data.name.upper(),
        "username": data.username.lower(),
        "password": data.password,
        "roll": data.roll if data.role == "Student" else None,
        "status": status_val
    }
    db.table("users").insert(new_user_data).execute()
    return {"success": True, "message": "Registration successful", "status": status_val}


@app.get("/api/exams")
def get_exams(class_name: Optional[str] = None):
    db = get_db()
    res = db.table("exams").select("*").execute()
    exams = res.data or []
    if class_name:
        return [e for e in exams if e.get("class") == class_name or e.get("class") == "All Sections"]
    return exams


@app.post("/api/exams/upload-multi-class")
async def upload_question_paper(
    subject_id: str = Form(...),
    subject_name: str = Form(...),
    duration: int = Form(45),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
    target_classes: str = Form(...),
    file: UploadFile = File(...)
):
    db = get_db()
    classes_list = json.loads(target_classes)
    content_bytes = await file.read()
    filename = file.filename.lower()
    
    parsed_questions = []

    if filename.endswith('.json'):
        parsed_questions = json.loads(content_bytes.decode('utf-8'))
    elif filename.endswith('.txt'):
        parsed_questions = parse_text_to_questions(content_bytes.decode('utf-8'), False)
    elif (filename.endswith('.docx') or filename.endswith('.doc')) and Document:
        doc = Document(io.BytesIO(content_bytes))
        full_text = "\n".join([p.text for p in doc.paragraphs])
        parsed_questions = parse_text_to_questions(full_text, False)
    elif filename.endswith('.pdf') and PdfReader:
        reader = PdfReader(io.BytesIO(content_bytes))
        full_text = ""
        for page in reader.pages:
            full_text += (page.extract_text() or "") + "\n"
        parsed_questions = parse_text_to_questions(full_text, False)
    else:
        raise HTTPException(status_code=400, detail="Unsupported file format or required parser library missing.")

    for cls in classes_list:
        exam_id = f"{subject_id}_{cls.lower().replace(' ', '')}"
        exam_payload = {
            "id": exam_id,
            "subject_id": subject_id,
            "name": f"{subject_name} - {cls}",
            "subject": subject_name,
            "class": cls,
            "status": "OFFLINE",
            "start_time": start_time or "",
            "end_time": end_time or "",
            "duration_minutes": duration,
            "questions": parsed_questions
        }
        db.table("exams").upsert(exam_payload).execute()

    return {"success": True, "message": f"Successfully assigned question paper to {len(classes_list)} classes."}


@app.post("/api/submit-exam")
def submit_exam(data: SubmissionRequest):
    db = get_db()
    exam_res = db.table("exams").select("*").eq("id", data.exam_id).execute()
    if not exam_res.data:
        raise HTTPException(status_code=404, detail="Exam not found.")
    exam = exam_res.data[0]

    user_res = db.table("users").select("*").eq("username", data.username.lower()).execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    user = user_res.data[0]

    score = 0
    total_mcq = 0
    desc_answered = 0

    questions = exam.get("questions", [])
    for q in questions:
        q_id_str = str(q["id"])
        if q["type"] == "mcq":
            total_mcq += 1
            if data.answers.get(q_id_str) == q["correct"] or data.answers.get(int(q_id_str)) == q["correct"]:
                score += 1
        elif q["type"] == "descriptive":
            if data.answers.get(q_id_str) or data.answers.get(int(q_id_str)):
                desc_answered += 1

    score_str = f"{score}/{total_mcq} MCQ Correct" if total_mcq > 0 else f"{desc_answered} Descriptive Answered"

    submission_payload = {
        "name": user["name"],
        "class": user["class"],
        "subject": exam["subject"],
        "exam_id": exam["id"],
        "score": score_str,
        "answers": data.answers,
        "graded_marks": {}
    }
    db.table("submissions").insert(submission_payload).execute()
    db.table("active_sessions").delete().eq("username", data.username.lower()).execute()

    return {"success": True, "score": score_str}


@app.get("/api/submissions")
def get_submissions(class_name: Optional[str] = None, subject: Optional[str] = None):
    db = get_db()
    res = db.table("submissions").select("*").execute()
    results = res.data or []
    if class_name and class_name != "ALL":
        results = [s for s in results if s.get("class") == class_name]
    if subject:
        results = [s for s in results if subject.lower() in s.get("subject", "").lower()]
    return results


@app.post("/api/grade-descriptive")
def grade_descriptive(data: GradeRequest):
    db = get_db()
    sub_res = db.table("submissions").select("*").eq("id", data.submission_id).execute()
    if not sub_res.data:
        raise HTTPException(status_code=404, detail="Submission not found.")
    
    sub = sub_res.data[0]
    graded_marks = sub.get("graded_marks") or {}
    graded_marks[str(data.question_id)] = data.grade
    
    db.table("submissions").update({"graded_marks": graded_marks}).eq("id", data.submission_id).execute()
    return {"success": True, "message": "Grade saved successfully."}


@app.post("/api/users/unlock")
def unlock_user(username: str = Form(...)):
    db = get_db()
    user_res = db.table("users").select("*").eq("username", username.lower()).execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    user = user_res.data[0]

    db.table("users").update({"status": "approved"}).eq("username", username.lower()).execute()
    db.table("active_sessions").delete().eq("username", username.lower()).execute()
    return {"success": True, "message": f"User {user['name']} unlocked successfully."}