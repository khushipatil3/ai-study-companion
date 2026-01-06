import streamlit as st
import fitz  # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- MODEL CONSTANTS ---
GROQ_MODEL = "llama-3.1-8b-instant" 
GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .main { background-color: #f0f2f6; color: #1c1e21; }
    .css-1d3f8rz { background-color: #ffffff; }
    .correct { color: green; font-weight: bold; }
    .incorrect { color: red; font-weight: bold; }
    .feedback-box { padding: 10px; margin: 5px 0; border-radius: 5px; }
    .correct-feedback { background-color: #e6ffe6; border-left: 5px solid green; }
    .incorrect-feedback { background-color: #ffe6e6; border-left: 5px solid red; }
</style>
""", unsafe_allow_html=True)

# --- DATABASE LAYER (SQLite) ---
class StudyDB:
    def __init__(self, db_name='study_db.sqlite'):
        self.db_name = db_name
        self.init_db()

    def connect(self):
        return sqlite3.connect(self.db_name)

    def init_db(self):
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
        for col in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                try: c.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT DEFAULT '{{}}'")
                except: pass
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

    def update_json_field(self, name, field, key, content):
        proj = self.get_project_details(name)
        if not proj: return
        data = json.loads(proj.get(field) or "{}")
        data[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f'UPDATE projects SET {field} = ? WHERE name = ?', (json.dumps(data), name))
        conn.commit()
        conn.close()
        
    def update_practice_data(self, name, key, content): return self.update_json_field(name, 'practice_data', key, content)
    def update_analogy_data(self, name, key, content): return self.update_json_field(name, 'analogy_data', key, content)
    def update_exam_analysis_data(self, name, key, content): return self.update_json_field(name, 'exam_analysis', key, content)

    def load_all_projects(self):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name FROM projects")
        return [row[0] for row in c.fetchall()]

    def get_project_details(self, name):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT * FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3],
                "progress": row[4], "practice_data": row[5], "analogy_data": row[6], "exam_analysis": row[7]
            }
        return None
        
    def update_progress_tracker(self, project_name, concept_scores):
        proj = self.get_project_details(project_name)
        if not proj: return
        p_data = json.loads(proj.get('practice_data') or "{}")
        tracker = json.loads(p_data.get('progress_tracker') or "{}")

        for concept, (correct, total) in concept_scores.items():
            if concept not in tracker: tracker[concept] = {"correct": 0, "total": 0}
            tracker[concept]["correct"] += correct
            tracker[concept]["total"] += total
        
        p_data['progress_tracker'] = json.dumps(tracker)
        conn = self.connect()
        c = conn.cursor()
        c.execute('UPDATE projects SET practice_data = ? WHERE name = ?', (json.dumps(p_data), project_name))
        conn.commit()
        conn.close()

    def reset_progress_tracker(self, project_name):
        proj = self.get_project_details(project_name)
        if not proj: return
        p_data = json.loads(proj.get('practice_data') or "{}")
        p_data['progress_tracker'] = json.dumps({}) 
        conn = self.connect()
        c = conn.cursor()
        c.execute('UPDATE projects SET practice_data = ? WHERE name = ?', (json.dumps(p_data), project_name))
        conn.commit()
        conn.close()

db = StudyDB()

# --- SESSION STATE ---
if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'groq_api_key' not in st.session_state: st.session_state.groq_api_key = None 
if 'quiz_data' not in st.session_state: st.session_state.quiz_data = None
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state: st.session_state.user_answers = {}
if 'exam_analysis_text' not in st.session_state: st.session_state.exam_analysis_text = None
if 'exam_analysis_content_cache' not in st.session_state: st.session_state.exam_analysis_content_cache = None
if 'last_uploaded_exam_id' not in st.session_state: st.session_state.last_uploaded_exam_id = None
if 'weak_topics' not in st.session_state: st.session_state.weak_topics = []

# --- HELPERS ---
def safe_json_parse(json_str):
    if not json_str: return None
    try:
        start = json_str.find('{')
        end = json_str.rfind('}')
        if start == -1 or end == -1: return json.loads(json_str.strip())
        clean = json_str[start:end+1]
        return json.loads(clean)
    except: return None

def pdf_page_to_base64(page):
    pix = page.get_pixmap()
    return base64.b64encode(pix.tobytes("png")).decode('utf-8')

def extract_content_smart(uploaded_file):
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_text = ""
    total_len = 0
    check_limit = min(3, len(doc))
    for i in range(check_limit):
        total_len += len(doc[i].get_text())
    
    is_scanned = (total_len / check_limit < 50) if check_limit > 0 else True
    
    if is_scanned:
        images = []
        for i in range(min(len(doc), 5)):
            images.append(pdf_page_to_base64(doc[i]))
        return "", images
    else:
        for page in doc: full_text += page.get_text() + "\n"
        return full_text, None

# --- LLM FUNCTIONS ---
def generate_study_notes(text, level, client):
    prompt = f"Act as a {level} Tutor. Summarize these notes in Markdown: {text[:25000]}"
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}]).choices[0].message.content
    except Exception as e: return f"Error: {e}"

def generate_analogies(notes, client):
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Provide 5 analogies for key concepts in: {notes[:10000]}"}]).choices[0].message.content
    except: return "Error."

def generate_interactive_drills(notes, client):
    prompt = """Generate JSON quiz: {"quiz_title": "General Drill", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A: .."], "correct_answer": "A", "primary_concept": "Topic", "detailed_explanation": "..."}]}"""
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": notes[:15000]}], response_format={"type": "json_object"}).choices[0].message.content
    except: return None

def generate_focused_drills(notes, topics, client):
    prompt = f"""Generate JSON quiz for topics {topics}: {"quiz_title": "Focus Drill", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A: .."], "correct_answer": "A", "primary_concept": "Topic", "detailed_explanation": "..."}]}"""
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": notes[:15000]}], response_format={"type": "json_object"}).choices[0].message.content
    except: return None

def analyze_exam_paper(txt, imgs, client):
    prompt_text = "Analyze this exam paper. List High-Priority Topics, Question Patterns, and Strategy."
    if imgs:
        payload = [{"type": "text", "text": "Analyze this scanned exam paper."}]
        for img in imgs: payload.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}})
        try:
            return client.chat.completions.create(model=GROQ_VISION_MODEL, messages=[{"role": "user", "content": payload}], max_tokens=2000).choices[0].message.content
        except Exception as e: return f"Vision Error: {e}"
    else:
        try:
            return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"{prompt_text} Content: {txt[:20000]}"}]).choices[0].message.content
        except Exception as e: return f"Text Error: {e}"

# --- UI LOGIC ---
def process_quiz(project, questions, answers):
    scores = {}
    valid = [q for q in questions if 'id' in q]
    for q in valid:
        concept = q.get('primary_concept', 'General')
        user_ans = answers.get(q['id'])
        correct = False
        if user_ans:
            if q['type'] == 'MCQ': correct = (user_ans == q['correct_answer'])
            else: correct = (str(user_ans).strip() == str(q['correct_answer']).strip())
        
        if concept not in scores: scores[concept] = [0, 0]
        scores[concept][1] += 1
        if correct: scores[concept][0] += 1
    db.update_progress_tracker(project, {k: tuple(v) for k,v in scores.items()})

def display_quiz(project, quiz_json):
    data = safe_json_parse(quiz_json)
    if not data: return
    st.subheader(data.get('quiz_title', 'Quiz'))
    
    with st.form("quiz"):
        for i, q in enumerate(data.get('questions', [])):
            st.markdown(f"**{i+1}. {q['question_text']}**")
            qid = q['id']
            if q['type'] == 'MCQ':
                opts = [o.split(': ')[1] if ': ' in o else o for o in q['options']]
                st.session_state.user_answers[qid] = st.radio("Choose:", opts, key=f"q_{qid}")
                # Map back to letter
                if st.session_state.user_answers[qid] in opts:
                    idx = opts.index(st.session_state.user_answers[qid])
                    st.session_state.user_answers[qid] = ['A','B','C','D'][idx]
            else:
                st.session_state.user_answers[qid] = st.radio("Choose:", ["True", "False"], key=f"q_{qid}")
            
            if st.session_state.quiz_submitted:
                cor = q['correct_answer']
                user = st.session_state.user_answers.get(qid)
                match = (user == cor) if q['type'] == 'MCQ' else (str(user) == str(cor))
                if match: st.success("Correct!")
                else: st.error(f"Incorrect. Answer: {cor}. {q.get('detailed_explanation','')}")
            st.markdown("---")
        
        if st.form_submit_button("Submit"):
            process_quiz(project, data['questions'], st.session_state.user_answers)
            st.session_state.quiz_submitted = True
            st.rerun()

    if st.button("Reset Quiz"):
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

# --- SIDEBAR ---
with st.sidebar:
    st.title("AI Companion")
    if "GROQ_API_KEY" in st.secrets: st.session_state.groq_api_key = st.secrets["GROQ_API_KEY"]
    key = st.text_input("API Key", type="password", value=st.session_state.groq_api_key or "")
    if key: st.session_state.groq_api_key = key
    
    st.markdown("---")
    for p in db.load_all_projects():
        # UPDATED: Replaced use_container_width with width='stretch' per 2026 deprecation
        if st.button(f"📄 {p}"): 
            st.session_state.current_project = p
            st.session_state.quiz_submitted = False
            st.session_state.quiz_data = None
            st.rerun()
            
    if st.button("➕ New Project"):
        st.session_state.current_project = None
        st.rerun()

if not st.session_state.groq_api_key:
    st.warning("Enter API Key")
    st.stop()

client = Groq(api_key=st.session_state.groq_api_key)

# --- MAIN PAGE ---
if not st.session_state.current_project:
    st.title("New Project")
    # VARIABLE: uploaded_file (Only used here)
    uploaded_file = st.file_uploader("Upload PDF", type="pdf")
    if uploaded_file and st.button("Create"):
        with st.spinner("Processing..."):
            txt, _ = extract_content_smart(uploaded_file)
            if len(txt) < 50: st.error("Scanned PDF detected. Notes require text PDF.")
            else:
                notes = generate_study_notes(txt, "Intermediate", client)
                ana = generate_analogies(notes, client)
                db.save_project(uploaded_file.name, "Intermediate", notes, txt, analogy_data=json.dumps({"default": ana}))
                st.session_state.current_project = uploaded_file.name
                st.rerun()
else:
    proj = db.get_project_details(st.session_state.current_project)
    st.title(proj['name'])
    t1, t2, t3, t4 = st.tabs(["Notes", "Exam Analysis", "Practice", "Progress"])
    
    with t1: st.markdown(proj['notes'])
    
    with t2:
        st.header("Exam Analysis")
        # VARIABLE: exam_pdf (Explicitly distinct)
        exam_pdf = st.file_uploader("Upload Exam PDF", type="pdf", key="exam_up")
        
        if exam_pdf:
            # FIX: Using exam_pdf.file_id
            if st.session_state.last_uploaded_exam_id != exam_pdf.file_id:
                with st.spinner("Reading..."):
                    txt, imgs = extract_content_smart(exam_pdf)
                    st.session_state.exam_analysis_content_cache = (txt, imgs)
                    st.session_state.last_uploaded_exam_id = exam_pdf.file_id
            
            txt_c, img_c = st.session_state.exam_analysis_content_cache
            if img_c: st.warning("Scanned PDF. Using Vision Model.")
            
            if st.button("Analyze"):
                res = analyze_exam_paper(txt_c, img_c, client)
                db.update_exam_analysis_data(proj['name'], "latest", res)
                st.session_state.exam_analysis_text = res
                st.rerun()
        
        disp = st.session_state.exam_analysis_text or json.loads(proj.get('exam_analysis') or "{}").get('latest')
        if disp: st.markdown(disp)

    with t3:
        if st.button("Gen Quiz"):
            q = generate_interactive_drills(proj['notes'], client)
            if q: 
                db.update_practice_data(proj['name'], "quiz", q)
                st.session_state.quiz_data = q
                st.rerun()
        
        curr_q = st.session_state.quiz_data or json.loads(proj.get('practice_data') or "{}").get('quiz')
        if curr_q: display_quiz(proj['name'], curr_q)

    with t4:
        if st.button("Clear Data"):
            db.reset_progress_tracker(proj['name'])
            st.rerun()
        
        tracker = json.loads(json.loads(proj.get('practice_data') or "{}").get('progress_tracker') or "{}")
        data = [{"Topic": k, "Score": f"{(v['correct']/v['total']*100):.0f}%"} for k,v in tracker.items()]
        # FIX: Replaced use_container_width with width='stretch' to fix 2026 error
        if data: st.dataframe(data, width=1000)
