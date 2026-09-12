from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from typing import Optional 
import json
import io
import re
import os
import traceback

from supabase import create_client, Client

# Document parsing libraries
from docx import Document
from pypdf import PdfReader

app = FastAPI(title="City Public School Secure CBT Backend", version="6.2")

# Add CORS middleware to allow frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the dist folder so FastAPI can serve output.css
# app.mount("/dist", StaticFiles(directory="dist"), name="dist")

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xpblznnqqwicaztzgamw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhwYmx6bm5xcXdpY2F6dHpnYW13Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk3MTQ4ODMsImV4cCI6MjA5NTI5MDg4M30._6_TA3hVMYITirfqORltzEGkcMGpp-EqU6YXMOtl_Bw")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

active_sessions = {}

# Pydantic Schemas
class LoginRequest(BaseModel):
    username: str
    password: str
    role: str
    class_name: str
    roll: str

class StudentRegisterRequest(BaseModel):
    name: str
    username: str
    password: str
    class_name: str  # Strictly required for student registration
    roll: str        # Strictly required for student registration

class SubmissionRequest(BaseModel):
    username: str
    exam_id: str
    answers: Dict[str, Any]

class GradeRequest(BaseModel):
    submission_index: int
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
        lines = text_content.split('\n')
        for line in lines:
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

@app.get("/api/users")
def get_users():
    try:
        response = supabase.table("users").select("*").execute()
        return response.data or []
    except Exception as e:
        print(f"Supabase users error: {e}")
        return []


@app.post("/api/login")
def login_user(data: LoginRequest):
    username = data.username.strip().lower()
    
    # Check Admin
    if username == "admin" and data.password == "Admin@2511":
        try:
            response = supabase.table("users").select("*").eq("role", "Admin").execute()
            admin = response.data[0] if response.data else { "id": 1, "role": "Admin", "class": "N/A", "subject": "All", "name": "ADMINISTRATOR", "username": "admin", "password": "Admin@2511", "status": "approved" }
            return {"success": True, "user": admin}
        except Exception:
            return {"success": True, "user": { "id": 1, "role": "Admin", "class": "N/A", "subject": "All", "name": "ADMINISTRATOR", "username": "admin", "password": "Admin@2511", "status": "approved" }}

    try:
        response = supabase.table("users").select("*").ilike("username", username).execute()
        users = response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not users:
        raise HTTPException(status_code=404, detail="User not found.")
    
    user = users[0]
    if user["password"] != data.password:
        raise HTTPException(status_code=401, detail="Incorrect password.")

    if user["status"] == "locked":
        raise HTTPException(status_code=403, detail="Account is locked due to security violations or multi-device login.")

    if user["role"] == "Student":
        if username in active_sessions:
            try:
                supabase.table("users").update({"status": "locked"}).eq("id", user["id"]).execute()
            except Exception:
                pass
            raise HTTPException(status_code=403, detail="Multiple session detected. Account locked.")
        active_sessions[username] = True
        if data.class_name:
            try:
                supabase.table("users").update({"class": data.class_name}).eq("id", user["id"]).execute()
            except Exception:
                pass
            user["class"] = data.class_name

    return {"success": True, "user": user}


@app.post("/api/users")
@app.post("/api/register")
def register_user(payload: Dict[str, Any]):
    try:
        username = str(payload.get("username", "")).strip().lower()
        password = str(payload.get("password", ""))
        name = str(payload.get("name", "")).strip().upper()
        role = str(payload.get("role", "Student"))
        class_name = payload.get("class") or payload.get("className") or payload.get("class_name") or ""
        subject = payload.get("subject", "All")
        roll = str(payload.get("roll", "")).strip()

        # Enforce strict mandatory fields for student registration
        if role == "Student":
            if not username or not password or not name or not class_name or not roll:
                raise HTTPException(
                    status_code=400, 
                    detail="All fields (Name, Username, Password, Class, and Roll Number) are mandatory for student registration."
                )
        else:
            if not username or not password or not name:
                raise HTTPException(
                    status_code=400, 
                    detail="Username, password, and name are required."
                )

        # Construct payload with core fields matching standard Supabase tables
        new_user = {
            "role": role,
            "class": class_name if role == "Student" else "N/A",
            "subject": subject if role == "Teacher" else "All",
            "name": name,
            "username": username,
            "password": password,
            "roll": roll if role == "Student" else None,
            "status": "pending" if role == "Teacher" else "approved"
        }

        try:
            supabase.table("users").insert(new_user).execute()
        except Exception as insert_err:
            # Fallback to minimal fields if custom columns don't exist in Supabase
            print(f"Fallback to minimal user insert: {insert_err}")
            minimal_user = {
                "role": role,
                "name": name,
                "username": username,
                "password": password
            }
            supabase.table("users").insert(minimal_user).execute()

        return {"success": True, "message": "Registration successful", "status": new_user["status"]}
        
    except HTTPException as he:
        raise he
    except Exception as e:
        traceback.print_exc()
        # Expose the exact error detail so you can see it immediately in the browser response
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/exams")
def get_exams(class_name: Optional[str] = Query(None, alias="className")):
    try:
        response = supabase.table("exams").select("*").execute()
        exams_db = response.data or []
        
        if class_name:
            return [
                e for e in exams_db 
                if e.get("class") == class_name or e.get("class") == "All Sections"
            ]
            
        return exams_db
    except Exception as e:
        print(f"Supabase exams error: {e}")
        return []


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
    classes_list = json.loads(target_classes)
    content_bytes = await file.read()
    filename = file.filename.lower()
    
    parsed_questions = []

    if filename.endswith('.json'):
        parsed_questions = json.loads(content_bytes.decode('utf-8'))
    elif filename.endswith('.txt'):
        parsed_questions = parse_text_to_questions(content_bytes.decode('utf-8'), False)
    elif filename.endswith('.docx') or filename.endswith('.doc'):
        doc = Document(io.BytesIO(content_bytes))
        full_text = "\n".join([p.text for p in doc.paragraphs])
        parsed_questions = parse_text_to_questions(full_text, False)
    elif filename.endswith('.pdf'):
        reader = PdfReader(io.BytesIO(content_bytes))
        full_text = ""
        for page in reader.pages:
            full_text += (page.extract_text() or "") + "\n"
        parsed_questions = parse_text_to_questions(full_text, False)
    else:
        raise HTTPException(status_code=400, detail="Unsupported file format.")

    try:
        for cls in classes_list:
            exam_id = f"{subject_id}_{cls.lower().replace(' ', '')}"
            existing_res = supabase.table("exams").select("*").eq("id", exam_id).execute()
            existing = existing_res.data[0] if existing_res.data else None
            
            exam_data = {
                "id": exam_id,
                "subjectId": subject_id,
                "name": f"{subject_name} - {cls}",
                "subject": subject_name,
                "class": cls,
                "status": existing.get("status", "OFFLINE") if existing else "OFFLINE",
                "startTime": start_time or "",
                "endTime": end_time or "",
                "durationMinutes": duration,
                "questions": parsed_questions
            }

            if existing:
                supabase.table("exams").update(exam_data).eq("id", exam_id).execute()
            else:
                supabase.table("exams").insert(exam_data).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database save error: {str(e)}")

    return {"success": True, "message": f"Successfully assigned question paper to {len(classes_list)} classes."}


@app.post("/api/submit-exam")
def submit_exam(data: SubmissionRequest):
    try:
        exam_res = supabase.table("exams").select("*").eq("id", data.exam_id).execute()
        if not exam_res.data:
            raise HTTPException(status_code=404, detail="Exam not found.")
        exam = exam_res.data[0]

        user_res = supabase.table("users").select("*").ilike("username", data.username).execute()
        if not user_res.data:
            raise HTTPException(status_code=404, detail="User not found.")
        user = user_res.data[0]
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    score = 0
    total_mcq = 0
    desc_answered = 0

    for q in exam["questions"]:
        q_id_str = str(q["id"])
        if q["type"] == "mcq":
            total_mcq += 1
            if data.answers.get(q_id_str) == q["correct"] or data.answers.get(int(q_id_str)) == q["correct"]:
                score += 1
        elif q["type"] == "descriptive":
            if data.answers.get(q_id_str) or data.answers.get(int(q_id_str)):
                desc_answered += 1

    score_str = f"{score}/{total_mcq} MCQ Correct" if total_mcq > 0 else f"{desc_answered} Descriptive Answered"

    submission = {
        "name": user["name"],
        "class": user["class"],
        "subject": exam["subject"],
        "examId": exam["id"],
        "score": score_str,
        "answers": data.answers,
        "gradedMarks": {}
    }
    
    try:
        supabase.table("submissions").insert(submission).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save submission: {str(e)}")
    
    if data.username in active_sessions:
        del active_sessions[data.username]

    return {"success": True, "score": score_str}


@app.get("/api/submissions")
def get_submissions(class_name: Optional[str] = None, subject: Optional[str] = None):
    try:
        res = supabase.table("submissions").select("*").execute()
        results = res.data or []
        if class_name and class_name != "ALL":
            results = [s for s in results if s.get("class") == class_name]
        if subject:
            results = [s for s in results if subject.lower() in s.get("subject", "").lower()]
        return results
    except Exception as e:
        print(f"Supabase submissions error: {e}")
        return []


@app.post("/api/grade-descriptive")
def grade_descriptive(data: GradeRequest):
    try:
        res = supabase.table("submissions").select("*").order("id").execute()
        submissions = res.data or []
        if data.submission_index < 0 or data.submission_index >= len(submissions):
            raise HTTPException(status_code=404, detail="Submission not found.")
        
        sub = submissions[data.submission_index]
        graded_marks = sub.get("gradedMarks") or {}
        graded_marks[str(data.question_id)] = data.grade
        
        supabase.table("submissions").update({"gradedMarks": graded_marks}).eq("id", sub["id"]).execute()
        return {"success": True, "message": "Grade saved successfully."}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.post("/api/users/unlock")
def unlock_user(username: str = Form(...)):
    try:
        user_res = supabase.table("users").select("*").ilike("username", username).execute()
        if not user_res.data:
            raise HTTPException(status_code=404, detail="Username not found.")
        user = user_res.data[0]
        
        supabase.table("users").update({"status": "approved"}).eq("id", user["id"]).execute()
        if username in active_sessions:
            del active_sessions[username]
        return {"success": True, "message": f"User {user['name']} unlocked successfully."}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")