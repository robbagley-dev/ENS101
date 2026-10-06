#!/usr/bin/env python3
"""Zero-dependency backend for ENS 101 Mentor Desk v1.0.

The app can use an optional OpenAI-compatible local endpoint, then an optional
Gemini key, and always retains a useful offline guidance layer. No AI endpoint
is contacted unless it is explicitly configured in the environment.
"""

import base64
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import ssl
import stat
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'AI AGENTS LOCAL LLM', 'shared'))
from career_explorer_engine import (
    parse_career_explorer_pdf, build_briefing, build_career_recommendations,
    generate_guidance_sections, personality_description,
    hexaco_terminology_prompt, normalize_assessment_data, assessment_prompt_context,
    STRENGTHS_FRAMING_PROMPT, PROFILE_MENTOR_ENS101,
    TRAIT_EXPLANATIONS, MAJOR_ALIGNMENTS, CAREER_ALIGNMENTS,
)
from urllib.request import Request, urlopen

from readiness_client import (
    ReadinessContractError,
    ReadinessRequestError,
    ReadinessUnavailable,
    lookup_ens101_projection,
)

try:
    from ai_fallback_notifier import notify_qwen_fallback
except ImportError:
    def notify_qwen_fallback(*args, **kwargs):
        pass

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

# Load .env file manually if present (no external packages needed)
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Optional primary engine: any OpenAI-compatible local or hosted endpoint.
# It is deliberately disabled by default; configure it in .env when desired.
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://127.0.0.1:1234/v1").strip().rstrip("/")
QWEN_MODEL = os.environ.get("MODEL_NAME", "qwen3-vl-30b-a3b-instruct-mlx").strip()

# Fallback Engine: Google Gemini API (optional, used if Qwen is unreachable)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Server Settings
PORT = int(os.environ.get("PORT", "5050"))
HOST = os.environ.get("HOST", "0.0.0.0")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "50"))

STATIC_DIR = Path(__file__).resolve().parent / "static"
_configured_db_path = os.environ.get("ENS101_DB_PATH", "").strip()
DB_PATH = (
    Path(_configured_db_path).expanduser()
    if _configured_db_path
    else Path(__file__).resolve().parent / "feedback.db"
)
ROOT_DIR = Path(__file__).resolve().parent

ENSIGN_EMAIL_PATTERN = re.compile(r"^[^@\s]+@ensign\.edu$", re.IGNORECASE)

# Student readiness comes only from the Student Readiness Hub `ens101.v1`
# projection. These former live PeopleGrove / Career Explorer endpoints answer
# 410 Gone for one release so stale clients fail visibly instead of silently.
RETIRED_SOURCE_ENDPOINTS = frozenset({
    "/api/career-explorer/session",
    "/api/career-explorer/admin-status",
    "/api/career-explorer/launch-login",
    "/api/career-explorer/lookup",
    "/api/ensign-connect/session",
    "/api/ensign-connect/admin-status",
    "/api/ensign-connect/launch-login",
    "/api/ensign-connect/lookup",
    "/api/ensign-connect/lookup-live",
})


def build_student_readiness_response(email: str, lookup_fn=None) -> tuple[HTTPStatus, dict]:
    """Translate one ens101.v1 lookup into an HTTP status and body. Never falls back."""
    lookup_fn = lookup_fn or lookup_ens101_projection
    try:
        payload = lookup_fn(email)
    except ReadinessUnavailable as error:
        print(f"[Student Readiness] {type(error).__name__}")
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "status": "temporarily_unavailable",
            "message": "Student Readiness is temporarily unavailable.",
        }
    except ReadinessContractError:
        print("[Student Readiness] ReadinessContractError")
        return HTTPStatus.BAD_GATEWAY, {
            "status": "incompatible_schema",
            "message": "Student Readiness returned an unsupported data version.",
        }
    except ReadinessRequestError:
        return HTTPStatus.BAD_REQUEST, {
            "status": "invalid_request",
            "message": "Student Readiness rejected this lookup.",
        }
    if payload["record_status"] == "not_found":
        return HTTPStatus.NOT_FOUND, payload
    return HTTPStatus.OK, payload

# ==============================================================================
# 2. LOCAL FEEDBACK & SUGGESTIONS DATABASE (SQLITE) & ADMIN CREDENTIALS
# ==============================================================================

def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "ENS 101 Mentor Desk"


class AdminCredential:
    """Owner-readable local admin password with no secret in source control."""

    MIN_PASSWORD_CHARACTERS = 8
    MAX_PASSWORD_CHARACTERS = 128

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._ensure_exists()

    def _ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if not self.path.exists():
            env_pwd = os.environ.get("ENS101_ADMIN_PASSWORD", "").strip()
            password = env_pwd if len(env_pwd) >= self.MIN_PASSWORD_CHARACTERS else secrets.token_urlsafe(24)
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(password + "\n")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        if len(self._read()) < self.MIN_PASSWORD_CHARACTERS:
            raise RuntimeError("The ENS 101 Mentor Desk v1.0 admin credential is invalid.")

    def _read(self) -> str:
        file_stat = self.path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError("The ENS 101 Mentor Desk v1.0 admin credential is invalid.")
        return self.path.read_text(encoding="utf-8").rstrip("\n")

    @classmethod
    def validate_new_password(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("New password must be text.")
        if value != value.strip():
            raise ValueError("New password cannot begin or end with whitespace.")
        if not (cls.MIN_PASSWORD_CHARACTERS <= len(value) <= cls.MAX_PASSWORD_CHARACTERS):
            raise ValueError(f"New password must be {cls.MIN_PASSWORD_CHARACTERS} to {cls.MAX_PASSWORD_CHARACTERS} characters.")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("New password cannot contain control characters.")
        return value

    def _write_atomic(self, password: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".admin-password-",
            dir=self.path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(password + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def verify(self, supplied: str) -> bool:
        if not isinstance(supplied, str) or len(supplied) > 256:
            return False
        with self._lock:
            return secrets.compare_digest(self._read(), supplied)

    def change(self, current_password: str, new_password: str) -> None:
        validated = self.validate_new_password(new_password)
        with self._lock:
            current = self._read()
            if not secrets.compare_digest(current, current_password):
                raise PermissionError("Current admin password was not accepted.")
            if secrets.compare_digest(current, validated):
                raise ValueError("New password must be different from current password.")
            self._write_atomic(validated)

    def replace_for_local_recovery(self, new_password: str) -> None:
        validated = self.validate_new_password(new_password)
        with self._lock:
            if secrets.compare_digest(self._read(), validated):
                raise ValueError("New password must be different from current password.")
            self._write_atomic(validated)


ADMIN_PASSWORD_PATH = Path(
    os.environ.get(
        "ENS101_ADMIN_PASSWORD_FILE",
        str(default_data_dir() / "admin-password"),
    )
).expanduser()

ADMIN_CREDENTIAL = AdminCredential(ADMIN_PASSWORD_PATH)


def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    mode TEXT,
                    rating TEXT NOT NULL,
                    question TEXT,
                    answer TEXT,
                    comment TEXT,
                    client_ip TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    suggestion TEXT NOT NULL,
                    submitter TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_notes TEXT,
                    implemented_at TEXT,
                    client_ip TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS appointments (
                    id TEXT PRIMARY KEY,
                    student_email TEXT NOT NULL,
                    student_name TEXT,
                    program TEXT,
                    career TEXT,
                    confidence TEXT DEFAULT '5',
                    roadmap_status TEXT,
                    followup_track TEXT,
                    assessment_data TEXT,
                    prep_notes TEXT,
                    session_notes TEXT,
                    student_next TEXT,
                    mentor_follow TEXT,
                    checked_tasks TEXT,
                    current_task TEXT DEFAULT 'prepare',
                    current_step INTEGER DEFAULT 0,
                    civitas_recorded INTEGER DEFAULT 0,
                    ensign_connect_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ensign_connect_cache (
                    student_email TEXT PRIMARY KEY,
                    has_account INTEGER NOT NULL,
                    profile_url TEXT,
                    checked_at TEXT NOT NULL
                )
            """)
            # Safe migration for existing DB without dropping table
            cursor = conn.execute("PRAGMA table_info(appointments)")
            cols = [row[1] for row in cursor.fetchall()]
            if "ensign_connect_status" not in cols:
                conn.execute("ALTER TABLE appointments ADD COLUMN ensign_connect_status TEXT")
            conn.commit()
    except Exception as e:
        print(f"[DB Init Error] {e}")

init_db()


def save_feedback(response_id: str, rating: str, comment: str = "", question: str = "", answer: str = "", mode: str = "", client_ip: str = ""):
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO feedback (created_at, response_id, mode, rating, question, answer, comment, client_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, response_id, mode, rating, question, answer, comment, client_ip))
        conn.commit()


def save_suggestion(category: str, suggestion: str, submitter: str = "", client_ip: str = "") -> int:
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO suggestions (created_at, category, suggestion, submitter, status, client_ip)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (now, category, suggestion, submitter, client_ip))
        conn.commit()
        return cursor.lastrowid


def get_suggestions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, created_at, category, suggestion, submitter, status, admin_notes, implemented_at
            FROM suggestions
            ORDER BY 
                CASE status
                    WHEN 'pending' THEN 1
                    WHEN 'in_progress' THEN 2
                    WHEN 'implemented' THEN 3
                    ELSE 4
                END,
                id DESC
        """)
        rows = [dict(r) for r in cursor.fetchall()]
        stats = {
            "total": len(rows),
            "pending": sum(1 for r in rows if r["status"] == "pending"),
            "in_progress": sum(1 for r in rows if r["status"] == "in_progress"),
            "implemented": sum(1 for r in rows if r["status"] == "implemented"),
            "dismissed": sum(1 for r in rows if r["status"] == "dismissed"),
        }
        return rows, stats


def update_suggestion_status(suggestion_id: int, status: str, admin_notes: str | None = None) -> bool:
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if status == "implemented":
            if admin_notes is not None:
                cursor.execute("""
                    UPDATE suggestions 
                    SET status = ?, admin_notes = ?, implemented_at = COALESCE(implemented_at, ?)
                    WHERE id = ?
                """, (status, admin_notes, now, suggestion_id))
            else:
                cursor.execute("""
                    UPDATE suggestions 
                    SET status = ?, implemented_at = COALESCE(implemented_at, ?)
                    WHERE id = ?
                """, (status, now, suggestion_id))
        else:
            if admin_notes is not None:
                cursor.execute("""
                    UPDATE suggestions 
                    SET status = ?, admin_notes = ?
                    WHERE id = ?
                """, (status, admin_notes, suggestion_id))
            else:
                cursor.execute("""
                    UPDATE suggestions 
                    SET status = ?
                    WHERE id = ?
                """, (status, suggestion_id))
        conn.commit()
        return cursor.rowcount > 0


def delete_suggestion(suggestion_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
        conn.commit()
        return cursor.rowcount > 0


# ==============================================================================
# 2B. APPOINTMENTS & PREPARATION PERSISTENCE (SQLITE)
# ==============================================================================

def get_all_appointments() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, student_email, student_name, program, career, confidence,
                   roadmap_status, followup_track, assessment_data, prep_notes,
                   current_task, current_step, civitas_recorded, ensign_connect_status,
                   created_at, updated_at
            FROM appointments
            ORDER BY updated_at DESC
        """)
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            if r.get("assessment_data"):
                try:
                    r["assessment_data"] = json.loads(r["assessment_data"])
                except Exception:
                    pass
            if r.get("ensign_connect_status"):
                try:
                    r["ensign_connect_status"] = json.loads(r["ensign_connect_status"])
                except Exception:
                    pass
        return rows


def get_appointment_by_id(appointment_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM appointments WHERE id = ? OR student_email = ?", (appointment_id, appointment_id.lower()))
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        if data.get("assessment_data"):
            try:
                data["assessment_data"] = json.loads(data["assessment_data"])
            except Exception:
                pass
        if data.get("ensign_connect_status"):
            try:
                data["ensign_connect_status"] = json.loads(data["ensign_connect_status"])
            except Exception:
                pass
        if data.get("checked_tasks"):
            try:
                data["checked_tasks"] = json.loads(data["checked_tasks"])
            except Exception:
                data["checked_tasks"] = {}
        return data


def save_appointment(data: dict) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    app_id = str(data.get("id") or "").strip()
    student_email = str(data.get("student_email") or "").strip().lower()
    if not app_id:
        email_prefix = re.sub(r'[^a-zA-Z0-9]', '-', (student_email or "student").split("@")[0])
        app_id = f"appt-{email_prefix}-{secrets.token_hex(4)}"

    student_name = str(data.get("student_name") or "").strip()
    program = str(data.get("program") or "").strip()
    career = str(data.get("career") or "").strip()
    confidence = str(data.get("confidence") or "5").strip()
    roadmap_status = str(data.get("roadmap_status") or "").strip()
    followup_track = str(data.get("followup_track") or "").strip()

    assessment_data = data.get("assessment_data")
    if isinstance(assessment_data, (dict, list)):
        assessment_data = json.dumps(assessment_data)
    elif not isinstance(assessment_data, str):
        assessment_data = ""

    ensign_connect_status = data.get("ensign_connect_status")
    if isinstance(ensign_connect_status, (dict, list)):
        ensign_connect_status = json.dumps(ensign_connect_status)
    elif not isinstance(ensign_connect_status, str):
        ensign_connect_status = ""

    prep_notes = str(data.get("prep_notes") or "")
    session_notes = str(data.get("session_notes") or "")
    student_next = str(data.get("student_next") or "")
    mentor_follow = str(data.get("mentor_follow") or "")

    checked_tasks = data.get("checked_tasks")
    if isinstance(checked_tasks, (dict, list)):
        checked_tasks = json.dumps(checked_tasks)
    elif not isinstance(checked_tasks, str):
        checked_tasks = "{}"

    current_task = str(data.get("current_task") or "prepare")
    current_step = int(data.get("current_step") or 0)
    civitas_recorded = 1 if data.get("civitas_recorded") else 0

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, created_at FROM appointments WHERE id = ?", (app_id,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE appointments SET
                    student_email = ?, student_name = ?, program = ?, career = ?,
                    confidence = ?, roadmap_status = ?, followup_track = ?,
                    assessment_data = ?, ensign_connect_status = ?, prep_notes = ?,
                    session_notes = ?, student_next = ?, mentor_follow = ?,
                    checked_tasks = ?, current_task = ?, current_step = ?,
                    civitas_recorded = ?, updated_at = ?
                WHERE id = ?
            """, (
                student_email, student_name, program, career,
                confidence, roadmap_status, followup_track,
                assessment_data, ensign_connect_status, prep_notes,
                session_notes, student_next, mentor_follow,
                checked_tasks, current_task, current_step,
                civitas_recorded, now, app_id
            ))
        else:
            cursor.execute("""
                INSERT INTO appointments (
                    id, student_email, student_name, program, career,
                    confidence, roadmap_status, followup_track,
                    assessment_data, ensign_connect_status, prep_notes,
                    session_notes, student_next, mentor_follow, checked_tasks,
                    current_task, current_step, civitas_recorded,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                app_id, student_email, student_name, program, career,
                confidence, roadmap_status, followup_track,
                assessment_data, ensign_connect_status, prep_notes,
                session_notes, student_next, mentor_follow, checked_tasks,
                current_task, current_step, civitas_recorded,
                now, now
            ))
        conn.commit()

    return get_appointment_by_id(app_id)


def confirm_civitas_recorded(app_id: str) -> bool:
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE appointments
            SET civitas_recorded = 1, updated_at = ?
            WHERE id = ?
        """, (now, app_id))
        conn.commit()
        return cursor.rowcount > 0


def delete_appointment(app_id: str, force: bool = False) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if not force:
            cursor.execute("SELECT civitas_recorded FROM appointments WHERE id = ?", (app_id,))
            row = cursor.fetchone()
            if not row or not row[0]:
                return False
        cursor.execute("DELETE FROM appointments WHERE id = ?", (app_id,))
        conn.commit()
        return cursor.rowcount > 0


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extracts text from PDF bytes using PyMuPDF (fitz) if available, falling back to regex parsing."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text_pages = [page.get_text() for page in doc]
        doc.close()
        full_text = "\n".join(text_pages).strip()
        if full_text:
            return full_text
    except Exception:
        pass

    try:
        raw = pdf_bytes.decode("latin1", errors="ignore")
        strings = re.findall(r"\((.*?)\)Tj", raw)
        if strings:
            return " ".join(strings)
    except Exception:
        pass
    return ""


def parse_pathwayu_text(text: str) -> dict:
    """Extracts student name, Holland code, interests, values, and assessment status."""
    return parse_career_explorer_pdf(text, track_completion=True)


def generate_career_guidance(student_name: str, program: str, career: str, assessment_data: dict | None = None) -> dict:
    """Generates tailored guidance from Career Explorer assessment results and student context."""
    return generate_guidance_sections(
        student_name,
        program,
        career,
        assessment_data,
        profile=PROFILE_MENTOR_ENS101,
    )


# ==============================================================================
# 3. COACH SYSTEM PROMPT & PERSONA
# ==============================================================================
# Customize this prompt for your agent's specific focus and purpose!

SYSTEM_PROMPT = """You are Mentor AI Help, a concise support tool for Ensign College career mentors conducting ENS 101 Appointment 1a: Ensign Connect and Internship Plan.
Your user is the mentor, not the student. Help the mentor complete the appointment guide accurately, explain Ensign Connect and internship preparation, choose the appropriate career follow-up, summarize non-sensitive notes, and draft warm follow-up messages.

ENS 101 (College Success) helps students understand Ensign College's mission and Honor Code, become effective stewards of their education, learn Ensign's instructional approach, and identify academic, career, health, financial, and other college resources.

Guidelines:
1. Be warm, practical, and brief. Prefer 2-4 useful bullets or one short draft.
2. Strengthen the mentor's judgment; do not pretend to know private student records or replace college staff.
3. When suggesting a referral, explain why it fits and give one clear first action.
4. Ask at most one focused follow-up question.
5. Do not diagnose, investigate, or invite sensitive details. If safety, health, financial, legal, or crisis concerns appear, advise the mentor to follow Ensign College policy and contact the appropriate professional or supervisor.
6. Never request student IDs, passwords, financial account information, health details, immigration documents, or other protected information.
7. Preserve the mentor's authentic, encouraging voice in drafts.
8. Page 1 of Appointment 1a includes: joining Ensign Connect and a major group; reviewing notification preferences, alumni, and informational interviews; explaining internship planning and early CAR 201 preparation; asking for the student's major, career direction, and 1-10 confidence; checking the Career Explorer roadmap; selecting a Career Explorer or Create Resume follow-up; confirming Roadmap 2 through Step 5; and taking the appointment selfie.
9. Treat internship-course details and international-student work rules as items to verify against current Ensign policy. Never present immigration guidance as a definitive personal determination.
10. INTERNSHIP DIRECTIVE: If the mentor asks ANY question regarding internships, internship requirements, finding or applying for an internship, internship courses (CAR 201, CAR 398, CAR 399, CAR 499), PBWE, practical training, or CPT, DO NOT answer the question in ENS 101 Mentor Desk v1.0. Instead, direct them to the Ensign Internship Expert app with the markdown link: [Ensign Internship Expert](/internship/).
"""

MODE_CONTEXTS = {
    "begin": "Appointment 1a stage: Begin. Help the mentor open warmly and ask about the student's major and intended career.",
    "ensign-connect": "Appointment 1a stage: Ensign Connect. Guide the mentor through joining, major groups, preferences, alumni discovery, and informational interviews.",
    "internship": "Appointment 1a stage: Internship Plan. Explain general preparation, course pairing, timelines, and which details require current-policy verification.",
    "career-direction": "Appointment 1a stage: Career Direction. Use confidence and Career Explorer progress to choose a Career Explorer or Create Resume follow-up.",
    "complete": "Appointment 1a stage: Complete. Confirm the student and mentor actions, then finish the page 1 checklist with the appointment selfie.",
}

# ==============================================================================

# ==============================================================================
# CAREER EXPLORER MENTOR COACH AI ENGINE (Shared with mentor-career-explorer-coach-ai)
# ==============================================================================

KNOWLEDGE_DIR = ROOT_DIR / "knowledge"

def load_file_text(path: Path) -> str:
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""

DOC1_TEXT = load_file_text(KNOWLEDGE_DIR / "01_Ensign_College_Degrees_and_Certificates_Official_Reference.txt")
DOC2_TEXT = load_file_text(KNOWLEDGE_DIR / "02_Career_Explorer_Ensign_Program_Mapping_Guide.txt")
DOC3_TEXT = load_file_text(KNOWLEDGE_DIR / "03_Career_as_Calling_Approved_McMullin_Excerpts.txt")
DOC4_TEXT = load_file_text(KNOWLEDGE_DIR / "04_Using_CliftonStrengths_to_Refine_Career_Direction.txt")
DOC5_TEXT = load_file_text(KNOWLEDGE_DIR / "05_How_to_Download_Your_PathwayU_Results.txt")
DOC6_TEXT = load_file_text(KNOWLEDGE_DIR / "06_design_thinking_first_career.md")
DOC7_TEXT = load_file_text(KNOWLEDGE_DIR / "07_maria_chooses_career_and_major_transcript.txt")

CAREER_SERVICES_URL = "https://www.ensign.edu/career-services"
ENSIGN_CONNECT_URL = "https://connect.byu.edu/hub/ces"
ROADMAP_URL = "https://connect.byu.edu/hub/ces/pathways/module-1-know-your-career-interests-and-options-beta-version-copy-ZWDJnjQ1Dm/steps/0"
PATHWAYU_URL = "https://ensign.pathwayu.com"
DOWNLOAD_EXAMPLE_URL = "https://lds-business-college.brightspotcdn.com/f9/b3/4318ea354586a9a04ba3b8cff319/career-explorer-results.pdf"
MARIA_VIDEO_URL = "https://www.youtube.com/watch?v=WzWFwJpoLUE"
MCMULLIN_VIDEO_URL = "https://www.youtube.com/watch?v=Rwdv2V0lOIM"

FOOTER_TEXT = f"""Ensign College Career Services • 10th Floor
[Career Services Help]({CAREER_SERVICES_URL}) • [Career & Major Exploration Roadmap]({ROADMAP_URL})"""

HEXACO_TERMINOLOGY_PROMPT = hexaco_terminology_prompt(PROFILE_MENTOR_ENS101)

CAREER_EXPLORER_SYSTEM_PROMPT = f"""Mentor Career Explorer System Prompt

System Identity & Target Audience:
You are Mentor Career Explorer Coach, an AI mentoring assistant created exclusively for Ensign College Career Mentors (senior retired church-service and community volunteers).
The target audience of ALL your generated text is ALWAYS the Career Mentor—NEVER the student directly.
The purpose of this app is to assist the Career Mentor in providing excellent, inspiring, and faith-guided career coaching to Ensign College students.

Audience Rules (CRITICAL):
1. The user chatting with you IS the Career Mentor. Never address the user as a student.
2. NEVER use phrases like "Schedule an appointment with your Career Mentor," "Talk to a Career Mentor," "Make an appointment with a mentor," or "Visit your Career Mentor on the 10th floor." The user IS the Career Mentor!
3. Frame all assessments, insights, and recommendations as coaching briefings for the mentor:
   - "Student Assessment Synthesis" (clear breakdown of the student's metrics)
   - "Key Coaching Insights for Your Session with the Student"
   - "Thoughtful Probing Questions You Can Ask the Student"
   - "Action Milestones You Can Suggest the Student Take This Week"
4. Understand your users: Career Mentors are senior retired volunteers devoting their time, wisdom, and life experience to bless Ensign College students. Provide clear, structured, high-yield coaching points that are immediately useful in live 1-on-1 student appointments.

Ensign College Mission & Mentoring Vision:
The school's mission is "to develop capable and trusted disciples of Jesus Christ who are leaders in their homes, the Church, and their communities."

Mission Integration in Mentor Advising:
Assist mentors in coaching students with this focused mission at the center:
1. Core Mission Alignment: Guide mentors to help students see that choosing a major and career is part of developing as capable and trusted disciples of Jesus Christ who lead in their homes, the Church, and their communities.
2. Professional Capabilities & Financial Success as Enabling Factors: Equip mentors to connect the idea that developing high-level professional capabilities and achieving financial success are vital enabling factors that expand the resources, stability, and opportunities students will have to serve as leaders in "their homes, the Church, and their communities." Financial stability and professional competence are consecrated assets that empower students to provide for family, serve in the Church, and lead in their communities.
3. Prompting Questions for Mentors: Equip mentors with thoughtful probing questions that ask students: "How will developing professional capabilities and achieving financial success in this field expand your resources and opportunities to lead in your home, serve in the Church, and bless your community?"
4. Spiritual Discernment & Career Design: Guide mentors to encourage students to pair assessment metrics and career planning with prayer and their patriarchal blessing.

Mentor Coaching Objectives:
1. Executive Assessment Synthesis: Provide concise, high-yield summaries of student results across all four assessments:
   - Personality Profile (HEXACO - Emotionality, Extraversion, Openness to Experience, Conscientiousness, Agreeableness, Honesty-Humility)
   - Workplace Environment Preferences
   - Top Core Values
   - Holland Code & Interests (RIASEC)
2. Academic & Career Alignment: Connect the student's Holland Code and values directly to Ensign College certificates, AAS, and BAS degrees using Doc 1 and Doc 2.
3. 1-on-1 Mentoring Strategy: Suggest 3-4 probing questions the mentor can ask to help the student reflect on their personal mission, prayer, and patriarchal blessing (Doc 3 & Doc 7).
4. Life & Career Design: Guide mentors in applying Design Thinking prototypes (conversations, experiences, 3 Odyssey Plans) from Doc 6.

Naming Convention:
Always refer to the assessment platform and results as "Career Explorer" (never as "PathwayU" or "Career Explorer by PathwayU").

Hyperlink Formatting:
NEVER output bare raw URLs in prose. ALWAYS format links as descriptive words that are hyperlinks using Markdown:
- [Career Services Help]({CAREER_SERVICES_URL})
- [Career Explorer Assessment]({PATHWAYU_URL})
- [Career & Major Exploration Roadmap]({ROADMAP_URL})
- [Download Example PDF]({DOWNLOAD_EXAMPLE_URL})
- [Maria Chooses a Career and Major]({MARIA_VIDEO_URL})
- [Keith B. McMullin: Career as Calling Address]({MCMULLIN_VIDEO_URL})
Do NOT output links telling the mentor to book an appointment with a mentor. Do NOT invent or output links for "How to Download Career Explorer Results". Give the exact UI steps in plain text instead.

Language: Respond in English only.
Tone: Professional, supportive, concise, and faith-aware.
Style: No emojis. Never use emojis in your responses.

Knowledge Documents (Your only source of truth):
- Doc 1: Ensign College Degrees and Certificates — Official program names and credential types.
- Doc 2: Career -> Ensign Program Mapping Guide — Career cluster alignment.
- Doc 3: McMullin Excerpts — Approved faith language only.
- Doc 4: CliftonStrengths Guidance — Use only after student shares their themes.
- Doc 5: How to Download Career Explorer Results — Exact steps for downloading results from the Assessments page (locate the DOWNLOAD button next to PRINT). Never format this as a link.
- Doc 6: Design Thinking & Gospel Principles for "What do I want to do for my first career?".
- Doc 7: Maria Chooses a Career and Major Video Story — Case study of Ensign College student Maria learning her major and career path via family counsel, Career Mentors, Brother Taylor, prayer, patriarchal blessing, and Career Explorer.
- Doc 8: Frequently Asked Questions (FAQ) — If a student or mentor asks "I can't access the Career & Major Exploration Roadmap / Ensign Connect", instruct them to send an email to careerservices@ensign.edu or call 801-524-1925.

Constraints:
1. Never address the user as a student. The user is the Career Mentor.
2. Never tell the mentor to schedule an appointment with a career mentor.
3. Never reference an academic program not in Doc 1.
4. Never use Doc 3 language outside Faith Integration contexts.
5. Never invent program names, Holland Codes, scores, traits, or career matches.
6. Role Boundaries:
   - Career Mentors: Assessments, career exploration, and how careers relate to majors.
   - Academic Advisors: Schedules, major changes, and enrollment decisions.
   This assistant does NOT approve majors, create schedules, recommend courses, or provide academic advising. Redirect those questions to Academic Advisors.
7. Program Guidance:
   - Frame programs as preparation, not promises. Never imply employment is guaranteed.
   - Present recommendations as options. Students may pursue paths at Ensign College or elsewhere.
   - Use exact program names from Doc 1. Distinguish between certificates, AAS, and BAS.
   - Use "Direct Preparation" or "Foundational Preparation" labels per Doc 2.
   - State clearly when Ensign does not offer a relevant program.
8. {STRENGTHS_FRAMING_PROMPT}

{HEXACO_TERMINOLOGY_PROMPT}

Official Degree List from Doc 1:
- Accounting Certificate, Accounting AAS, Accounting BAS, Finance BAS
- Business Management BAS, Project Management Certificate, Project Management AAS, Professional Sales Certificate, Supply Chain Management Certificate
- Marketing BAS, Communication AAS, Communication BAS, Social Media Marketing Certificate
- Information Technology AAS, Information Technology BAS, Technical Support Engineer Certificate, Software Engineering Certificate, Cybersecurity Certificate
- Hospitality and Tourism Management Certificate
"""

CAREER_EXPLORER_MODE_CONTEXTS = {
    "step1": f"""Mode: Step 1 (Roadmap & Assessment Guidance for Mentors).
Goal: Assist the Career Mentor in guiding a student through the beginning of their career exploration.
Provide key coaching points and advice the mentor can share with the student:
1. Introducing the Roadmap: Guide the mentor on directing the student to the [Career & Major Exploration Roadmap]({ROADMAP_URL}) on Ensign Connect.
2. Mission Alignment: Share how developing strong professional capabilities and achieving financial success are enabling factors that expand the student's resources and opportunities to serve as leaders in their homes, the Church, and their communities.
3. Video Case Study: Recommend the mentor reference the video [Maria Chooses a Career and Major]({MARIA_VIDEO_URL}) to illustrate how prayer, family counsel, mentoring, and Career Explorer help students find clarity.
4. Taking Assessments: Give the mentor the exact steps to explain to the student: take all 4 assessments on [Career Explorer Assessment]({PATHWAYU_URL}) (Interests, Values, Personality, Workplace Preferences), click the DOWNLOAD button next to PRINT on the Assessments page to save their official PDF, and bring it to their mentoring session.
5. If the student cannot access Ensign Connect or the Roadmap: Send an email to careerservices@ensign.edu or call 801-524-1925.""",

    "step2": """Mode: Step 2 (Assessment Report Synthesis for Mentors).
Goal: Provide the Career Mentor with an executive assessment synthesis of the student's Career Explorer results to prepare for their 1-on-1 coaching session.
Base analysis ONLY on the uploaded or provided assessment data. If information is missing, say: 'That information was not visible in the uploaded report.'
Use the headings 'Executive Summary' and 'Career & Major Recommendations'. Provide the summary first, then 3 career exploration options with aligned Ensign programs and evidence from the supplied assessment.
Required topics for the mentor:
1. Personality Profile (HEXACO): Summarize scores using the mandatory Emotional Sensitivity label with 2-3 coaching takeaways for the mentor.
2. Workplace Preferences: Highlight top organizational preferences and how the mentor can help the student evaluate work environments.
3. Core Values: Top 3 only; one sentence per value explaining what motivates the student.
4. Interests & Holland Code: State the RIASEC code and explain aligned work environments with 2-3 related O*NET occupations.
5. Probing Questions for the Mentor: Provide 3 thoughtful questions the mentor can ask the student during their session to foster reflection.""",

    "step3": """Mode: Step 3 (Majors & Careers Alignment for Mentors).
Goal: Provide the Career Mentor with 3 aligned career recommendations and Ensign College programs based on the student's assessment profile.
For each career provide:
- Career name (Aligned Ensign program in parentheses; exact names from Doc 1 only).
- Rationale connecting fit to the student's Holland Code, values, or personality.
- One concrete action step the mentor can encourage the student to take this week.
Rules: Use exact program names from Doc 1 only. Use Doc 2 alignment guidance (Direct Preparation vs Foundational Preparation). Connect certificates to AAS and BAS degree stacking.""",

    "step4": f"""Mode: Step 4 (Life Design, Calling & Mentoring Action Plan).
Goal: Assist the Career Mentor in guiding the student through Design Thinking and Keith B. McMullin's teachings on 'Career as Calling'.
1. Design Thinking:
   - Help the mentor reassure the student that choosing a career is not a permanent forever decision, but designing their first career through intentional prototyping.
   - Provide 3 Odyssey Plan prompts the mentor can explore with the student (Plan 1: Direct path, Plan 2: Alternative pivot, Plan 3: Service/wildcard).
   - Suggest conversation prototypes (informational interviews on Ensign Connect) and experience prototypes (intro courses, internships).
2. Gospel Principles & Mission Integration (Doc 3 Keith B. McMullin):
   - Anchor in developing capable and trusted disciples of Jesus Christ who lead in their homes, the Church, and their communities.
   - Teach mentors to help students see professional capabilities and financial success as consecrated enabling factors.
   - Suggest the mentor encourage the student to review their patriarchal blessing for references to talents, education, and service.
   - Provide the direct video link for Keith B. McMullin's keynote address: [▶ Watch Keith B. McMullin: Career as Calling Address (Video)]({MCMULLIN_VIDEO_URL}).
3. Action Milestones: Give the mentor 2-3 concrete milestones to establish with the student for their next check-in."""
}

CAREER_EXPLORER_MODE_CONTEXTS["prep-assessment"] = CAREER_EXPLORER_MODE_CONTEXTS["step1"]
CAREER_EXPLORER_MODE_CONTEXTS["prep-guidance"] = CAREER_EXPLORER_MODE_CONTEXTS["step2"]
CAREER_EXPLORER_MODE_CONTEXTS["prep-notes"] = CAREER_EXPLORER_MODE_CONTEXTS["step3"]

def clean_no_emojis(text: str) -> str:
    """Removes emoji characters and scrubs student-facing mentor referral phrases."""
    emoji_pattern = re.compile("[\U00010000-\U0010ffff]", flags=re.UNICODE)
    cleaned = emoji_pattern.sub("", text)
    cleaned = re.sub(
        r'\[([^\]]+)\]\(\[?[^\]]*\]?\((https?://[^\s\)]+)\)\)',
        r'[\1](\2)',
        cleaned
    )
    cleaned = re.sub(r'https?://[^\s\)]+/hub/ces/appointments\S*', CAREER_SERVICES_URL, cleaned)
    cleaned = re.sub(r'https?://calendly\.com/\S+', CAREER_SERVICES_URL, cleaned)
    return cleaned

def offline_career_explorer_python_engine(message: str, mode: str, history: list[dict[str, str]], parsed_data: dict | None = None) -> str:
    """Offline Python Engine providing high-fidelity coaching briefings based on student assessment data."""
    msg_lower = message.lower()

    if mode in ("step1", "prep-assessment"):
        if any(w in msg_lower for w in ["video", "maria", "story", "watch"]):
            return f"""**Coaching Guidance for Career Mentors:**
Use Maria's journey as a relatable case study when coaching students who feel overwhelmed or uncertain about their major. Guide your student through these steps:
1. Recommend they watch the video [Maria Chooses a Career and Major]({MARIA_VIDEO_URL}) prior to your coaching conversation.
2. Direct them to complete all 4 assessments on [Career Explorer Assessment]({PATHWAYU_URL}) (Interests, Values, Personality, Workplace Preferences).
3. Instruct them to download their official results PDF using the **DOWNLOAD** button next to **PRINT** at the top of the Assessments page.
4. Have them bring the PDF to your session so you can review their executive synthesis together.

{FOOTER_TEXT}"""

        if any(w in msg_lower for w in ["download", "pdf", "button"]):
            return f"""**Guiding Your Student to Download Their Official Career Explorer PDF:**

Provide your student with these instructions:
1. Log in to [Career Explorer Assessment]({PATHWAYU_URL}) and go to their Assessments page.
2. Verify that all 4 assessment sections (Interests, Values, Personality, Workplace Preferences) show completed checkmarks.
3. At the top of the Assessments page, right next to the **PRINT** button, click the **DOWNLOAD** button to save their official PDF report.
4. Have the student bring or email their downloaded PDF to your coaching session.

{FOOTER_TEXT}"""

        return f"""Welcome to the Mentor Career Explorer Coach. This tool assists you—an Ensign College Career Mentor—in providing high-impact, faith-guided career and major coaching to students.

Guided by the mission of Ensign College, we help mentors develop students into capable and trusted disciples of Jesus Christ who lead in their homes, the Church, and their communities. We frame professional capabilities and financial success as vital enabling factors that expand the student's resources and opportunities to serve.

**Next Steps for Your Session:**
1. Look up your student above, or upload their downloaded Career Explorer PDF.
2. Review the generated executive synthesis, probing questions, and stacked degree pathways before or during your 1-on-1 coaching session.

{FOOTER_TEXT}"""

    if mode in ("step2", "prep-guidance"):
        return build_briefing(parsed_data, profile=PROFILE_MENTOR_ENS101, pathwayu_url=PATHWAYU_URL, footer_text=FOOTER_TEXT)

    if mode in ("step3", "prep-notes"):
        return build_career_recommendations(parsed_data, profile=PROFILE_MENTOR_ENS101, footer_text=FOOTER_TEXT)

    if mode in ("step4",):
        return f"""### Life Design & Calling: Mentoring Guide for Career Mentors

Guide your student through answering: *"What do I want to do for my first career?"*

#### 1. Design Thinking: Designing the First Career
- **Reframe the Question:** Reassure the student that choosing a career is not a permanent, lifelong verdict; it is about designing their *first career*.
- **The Three Odyssey Plans:** Guide the student to sketch 3 distinct 5-year paths:
  1. *Plan 1 (Direct Path):* Pursuing their primary interest (e.g., Communication BAS or Business Management BAS).
  2. *Plan 2 (Alternative Pivot):* What they would do if Plan 1 were not an option.
  3. *Plan 3 (Wildcard / Mission-Driven):* What they would pursue if money or image were no object.
- **Prototyping:** Encourage low-stakes conversation prototypes (15-minute informational interviews with alumni on Ensign Connect) and experience prototypes (introductory coursework, job shadowing).

#### 2. Gospel Principles & The Ensign College Mission (Keith B. McMullin)
- **Anchor in Mission:** Ensign College exists to develop capable and trusted disciples of Jesus Christ who are leaders in their homes, the Church, and their communities.
- **Career as Calling:** Share Elder Keith B. McMullin's teaching: *"Decide that your career is your mission, not your job. Your mission comes first. Your job comes along and everything works out all right."*
- **Keynote Address Video:** [▶ Watch Keith B. McMullin: Career as Calling Address (Video)]({MCMULLIN_VIDEO_URL})
- **Capabilities and Financial Success as Enabling Factors:** Help the student understand that professional capabilities and financial success are not worldly ends, but consecrated enabling factors that expand the resources, stability, and freedom they have to serve others, provide for their families, and build the Kingdom.
- **Patriarchal Blessing:** Suggest the student prayerfully review their patriarchal blessing for references to talents, gifts, education, or avenues of service.

#### 3. Action Milestones for Your Next Coaching Session
- [ ] Student conducts 1 informational interview on Ensign Connect.
- [ ] Student completes drafts of their 3 Odyssey Plans.
- [ ] Student schedules a follow-up coaching session with you to review their learnings.

{FOOTER_TEXT}"""

    return f"""Welcome to the Mentor Career Explorer Coach. I assist Ensign College Career Mentors in interpreting student Career Explorer assessments, mapping degrees, and applying design thinking principles.

How can I assist you with your student coaching session today?

{FOOTER_TEXT}"""


# 4. PRIVACY FILTER & GUARDRAILS
# ==============================================================================

PII_PATTERNS = [
    # Social Security Numbers (e.g., 000-00-0000 or 000 00 0000)
    (re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b"), "Social Security numbers"),
    # Credit Card Numbers (13 to 19 digits with dashes or spaces)
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{1,4}\b"), "payment card numbers"),
    # Password disclosures
    (re.compile(r"\b(?:password|pwd|passcode|secret_key)\s*[:=]\s*\S+", re.IGNORECASE), "passwords"),
]

def check_privacy(text: str) -> str | None:
    """Returns a warning message if sensitive PII is detected, else None."""
    for pattern, label in PII_PATTERNS:
        if pattern.search(text):
            return f"For your privacy and safety, please remove {label} or private identifiers before submitting."
    return None

# ==============================================================================
# 5. SLIDING-WINDOW RATE LIMITER (DEFAULT: 50 REQ/MIN)
# ==============================================================================

class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int = 50):
        self.limit = limit_per_minute
        self.requests = defaultdict(list)

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        window_start = now - 60.0
        self.requests[client_ip] = [t for t in self.requests[client_ip] if t > window_start]
        if len(self.requests[client_ip]) >= self.limit:
            return False
        self.requests[client_ip].append(now)
        return True

RATE_LIMITER = SlidingWindowRateLimiter(limit_per_minute=RATE_LIMIT)

# ==============================================================================
# 6. GUIDANCE ENGINES (OPTIONAL COMPATIBLE API -> GEMINI -> OFFLINE)
# ==============================================================================

def extract_user_query(message: str) -> str:
    marker = "\n\nNon-sensitive appointment context:"
    if marker in message:
        return message.split(marker, 1)[0].strip()
    return message.strip()

def is_internship_question(message: str) -> bool:
    """Checks if the user's query asks questions about internships or CPT."""
    query = extract_user_query(message)
    pattern = r"\b(internships?|interns?|cpt|car\s*-?(?:201|398|399|499)|pbwe|practical training)\b"
    return bool(re.search(pattern, query, re.IGNORECASE))

def get_internship_expert_url(headers=None) -> str:
    """Returns the URL for Internship Expert based on host/headers."""
    if headers:
        host = headers.get("Host", "").lower()
        referer = headers.get("Referer", "").lower()
        if "tail299fc7.ts.net" in host or "tail299fc7.ts.net" in referer or "/ens101" in referer or "/mentor-desk" in referer:
            return "/internship/"
        if "127.0.0.1" in host or "localhost" in host:
            host_name = host.split(":")[0]
            return f"http://{host_name}:5035/"
    return "/internship/"

def fallback_reply(message: str, mode: str, headers=None) -> str:
    """Useful, deterministic guidance when no AI engine is configured."""
    if is_internship_question(message):
        url = get_internship_expert_url(headers)
        return (
            "For all questions regarding internships, degree requirements, course pairing, timelines, and CPT authorization, "
            f"please consult the [Ensign Internship Expert]({url}) app. "
            "ENS 101 Mentor Desk v1.0 does not answer internship questions directly—official internship policies and source-grounded answers are maintained in the Internship Expert."
        )
    lower = message.lower()
    if "follow-up" in lower or "message" in lower or "email" in lower:
        return (
            "Here is a concise draft:\n\n"
            "Hi! Thank you for meeting with me today. I appreciated hearing about what you are working toward. "
            "Your next step is [student action], and I will [mentor follow-up]. I’m cheering you on—please reach out if you need help finding the resource we discussed."
        )
    if "summar" in lower or "next step" in lower:
        return (
            "Use this three-part summary:\n"
            "• Focus: the main goal or barrier discussed\n"
            "• Student action: one specific step and intended time frame\n"
            "• Mentor follow-up: the resource, introduction, or check-in you agreed to provide"
        )
    if "refer" in lower or "resource" in lower or "where" in lower:
        return (
            "Match the need to one clear starting point: Academic Advisors for course or graduation planning; "
            "Student Success Coaches for habits, time management, and college-life barriers; Career Mentors or "
            "Handshake for résumés, interviews, internships, and career direction. Explain why the resource fits, then help the student open it."
        )
    fallbacks = {
        "begin": "Begin with the prayer direction in the guide, then ask: “What is your major?” and “What type of career do you see yourself doing when you graduate?”",
        "ensign-connect": "Open Ensign Connect, complete Join Now, join the student's major group, review notification preferences, and show how to explore alumni for informational interviews.",
        "internship": "Explain that internship planning starts early: connect the experience to the major, review the appropriate internship course, discuss recruiting timelines, and verify international-student rules with the appropriate office.",
        "career-direction": "Ask for career confidence from 1–10 and check Major & Career Exploration. If the student is still exploring, plan a Major & Career Exploration follow-up; if confident, consider a Create Resume appointment.",
        "complete": "Confirm the student action and mentor follow-up, then finish the page 1 checklist with the appointment selfie after obtaining consent.",
    }
    return fallbacks.get(mode, "Choose one open question, one useful resource, and one specific next step. What part would you like help drafting?")


def query_qwen(message: str, mode: str, history: list[dict[str, str]], parsed_data: dict | None = None) -> str | None:
    """Queries LM Studio Qwen via OpenAI-compatible /v1/chat/completions."""
    parsed_data = normalize_assessment_data(parsed_data)
    if not LM_STUDIO_URL:
        return None

    is_ce = mode in ("prep-assessment", "prep-guidance", "prep-notes", "step1", "step2", "step3", "step4") or bool(parsed_data)

    if is_ce:
        mode_context = CAREER_EXPLORER_MODE_CONTEXTS.get(mode, "")
        data_context = ""
        if parsed_data and any(parsed_data.get(k) for k in ["holland_code", "primary_interests", "primary_values", "personality", "personality_scores", "primary_workplace_preferences"]):
            data_context = assessment_prompt_context(parsed_data, PROFILE_MENTOR_ENS101)
            # Hub personality scores are raw 1–5 values, not normative high/low bands.
            source_details = {
                "status": parsed_data.get("status"),
                "completed_count": parsed_data.get("completed_count"),
                "missing": parsed_data.get("missing", []),
                "personality_scores": parsed_data.get("personality_scores"),
            }
            data_context += "\nImported assessment details:\n" + json.dumps(source_details)
            data_context += "\nDo not convert raw scores to normative bands. Identify missing assessments explicitly."
        system_content = f"{CAREER_EXPLORER_SYSTEM_PROMPT}\n\n{mode_context}".strip()
    else:
        mode_context = MODE_CONTEXTS.get(mode, "")
        system_content = f"{SYSTEM_PROMPT}\n\n{mode_context}".strip()

    messages = [{"role": "system", "content": system_content}]
    for item in history[-8:]:
        role = item.get("role", "user")
        content = str(item.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Assessment evidence belongs to this request, not to a previous conversation.
    user_content = f"{message}\n\nUse the supplied assessment results below to generate feedback now.{data_context}" if is_ce and data_context else message
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "temperature": 0.3 if is_ce else 0.0,
        "max_tokens": 1200 if is_ce else 1024,
    }

    url = f"{LM_STUDIO_URL}/chat/completions"
    req_data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    request = Request(url, data=req_data, headers=headers, method="POST")
    with urlopen(request, timeout=35, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    choices = data.get("choices", [])
    if choices:
        reply = choices[0].get("message", {}).get("content", "").strip()
        if reply:
            return clean_no_emojis(reply) if is_ce else reply
    return None
    mode_context = MODE_CONTEXTS.get(mode, "")
    system_content = f"{SYSTEM_PROMPT}\n\n{mode_context}".strip()

    messages = [{"role": "system", "content": system_content}]
    for item in history[-8:]:
        role = item.get("role", "user")
        content = str(item.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": message})

    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 1024,
    }

    url = f"{LM_STUDIO_URL}/chat/completions"
    req_data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    request = Request(url, data=req_data, headers=headers, method="POST")
    with urlopen(request, timeout=35, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip() or None
    return None


def query_gemini(message: str, mode: str, history: list[dict[str, str]]) -> str | None:
    """Queries Google Gemini GenerateContent API as fallback."""
    if not GEMINI_API_KEY:
        return None

    mode_context = MODE_CONTEXTS.get(mode, "")
    system_instruction = f"{SYSTEM_PROMPT}\n\n{mode_context}".strip()

    contents = []
    for item in history[-8:]:
        role = item.get("role")
        content = str(item.get("content", "")).strip()
        if role == "user" and content:
            contents.append({"role": "user", "parts": [{"text": content}]})
        elif role == "assistant" and content:
            contents.append({"role": "model", "parts": [{"text": content}]})

    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 2048,
        },
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    req_data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    request = Request(url, data=req_data, headers=headers, method="POST")
    with urlopen(request, timeout=30, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            return parts[0].get("text", "").strip() or None
    return None


def ask_coach(message: str, mode: str, history: list[dict[str, str]], headers=None, parsed_data: dict | None = None) -> tuple[str, bool, str]:
    """
    Guidance coordinator:
    1. Intercept internship questions and redirect to Internship Expert
    2. Try LM Studio Qwen (local or Tailscale)
    3. Fallback to offline Career Explorer python engine (for CE/Prep modes) or Gemini / appointment fallback
    Returns (reply_text, is_live_ai, engine_name).
    """
    if is_internship_question(message):
        url = get_internship_expert_url(headers)
        reply = (
            "For all questions regarding internships, degree requirements, course pairing, timelines, and CPT authorization, "
            f"please consult the [Ensign Internship Expert]({url}) app. "
            "ENS 101 Mentor Desk v1.0 does not answer internship questions directly—official internship policies and source-grounded answers are maintained in the Internship Expert."
        )
        return reply, True, "internship_redirect"

    is_ce = mode in ("prep-assessment", "prep-guidance", "prep-notes", "step1", "step2", "step3", "step4") or bool(parsed_data)

    # 1. Primary: LM Studio Qwen
    qwen_error: str | None = None
    if LM_STUDIO_URL:
        try:
            reply = query_qwen(message, mode, history, parsed_data)
            if reply:
                return reply, True, "qwen"
            qwen_error = "Qwen Local returned an empty response"
        except Exception as e:
            qwen_error = f"Qwen Local error: {e}"
            print(f"[Primary AI Unavailable] {e}")
    else:
        qwen_error = "LM Studio endpoint is not configured"

    # 2. Career Explorer offline python fallback (for CE modes or when assessment data provided)
    if is_ce:
        notify_qwen_fallback(
            service_name="ENS 101 Mentor Desk v1.0 (Career Explorer)",
            fallback_engine="Offline Career Explorer Python Engine",
            error_reason=qwen_error or "Qwen Local unavailable",
            prompt_snippet=message,
        )
        return offline_career_explorer_python_engine(message, mode, history, parsed_data), False, "offline_python_engine"

    # 3. Appointment 1a Workflow Fallback: Google Gemini
    if GEMINI_API_KEY:
        try:
            reply = query_gemini(message, mode, history)
            if reply:
                notify_qwen_fallback(
                    service_name="ENS 101 Mentor Desk v1.0",
                    fallback_engine="Google Gemini",
                    error_reason=qwen_error or "Qwen Local unavailable",
                    prompt_snippet=message,
                )
                return reply, True, "gemini"
        except Exception as e:
            print(f"[Fallback Gemini Error] {e}")

    # 4. Final Fallback: Offline Appointment Guidance
    notify_qwen_fallback(
        service_name="ENS 101 Mentor Desk v1.0",
        fallback_engine="Offline Python Engine",
        error_reason=qwen_error or "Qwen Local and Gemini unavailable",
        prompt_snippet=message,
    )
    return fallback_reply(message, mode, headers), False, "fallback"


# ==============================================================================
# 7. HTTP REQUEST HANDLER
# ==============================================================================

def is_lm_studio_online() -> bool:
    if not LM_STUDIO_URL:
        return False
    try:
        req = Request(f"{LM_STUDIO_URL}/models", headers={"User-Agent": "ens-101"})
        with urlopen(req, timeout=0.8) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_active_engine() -> str:
    if is_lm_studio_online():
        return "Qwen Local"
    if GEMINI_API_KEY:
        return "Google Gemini"
    return "Offline Guidance"


class CoachHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_HEAD(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ("/healthz", "/api/status"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if clean_path == "/api/career-explorer/download-report":
            self._handle_download_report(head_only=True)
            return
        if clean_path in ("/admin", "/admin/"):
            self.path = "/admin.html"
        super().do_HEAD()

    def _json(self, payload: dict, status: int = HTTPStatus.OK, head_only: bool = False):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json_retired(self):
        self._json({
            "status": "retired",
            "message": "This endpoint was retired. Use /api/student-readiness/lookup.",
        }, HTTPStatus.GONE)

    def _is_admin_authenticated(self) -> bool:
        auth_header = self.headers.get("X-ENS101-Admin", "").strip()
        if not auth_header:
            return False
        return ADMIN_CREDENTIAL.verify(auth_header)

    def _career_lookup_is_local(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path == "/favicon.ico":
            self.path = "/favicon.svg"
            super().do_GET()
            return
        if clean_path in RETIRED_SOURCE_ENDPOINTS:
            self._json_retired()
            return

        # Health & status endpoints
        if clean_path in ("/healthz", "/api/status"):
            active_eng = get_active_engine()
            self._json({
                "status": "ok",
                "service": "ENS 101 Mentor Desk v1.0",
                "active_engine": active_eng,
                "ai_configured": bool(LM_STUDIO_URL or GEMINI_API_KEY),
                "primary_engine": "LM Studio Qwen" if LM_STUDIO_URL else "Not configured",
                "primary_model": QWEN_MODEL,
                "primary_configured": bool(LM_STUDIO_URL),
                "fallback_engine": "Google Gemini" if GEMINI_API_KEY else "Static Fallback",
                "fallback_configured": bool(GEMINI_API_KEY),
                "readiness_source": "Student Readiness ens101.v1",
                "rate_limit_per_min": RATE_LIMIT,
            })
            return

        if clean_path in ("/admin", "/admin/"):
            self.path = "/admin.html"
            super().do_GET()
            return

        if clean_path == "/api/admin/suggestions":
            if not self._is_admin_authenticated():
                self._json({"error": "Unauthorized. Provide valid admin credentials in X-ENS101-Admin header."}, HTTPStatus.UNAUTHORIZED)
                return
            try:
                suggestions, stats = get_suggestions()
                self._json({"status": "ok", "stats": stats, "suggestions": suggestions})
            except Exception as e:
                print(f"[Admin Suggestions Error] {e}")
                self._json({"error": "Failed to retrieve suggestions."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if clean_path == "/api/appointments":
            try:
                appointments = get_all_appointments()
                self._json({"status": "ok", "appointments": appointments})
            except Exception as e:
                print(f"[Appointments GET error] {e}")
                self._json({"error": "Failed to fetch appointments."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if clean_path == "/api/appointments/get":
            query = parse_qs(urlparse(self.path).query)
            app_id = query.get("id", [""])[0].strip()
            if not app_id:
                self._json({"error": "Missing appointment id."}, HTTPStatus.BAD_REQUEST)
                return
            appointment = get_appointment_by_id(app_id)
            if not appointment:
                self._json({"error": "Appointment not found."}, HTTPStatus.NOT_FOUND)
                return
            self._json({"status": "ok", "appointment": appointment})
            return

        # Direct download endpoint for student assessment full reports (.PDF)
        if clean_path == "/api/career-explorer/download-report":
            self._handle_download_report(head_only=False)
            return

        super().do_GET()

    def _handle_download_report(self, head_only: bool = False):
        query = parse_qs(urlparse(self.path).query)
        target_filename = query.get("file", [""])[0].strip()
        student_email = query.get("email", [""])[0].strip()
        requested_name = query.get("name", [""])[0].strip()

        search_dirs = [
            ROOT_DIR / "downloads",
            ROOT_DIR.parent / "mentor-career-explorer-coach-ai" / "downloads",
            ROOT_DIR.parent / "AI AGENTS LOCAL LLM" / "shared" / "downloads",
        ]

        target_file = None
        if target_filename:
            safe_name = os.path.basename(target_filename)
            for sdir in search_dirs:
                if not sdir.exists():
                    continue
                candidate = (sdir / safe_name).resolve()
                if candidate.is_file() and candidate.name.lower().endswith(".pdf"):
                    target_file = candidate
                    break
                for f in sdir.glob("*.pdf"):
                    if f.is_file() and f.name.lower() == safe_name.lower():
                        target_file = f
                        break
                if target_file:
                    break

        if target_file is None and student_email:
            clean_email = re.sub(r"[^a-zA-Z0-9_.-]", "_", student_email.lower())
            for sdir in search_dirs:
                if not sdir.exists():
                    continue
                matching = sorted(
                    [f for f in sdir.glob(f"{clean_email}*.pdf") if f.is_file()],
                    key=lambda f: f.stat().st_mtime,
                    reverse=True
                )
                if matching:
                    target_file = matching[0]
                    break

        if target_file is None:
            candidates = []
            for sdir in search_dirs:
                if sdir.exists():
                    candidates.extend([f for f in sdir.glob("*.pdf") if f.is_file() and f.stat().st_size > 1000])
            if candidates:
                candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                target_file = candidates[0]

        if target_file is None or not target_file.exists():
            self._json({"error": "Student report PDF not found."}, HTTPStatus.NOT_FOUND, head_only=head_only)
            return

        try:
            with open(target_file, "rb") as f:
                pdf_bytes = f.read()

            if len(pdf_bytes) < 500 or not pdf_bytes.startswith(b"%PDF-"):
                self._json({"error": "File on disk is not a valid PDF document."}, HTTPStatus.INTERNAL_SERVER_ERROR, head_only=head_only)
                return

            safe_dl_name = re.sub(r"[^\w\-. ]", "_", requested_name) if requested_name else target_file.name
            if not safe_dl_name.lower().endswith(".pdf"):
                safe_dl_name += ".pdf"

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_dl_name}"')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.send_header("Cache-Control", "private, no-cache, no-store, must-revalidate")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if not head_only:
                self.wfile.write(pdf_bytes)
            return
        except Exception as exc:
            self._json({"error": f"Failed to download report PDF: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR, head_only=head_only)
            return

    def do_POST(self):
        # Keep routing consistent with GET: query parameters must not turn a
        # valid API endpoint into a static-file 404.
        clean_path = self.path.split("?")[0]

        if clean_path in RETIRED_SOURCE_ENDPOINTS:
            self._json_retired()
            return

        # ----------------------------------------------------------------------
        # Student Readiness lookup (Student Readiness Hub ens101.v1 only)
        # ----------------------------------------------------------------------
        if clean_path == "/api/student-readiness/lookup":
            if not self._career_lookup_is_local():
                self._json({
                    "status": "local_only",
                    "message": "Student Readiness lookup is available only on the mentor workstation.",
                }, HTTPStatus.FORBIDDEN)
                return
            client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            if not RATE_LIMITER.is_allowed(client_ip):
                self._json({
                    "status": "rate_limited",
                    "message": "Please wait a moment before checking another student.",
                }, HTTPStatus.TOO_MANY_REQUESTS)
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 8192:
                    raise ValueError("Invalid body size")
                data = json.loads(self.rfile.read(content_length).decode("utf-8"))
                email = str(data.get("email", "")).strip().lower()
            except Exception:
                email = ""
            if not ENSIGN_EMAIL_PATTERN.fullmatch(email):
                self._json({
                    "status": "invalid_email",
                    "message": "Enter the student's @ensign.edu email address.",
                }, HTTPStatus.BAD_REQUEST)
                return

            status, payload = build_student_readiness_response(email)
            self._json(payload, status)
            return

        # ----------------------------------------------------------------------
        # Appointments Endpoints (Multi-student persistence & Civitas lifecycle)
        # ----------------------------------------------------------------------
        if self.path == "/api/appointments/save":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            try:
                saved = save_appointment(data)
                self._json({"status": "ok", "appointment": saved})
            except Exception as e:
                print(f"[Save Appointment Error] {e}")
                self._json({"error": "Failed to save appointment."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if self.path == "/api/appointments/civitas-confirm":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            app_id = str(data.get("id", "")).strip()
            if not app_id:
                self._json({"error": "Missing appointment id."}, HTTPStatus.BAD_REQUEST)
                return

            success = confirm_civitas_recorded(app_id)
            if success:
                self._json({"status": "ok", "message": "Civitas recording confirmed."})
            else:
                self._json({"error": "Appointment not found."}, HTTPStatus.NOT_FOUND)
            return

        if self.path == "/api/appointments/delete":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            app_id = str(data.get("id", "")).strip()
            force = bool(data.get("force", False))
            if not app_id:
                self._json({"error": "Missing appointment id."}, HTTPStatus.BAD_REQUEST)
                return

            appt = get_appointment_by_id(app_id)
            if not appt:
                self._json({"error": "Appointment not found."}, HTTPStatus.NOT_FOUND)
                return

            if not force and not appt.get("civitas_recorded"):
                self._json({
                    "error": "Appointment cannot be deleted until recording in Civitas is confirmed.",
                    "civitas_recorded": False
                }, HTTPStatus.BAD_REQUEST)
                return

            deleted = delete_appointment(app_id, force=True)
            if deleted:
                self._json({"status": "ok", "message": "Appointment record deleted successfully."})
            else:
                self._json({"error": "Failed to delete appointment."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Career Explorer Guidance Endpoint
        # ----------------------------------------------------------------------
        if self.path == "/api/career-explorer/guidance":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            student_name = str(data.get("student_name", "")).strip()
            program = str(data.get("program", "")).strip()
            career = str(data.get("career", "")).strip()
            assessment_data = data.get("assessment_data") or {}

            guidance = generate_career_guidance(student_name, program, career, assessment_data)
            self._json(guidance)
            return

        # ----------------------------------------------------------------------
        # Career Explorer PDF Upload & Parse Endpoint
        # ----------------------------------------------------------------------
        if self.path == "/api/career-explorer/parse-pdf":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            pdf_base64 = data.get("pdf_base64", "")
            if not pdf_base64:
                self._json({"error": "Missing pdf_base64 data."}, HTTPStatus.BAD_REQUEST)
                return

            try:
                if "," in pdf_base64:
                    pdf_base64 = pdf_base64.split(",", 1)[1]
                pdf_bytes = base64.b64decode(pdf_base64)
                extracted_text = extract_text_from_pdf(pdf_bytes)
                parsed = parse_pathwayu_text(extracted_text)
                self._json({"status": "ok", "data": parsed, "extracted_length": len(extracted_text)})
            except Exception as e:
                print(f"[PDF Parse Error] {e}")
                self._json({"error": f"Failed to parse PDF report: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # Suggestion Submission Endpoint (Public, Rate-limited, Privacy-checked)
        # ----------------------------------------------------------------------
        if clean_path == "/api/suggestions":
            client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            if not RATE_LIMITER.is_allowed(client_ip):
                self._json(
                    {"error": f"Rate limit exceeded. Please wait a moment before submitting again."},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            category = str(data.get("category", "General")).strip()
            suggestion = str(data.get("suggestion", "")).strip()
            submitter = str(data.get("submitter", "")).strip()

            if not suggestion:
                self._json({"error": "Suggestion text is required."}, HTTPStatus.BAD_REQUEST)
                return

            warning = check_privacy(suggestion)
            if not warning and submitter:
                warning = check_privacy(submitter)
            if warning:
                self._json({"error": warning}, HTTPStatus.BAD_REQUEST)
                return

            try:
                new_id = save_suggestion(category, suggestion, submitter, client_ip)
                self._json({
                    "status": "ok",
                    "message": "Thank you! Your suggestion has been received for staff review.",
                    "id": new_id,
                })
            except Exception as e:
                print(f"[Suggestion Save Error] {e}")
                self._json({"error": "Failed to save suggestion."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Admin Authentication Verification Endpoint
        # ----------------------------------------------------------------------
        if clean_path == "/api/admin/verify":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            password = str(data.get("password", ""))
            if ADMIN_CREDENTIAL.verify(password):
                self._json({"status": "ok", "message": "Admin authenticated successfully."})
            else:
                self._json({"error": "Incorrect admin password."}, HTTPStatus.UNAUTHORIZED)
            return

        # ----------------------------------------------------------------------
        # Admin Update Suggestion Status & Notes Endpoint
        # ----------------------------------------------------------------------
        if clean_path == "/api/admin/suggestions/status":
            if not self._is_admin_authenticated():
                self._json({"error": "Unauthorized."}, HTTPStatus.UNAUTHORIZED)
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            suggestion_id = data.get("id")
            new_status = str(data.get("status", "")).strip().lower()
            admin_notes = data.get("admin_notes")
            if admin_notes is not None:
                admin_notes = str(admin_notes).strip()

            if not suggestion_id or new_status not in ("pending", "in_progress", "implemented", "dismissed"):
                self._json({"error": "Invalid id or status. Status must be pending, in_progress, implemented, or dismissed."}, HTTPStatus.BAD_REQUEST)
                return

            try:
                updated = update_suggestion_status(int(suggestion_id), new_status, admin_notes)
                if updated:
                    self._json({"status": "ok", "message": "Suggestion status updated successfully."})
                else:
                    self._json({"error": "Suggestion not found."}, HTTPStatus.NOT_FOUND)
            except Exception as e:
                print(f"[Suggestion Status Update Error] {e}")
                self._json({"error": "Failed to update suggestion status."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Admin Delete Suggestion Endpoint
        # ----------------------------------------------------------------------
        if clean_path == "/api/admin/suggestions/delete":
            if not self._is_admin_authenticated():
                self._json({"error": "Unauthorized."}, HTTPStatus.UNAUTHORIZED)
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            suggestion_id = data.get("id")
            if not suggestion_id:
                self._json({"error": "Missing suggestion id."}, HTTPStatus.BAD_REQUEST)
                return

            try:
                deleted = delete_suggestion(int(suggestion_id))
                if deleted:
                    self._json({"status": "ok", "message": "Suggestion deleted."})
                else:
                    self._json({"error": "Suggestion not found."}, HTTPStatus.NOT_FOUND)
            except Exception as e:
                print(f"[Suggestion Delete Error] {e}")
                self._json({"error": "Failed to delete suggestion."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Admin Password Change Endpoint
        # ----------------------------------------------------------------------
        if clean_path == "/api/admin/password":
            if not self._is_admin_authenticated():
                self._json({"error": "Unauthorized."}, HTTPStatus.UNAUTHORIZED)
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            current_password = str(data.get("current_password", ""))
            new_password = str(data.get("new_password", ""))

            try:
                ADMIN_CREDENTIAL.change(current_password, new_password)
                self._json({"status": "ok", "message": "Admin password changed successfully."})
            except (ValueError, PermissionError) as pe:
                self._json({"error": str(pe)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                print(f"[Admin Password Change Error] {e}")
                self._json({"error": "Failed to change admin password."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Feedback Submission Endpoint
        # ----------------------------------------------------------------------
        if clean_path == "/api/feedback":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(raw_body)
            except Exception:
                self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
                return

            response_id = str(data.get("response_id", "")).strip()
            rating = str(data.get("rating", "")).strip().lower()
            comment = str(data.get("comment", "")).strip()
            question = str(data.get("question", "")).strip()
            answer = str(data.get("answer", "")).strip()
            mode = str(data.get("mode", "")).strip()

            if rating not in ("up", "down"):
                self._json({"error": "Rating must be 'up' or 'down'."}, HTTPStatus.BAD_REQUEST)
                return

            if comment:
                warning = check_privacy(comment)
                if warning:
                    self._json({"error": warning}, HTTPStatus.BAD_REQUEST)
                    return

            client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            try:
                save_feedback(response_id, rating, comment, question, answer, mode, client_ip)
                self._json({"status": "ok", "message": "Feedback saved."})
            except Exception as e:
                print(f"[Feedback Save Error] {e}")
                self._json({"error": "Failed to save feedback."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ----------------------------------------------------------------------
        # Chat Generation Endpoint
        # ----------------------------------------------------------------------
        if self.path != "/api/chat":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        # 1. Rate Limiting Check
        client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        if not RATE_LIMITER.is_allowed(client_ip):
            self._json(
                {"error": f"Rate limit of {RATE_LIMIT} req/min exceeded. Please wait a moment before sending another message."},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return

                # 2. Parse JSON body
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(raw_body)
        except Exception:
            self._json({"error": "Invalid JSON payload."}, HTTPStatus.BAD_REQUEST)
            return

        message = str(data.get("message", "")).strip()
        mode = str(data.get("mode", "step1")).strip()
        history = data.get("history", [])
        parsed_data = data.get("assessment_data") or data.get("parsed_data")

        if not message:
            self._json({"error": "Message is required."}, HTTPStatus.BAD_REQUEST)
            return

        # 3. Privacy Guardrail Check
        privacy_warning = check_privacy(message)
        if privacy_warning:
            self._json({"error": privacy_warning}, HTTPStatus.BAD_REQUEST)
            return

        # 4. Generate Coach Response via Dual-Engine Coordinator
        reply, is_live, engine = ask_coach(message, mode, history, headers=self.headers, parsed_data=parsed_data)
        response_id = f"resp-{uuid.uuid4().hex[:12]}"
        self._json({
            "reply": reply,
            "live": is_live,
            "engine": engine,
            "response_id": response_id
        })


# ==============================================================================
# 8. SERVER ENTRYPOINT
# ==============================================================================

def main():
    server_address = (HOST, PORT)
    with ThreadingHTTPServer(server_address, CoachHandler) as httpd:
        print("================================================================")
        print(f"ENS 101 Mentor Desk v1.0 running at http://localhost:{PORT}")
        print(f"   Primary Engine:   {'OpenAI-compatible (' + QWEN_MODEL + ')' if LM_STUDIO_URL else 'Not configured'}")
        print(f"   Fallback Engine:  {'Google Gemini (' + GEMINI_MODEL + ')' if GEMINI_API_KEY else 'Offline Fallback (Set GEMINI_API_KEY to enable Gemini)'}")
        print(f"   Rate Limit:       {RATE_LIMIT} req/min per IP")
        print(f"   Feedback Store:   SQLite ({DB_PATH.name})")
        print("================================================================")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    main()
