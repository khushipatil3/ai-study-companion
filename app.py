import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64 

# --- CONFIGURATION ---
GROQ_MODEL = "llama-3.1-8b-instant" 
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3          # Used for 'Low Data' message, no longer blocks adaptive logic

# --- DATABASE LAYER (SQLite) ---
class StudyDB:
    def __init__(self, db_name='study_db.sqlite'):
        self.db_name = db_name
        self.init_db()

    def connect(self):
        return sqlite3.connect(self.db_name)

    def init_db(self):
        """Creates the database table if it doesn't exist and handles schema migration."""
        conn = self.connect()
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                level TEXT,
                notes TEXT,
                raw_text TEXT,
                progress INTEGER DEFAULT 0,
                practice_data TEXT,
                analogy_data TEXT,
                exam_analysis TEXT
            )
        ''')

        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError as e:
                if "no such column" in str(e):
                    c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} TEXT DEFAULT '{{}}'")
        
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text, practice_data="{}", analogy_data="{}", exam_analysis="{}"):
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data, analogy_data, exam_analysis))
        conn.commit()
        conn.close()

    def get_project_details(self, name):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3],
                "progress": row[4], "practice_data": row[5], "analogy_data": row[6],
                "exam_analysis": row[7]
            }
        return None
        
    def update_practice_data(self, name, key, content):
        """FIXED: Added missing method to update practice JSON field (Theory Q&A)."""
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get('practice_data') or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET practice_data = ? WHERE name = ? ''', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()

    def update_progress_tracker(self, project_name, concept_scores):
        project_data = self.get_project_details(project_name)
        if not project_data: return
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        tracker = json.loads(practice_dict.get('progress_tracker') or "{}")
        for concept, (correct, total) in concept_scores.items():
            if concept not in tracker: tracker[concept] = {"correct": 0, "total": 0}
            tracker[concept]["correct"] += correct
            tracker[concept]["total"] += total
        practice_dict['progress_tracker'] = json.dumps(tracker)
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET practice_data = ? WHERE name = ? ''', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

    def reset_progress_tracker(self, project_name):
        project_data = self.get_project_details(project_name)
        if not project_data: return
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({}) 
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET practice_data = ? WHERE name = ? ''', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()
        
    def load_all_projects(self):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name FROM projects")
        projects = [row[0] for row in c.fetchall()]
        conn.close()
        return projects

    def update_analogy_data(self, name, key, content):
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get('analogy_data') or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET analogy_data = ? WHERE name = ? ''', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()

    def update_exam_analysis_data(self, name, key, content):
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get('exam_analysis') or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET exam_analysis = ? WHERE name = ? ''', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()

db = StudyDB()

# --- UTILITY & LLM LOGIC ---

def safe_json_parse(json_str):
    if not json_str: return None
    try:
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1: return json.loads(json_str.strip())
        clean_json_str = json_str[start_index:end_index + 1]
        return json.loads(clean_json_str)
    except: return None

def extract_content_text_only(uploaded_file):
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    for i, page in enumerate(doc):
        text = page.get_text("text") 
        full_content += f"\n--- PAGE_BREAK ---\n{text}\n"
    return full_content

def initialize_client(api_key):
    try: return Groq(api_key=api_key)
    except: return None

# --- GENERATION LOGIC ---

def generate_study_notes(raw_text, level, client):
    prompt = f"Act as an expert tutor. Level: {level}. Synthesize these notes into a clean markdown study guide: {raw_text[:15000]}"
    completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3)
    return completion.choices[0].message.content

def generate_interactive_drills(notes, client):
    system_prompt = "Generate 10 MCQ/TF questions in JSON format. MUST include 'primary_concept' and 'detailed_explanation'."
    completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": notes[:15000]}], response_format={"type": "json_object"})
    return completion.choices[0].message.content

def generate_qna(notes, q_type, marks, client):
    desc = f"{q_type} questions (weightage: {marks} marks)"
    prompt = f"Generate {desc} based on these notes: {notes[:10000]}"
    completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.5)
    return completion.choices[0].message.content

# --- MAIN UI ---
st.set_page_config(page_title="AI Study Companion", layout="wide")

if 'groq_api_key' not in st.session_state: st.session_state.groq_api_key = None
if 'current_project' not in st.session_state: st.session_state.current_project = None

with st.sidebar:
    st.title("🎓 Study Companion")
    api_key_input = st.text_input("Groq API Key", type="password")
    if api_key_input: st.session_state.groq_api_key = api_key_input
    
    saved_projects = db.load_all_projects()
    if saved_projects:
        st.subheader("📁 Saved Units")
        for p in saved_projects:
            if st.button(p, use_container_width=True):
                st.session_state.current_project = p
                st.rerun()
    if st.button("➕ New Project"):
        st.session_state.current_project = None
        st.rerun()

client = initialize_client(st.session_state.groq_api_key)

if not st.session_state.groq_api_key:
    st.warning("Please enter your API Key in the sidebar.")
    st.stop()

# PROJECT CREATION OR DASHBOARD
if st.session_state.current_project is None:
    st.title("🚀 Create New Study Unit")
    uploaded_file = st.file_uploader("Upload PDF", type="pdf")
    if uploaded_file and st.button("Generate"):
        raw = extract_content_text_only(uploaded_file)
        notes = generate_study_notes(raw, "Intermediate", client)
        db.save_project(uploaded_file.name.split('.')[0], "Intermediate", notes, raw)
        st.session_state.current_project = uploaded_file.name.split('.')[0]
        st.rerun()
else:
    project_data = db.get_project_details(st.session_state.current_project)
    tab1, tab2, tab3 = st.tabs(["📖 Notes", "📝 Theory Hub", "📊 Progress"])
    
    with tab1:
        st.markdown(project_data['notes'])
    
    with tab2:
        st.header("📝 Theory Q&A")
        c1, c2, c3 = st.columns(3)
        if c1.button("Short Answers (2M)"):
            content = generate_qna(project_data['notes'], "short", 2, client)
            db.update_practice_data(project_data['name'], "short_qna", content)
            st.rerun()
        if c2.button("Long Answers (5M)"):
            content = generate_qna(project_data['notes'], "long", 5, client)
            db.update_practice_data(project_data['name'], "long_qna", content)
            st.rerun()
        
        practice_data = json.loads(project_data['practice_data'])
        if "short_qna" in practice_data: st.markdown(practice_data['short_qna'])
        if "long_qna" in practice_data: st.markdown(practice_data['long_qna'])

    with tab3:
        st.header("📊 Progress Tracker")
        # Tracker visualization logic...
