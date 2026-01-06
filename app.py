import streamlit as st
import fitz  # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- MODEL CONSTANTS (UPDATED) ---
# Text Model: Llama 3.3 70B (State of the art open source)
GROQ_MODEL = "llama-3.3-70b-versatile"
# Vision Model: Llama 3.2 90B Vision (Replaces the decommissioned 11B model)
GROQ_VISION_MODEL = "llama-3.2-90b-vision-preview"

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
        # Schema migration for existing users
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
def get_system_prompt(level):
    if level == "Basic":
        return "Act as a Tutor. GOAL: Pass the exam. Focus on definitions, brevity, and outlines. Output strictly Markdown."
    elif level == "Intermediate":
        return "Act as a Professor. GOAL: Solid understanding. Use detailed definitions, process steps, and exam tips. Output strictly Markdown."
    else:
        return "Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Output strictly Markdown."

def generate_study_notes(text, level, client):
    prompt = f"{get_system_prompt(level)}\nCONTENT: {text[:25000]}\nOutput strictly Markdown."
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}]).choices[0].message.content
    except Exception as e: return f"Error: {e}"

def generate_analogies(notes, client):
    sys_prompt = "Identify 5 key concepts. For each, provide a real-life analogy. Format: '**[Concept]**' followed by 'Analogy: [Analogy]'."
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": notes[:10000]}]).choices[0].message.content
    except: return "Error generating analogies."

def generate_specific_analogy(topic, client):
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Give a detailed analogy for: {topic}"}]).choices[0].message.content
    except: return "Error."

def generate_interactive_drills(notes, client):
    prompt = """You are a quiz master. Generate a quiz with 10 questions total (5 MCQ, 5 T/F).
    JSON Format MUST be:
    {
      "quiz_title": "Interactive Practice Drill (General)",
      "questions": [
        {
          "id": 1,
          "type": "MCQ",
          "question_text": "...",
          "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "primary_concept": "Short Concept Name", 
          "detailed_explanation": "..."
        }
      ]
    }
    """
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": notes[:15000]}], response_format={"type": "json_object"}).choices[0].message.content
    except: return None

def generate_focused_drills(notes, topics, client):
    t_str = ", ".join(topics)
    prompt = f"""Generate a 10 question quiz focusing ONLY on these topics: {t_str}.
    JSON Format MUST be:
    {{
      "quiz_title": "Adaptive Focus Drill",
      "questions": [
        {{
          "id": 1,
          "type": "MCQ",
          "question_text": "...",
          "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "primary_concept": "Exact Topic Name", 
          "detailed_explanation": "..."
        }}
      ]
    }}
    """
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": notes[:15000]}], response_format={"type": "json_object"}).choices[0].message.content
    except: return None

def analyze_exam_paper(txt, imgs, client):
    prompt_text = "You are an expert exam strategist. Analyze the provided exam paper content. Output a structured Markdown report with: 1. High-Priority Topics (Top 3-5 themes). 2. Question Patterns. 3. Strategic Advice."
    if imgs:
        payload = [{"type": "text", "text": "Analyze this scanned exam paper. Focus on identifying repeated questions and high-value topics."}]
        for img in imgs: payload.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}})
        try:
            return client.chat.completions.create(model=GROQ_VISION_MODEL, messages=[{"role": "user", "content": payload}], max_tokens=2000).choices[0].message.content
        except Exception as e: return f"Vision Error: {e}"
    else:
        try:
            return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": prompt_text}, {"role": "user", "content": f"Paper Content: {txt[:20000]}"}]).choices[0].message.content
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
            st.markdown(f"**{i+1}. {q.get('primary_concept','')}:** {q['question_text']}")
            qid = q['id']
            if q['type'] == 'MCQ':
                opts = [o.split(': ')[1] if ': ' in o else o for o in q['options']]
                st.session_state.user_answers[qid] = st.radio("Choose:", opts, key=f"q_{qid}")
                # Map back to letter for grading
                if st.session_state.user_answers[qid] in opts:
                    idx = opts.index(st.session_state.user_answers[qid])
                    st.session_state.user_answers[qid] = ['A','B','C','D'][idx]
            else:
                st.session_state.user_answers[qid] = st.radio("Choose:", ["True", "False"], key=f"q_{qid}")
            
            if st.session_state.quiz_submitted:
                cor = q['correct_answer']
                user = st.session_state.user_answers.get(qid)
                match = (user == cor) if q['type'] == 'MCQ' else (str(user) == str(cor))
                if match: st.markdown(f'<div class="feedback-box correct-feedback">✅ Correct!</div>', unsafe_allow_html=True)
                else: st.markdown(f'<div class="feedback-box incorrect-feedback">❌ Incorrect. Answer: {cor}.<br><em>{q.get("detailed_explanation","")}</em></div>', unsafe_allow_html=True)
            st.markdown("---")
        
        if st.form_submit_button("Submit Quiz"):
            process_quiz(project, data['questions'], st.session_state.user_answers)
            st.session_state.quiz_submitted = True
            st.rerun()

    if st.button("Reset Quiz"):
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

# --- SIDEBAR ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    if "GROQ_API_KEY" in st.secrets: st.session_state.groq_api_key = st.secrets["GROQ_API_KEY"]
    key = st.text_input("Groq API Key", type="password", value=st.session_state.groq_api_key or "")
    if key: st.session_state.groq_api_key = key
    
    st.markdown("---")
    for p in db.load_all_projects():
        if st.button(f"📄 {p}"): 
            st.session_state.current_project = p
            st.session_state.quiz_submitted = False
            st.session_state.quiz_data = None
            st.session_state.weak_topics = []
            st.rerun()
            
    if st.button("➕ New Project"):
        st.session_state.current_project = None
        st.rerun()

if not st.session_state.groq_api_key:
    st.warning("🚨 Please configure your Groq API Key in the sidebar.")
    st.stop()

client = Groq(api_key=st.session_state.groq_api_key)

# --- MAIN PAGE ---
if not st.session_state.current_project:
    st.title("🚀 New Study Project")
    uploaded_file = st.file_uploader("Upload PDF Document", type="pdf")
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1: project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2: level = st.select_slider("Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
        
        if st.button("✨ Create Project"):
            with st.spinner("Processing..."):
                txt, _ = extract_content_smart(uploaded_file)
                if len(txt) < 50: 
                    st.error("⚠️ Document text is too sparse (Scanned PDF). Notes require selectable text.")
                else:
                    notes = generate_study_notes(txt, level, client)
                    ana = generate_analogies(notes, client)
                    db.save_project(project_name, level, notes, txt, analogy_data=json.dumps({"default": ana}))
                    st.session_state.current_project = project_name
                    st.rerun()
else:
    proj = db.get_project_details(st.session_state.current_project)
    if proj:
        practice_data = json.loads(proj.get('practice_data') or "{}")
        analogy_data = json.loads(proj.get('analogy_data') or "{}")
        
        st.title(f"📘 {proj['name']}")
        t1, t2, t3, t4, t5 = st.tabs(["📖 Notes", "💡 Analogies", "📈 Exam Analysis", "🧠 Practice", "📊 Progress"])
        
        with t1:
            st.markdown(proj['notes'])
            
        with t2:
            st.subheader("Concept Analogies")
            st.markdown(analogy_data.get('default', ""))
            if st.button("Refresh Analogies"):
                new_a = generate_analogies(proj['notes'], client)
                db.update_analogy_data(proj['name'], "default", new_a)
                st.rerun()
            st.divider()
            t_req = st.text_input("Request specific analogy:")
            if st.button("Generate Analogy") and t_req:
                res = generate_specific_analogy(t_req, client)
                st.markdown(res)

        with t3:
            st.header("Exam Paper Analysis")
            st.info("Upload a past paper. **Supports Text PDFs & Scanned Images!**")
            exam_pdf = st.file_uploader("Upload Exam PDF", type="pdf", key="exam_up")
            
            if exam_pdf:
                if st.session_state.last_uploaded_exam_id != exam_pdf.file_id:
                    with st.spinner("Reading PDF..."):
                        txt, imgs = extract_content_smart(exam_pdf)
                        st.session_state.exam_analysis_content_cache = (txt, imgs)
                        st.session_state.last_uploaded_exam_id = exam_pdf.file_id
                
                txt_c, img_c = st.session_state.exam_analysis_content_cache
                if img_c: st.warning("📷 **Scanned Document Detected.** Using Vision Model (Slower but accurate).")
                else: st.success("📄 **Text Document Detected.** Using Standard Model.")
                
                if st.button("🎯 Run Analysis"):
                    res = analyze_exam_paper(txt_c, img_c, client)
                    db.update_exam_analysis_data(proj['name'], "latest", res)
                    st.session_state.exam_analysis_text = res
                    st.rerun()
            
            disp = st.session_state.exam_analysis_text or json.loads(proj.get('exam_analysis') or "{}").get('latest')
            if disp: st.markdown(disp)

        with t4:
            st.subheader("Interactive Quizzes")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Generate General Quiz"):
                    q = generate_interactive_drills(proj['notes'], client)
                    if q: 
                        db.update_practice_data(proj['name'], "quiz", q)
                        st.session_state.quiz_data = q
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()
            with c2:
                weak = st.session_state.weak_topics
                if st.button(f"Generate Focus Quiz ({len(weak)} topics)", disabled=not weak):
                    q = generate_focused_drills(proj['notes'], weak, client)
                    if q:
                        db.update_practice_data(proj['name'], "quiz", q)
                        st.session_state.quiz_data = q
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()
            
            curr_q = st.session_state.quiz_data or practice_data.get('quiz')
            if curr_q: display_quiz(proj['name'], curr_q)

        with t5:
            st.header("Progress Tracker")
            if st.button("⚠️ Clear Progress Data"):
                db.reset_progress_tracker(proj['name'])
                st.rerun()
            
            tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            data = []
            current_weak = []
            for k,v in tracker.items():
                acc = (v['correct']/v['total']*100) if v['total'] > 0 else 0
                status = "🟢 Strong" if acc > 80 else "🔴 Weak"
                if acc <= 80: current_weak.append(k)
                data.append({"Topic": k, "Accuracy": f"{acc:.1f}%", "Attempts": v['total'], "Status": status})
            
            st.session_state.weak_topics = current_weak
            if data: st.dataframe(data)
            else: st.info("Take quizzes to see stats!")
