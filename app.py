import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64 

# --- MODEL CONSTANTS ---
GROQ_MODEL = "llama-3.1-8b-instant" 
# New Vision model for scanned exam papers
GROQ_VISION_MODEL = "llama-3.2-90b-vision-preview"

# --- CONFIGURABLE THRESHOLDS ---
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 
WEAK_TOPIC_MIN_ATTEMPTS = 3          

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .main { background-color: #f0f2f6; color: #1c1e21; }
    .css-1d3f8rz { background-color: #ffffff; }
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
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

    def update_project_json_field(self, name, field_name, key, content):
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get(field_name) or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f'UPDATE projects SET {field_name} = ? WHERE name = ?', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()
        
    def update_practice_data(self, name, key, content): return self.update_project_json_field(name, 'practice_data', key, content)
    def update_analogy_data(self, name, key, content): return self.update_project_json_field(name, 'analogy_data', key, content)
    def update_exam_analysis_data(self, name, key, content): return self.update_project_json_field(name, 'exam_analysis', key, content)

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
        c.execute('UPDATE projects SET practice_data = ? WHERE name = ?', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

    def reset_progress_tracker(self, project_name):
        project_data = self.get_project_details(project_name)
        if not project_data: return
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({})
        conn = self.connect()
        c = conn.cursor()
        c.execute('UPDATE projects SET practice_data = ? WHERE name = ?', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

db = StudyDB()

# --- SESSION STATE ---
if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'theory_marks' not in st.session_state: st.session_state.theory_marks = 5
if 'groq_api_key' not in st.session_state: st.session_state.groq_api_key = None 
if 'quiz_data' not in st.session_state: st.session_state.quiz_data = None
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state: st.session_state.user_answers = {}
if 'quiz_type' not in st.session_state: st.session_state.quiz_type = 'general'
if 'exam_analysis_text' not in st.session_state: st.session_state.exam_analysis_text = None
if 'exam_analysis_content_cache' not in st.session_state: st.session_state.exam_analysis_content_cache = None
if 'last_uploaded_exam_pdf_id' not in st.session_state: st.session_state.last_uploaded_exam_pdf_id = None
if 'weak_topics' not in st.session_state: st.session_state.weak_topics = []
if 'focus_quiz_active' not in st.session_state: st.session_state.focus_quiz_active = False
if 'qna_display_key' not in st.session_state: st.session_state.qna_display_key = None
if 'qna_content' not in st.session_state: st.session_state.qna_content = None

# --- HELPER FUNCTIONS ---
def safe_json_parse(json_str):
    if not json_str: return None
    try:
        start = json_str.find('{')
        end = json_str.rfind('}')
        if start == -1 or end == -1: return json.loads(json_str.strip())
        clean = json_str[start:end+1]
        if clean.startswith('```json'): clean = clean[7:].strip()
        if clean.endswith('```'): clean = clean[:-3].strip()
        return json.loads(clean)
    except: return None

# --- NEW UTILITY FOR VISION/TEXT ---
def pdf_page_to_base64(page):
    pix = page.get_pixmap()
    return base64.b64encode(pix.tobytes("png")).decode('utf-8')

def extract_content_smart(uploaded_file):
    """
    Intelligently extracts content. 
    Returns: (text_content, images_list_b64)
    If text is sufficient, images_list_b64 is None.
    If text is sparse (scan), text_content is None and images_list_b64 is populated.
    """
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
        for page in doc: full_text += page.get_text() + "\n--- PAGE_BREAK ---\n"
        return full_text, None

# --- LLM FUNCTIONS ---
def get_system_prompt(level):
    if level == "Basic": return "Act as a Tutor. GOAL: Pass the exam. Focus on definitions. Output strictly Markdown."
    elif level == "Intermediate": return "Act as a Professor. GOAL: Solid understanding. Output strictly Markdown."
    else: return "Act as a Subject Matter Expert. GOAL: Mastery. Output strictly Markdown."

def _attempt_quiz_generation(system_prompt, notes_truncated, client):
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate 10 questions in strict JSON format based on these notes: {notes_truncated}"}
            ],
            response_format={"type": "json_object"}, temperature=0.8
        )
        return completion.choices[0].message.content
    except Exception as e:
        if 'invalid_api_key' in str(e): st.error("❌ API Key Error: Check sidebar settings.")
        return None

def generate_interactive_drills(notes, client):
    system_prompt = """You are a quiz master. Generate a quiz with 10 questions total (5 MCQ, 5 T/F).
    JSON Format: {"quiz_title": "Drill", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A: ..","B: .."], "correct_answer": "A", "primary_concept": "Concept", "detailed_explanation": "..."}]}"""
    return _attempt_quiz_generation(system_prompt, notes[:15000], client)

def generate_focused_drills(notes, weak_topics, client):
    t_str = ", ".join(weak_topics)
    system_prompt = f"""Generate JSON quiz for topics: {t_str}.
    JSON Format: {"quiz_title": "Focus Drill", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A: ..","B: .."], "correct_answer": "A", "primary_concept": "Concept", "detailed_explanation": "..."}]}"""
    return _attempt_quiz_generation(system_prompt, notes[:15000], client)

def generate_study_notes(raw_text, level, client):
    prompt = f"""{get_system_prompt(level)}\nCONTENT: {raw_text[:25000]}\nOutput strictly Markdown."""
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3)
        return completion.choices[0].message.content
    except Exception as e: return f"Error: {e}"

def generate_analogies(notes, client):
    sys = "Identify 5 key concepts. Provide real-life analogies. Format: '**[Concept]**' followed by 'Analogy: [Analogy]'."
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": sys}, {"role": "user", "content": notes[:10000]}]).choices[0].message.content
    except: return "Error."

def generate_specific_analogy(topic, client):
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Give a detailed analogy for: {topic}"}]).choices[0].message.content
    except: return "Error."

def generate_qna(notes, q_type, marks, client):
    prompt = f"Generate {q_type} questions based on these notes."
    if q_type == "custom": prompt += f" Each worth {marks} marks."
    try:
        return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"{prompt} Content: {notes[:15000]}"}]).choices[0].message.content
    except: return "Error."

def analyze_past_papers(text_content, image_content, client):
    """
    Robust Analysis: Tries Vision, Falls back to Text if Vision fails.
    """
    prompt_text = "Analyze this exam paper. List: 1. High-Priority Topics. 2. Question Patterns. 3. Strategic Advice."
    
    if image_content:
        # Try Vision first
        payload = [{"type": "text", "text": "Analyze this scanned exam paper."}]
        for img in image_content: payload.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}})
        try:
            return client.chat.completions.create(model=GROQ_VISION_MODEL, messages=[{"role": "user", "content": payload}], max_tokens=2000).choices[0].message.content
        except Exception as e:
            # Fallback to Text analysis if Vision fails
            if text_content and len(text_content) > 50:
                fallback_msg = f"\n\n**⚠️ Note:** Visual scan analysis failed ({str(e)}). Falling back to text extraction analysis.\n\n---\n\n"
                try:
                    fallback_res = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"{prompt_text} Content: {text_content[:20000]}"}]).choices[0].message.content
                    return fallback_msg + fallback_res
                except:
                    return f"❌ Critical Error: Both Vision and Text analysis failed. Error: {e}"
            else:
                return f"❌ Error: Vision model unavailable ({e}) and no extractable text found in this document."
    else:
        # Text only path
        try:
            return client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"{prompt_text} Content: {text_content[:20000]}"}]).choices[0].message.content
        except Exception as e: return f"Text Analysis Error: {e}"

# --- UI INTERACTIVE LOGIC ---
def process_and_update_progress(project_name, questions, user_answers):
    scores = {} 
    valid_questions = [q for q in questions if 'id' in q]
    for q in valid_questions:
        concept = q.get('primary_concept', 'General') 
        user_ans = user_answers.get(q['id'])
        correct = False
        if user_ans:
            if q['type'] == 'MCQ': correct = (user_ans == q['correct_answer'])
            else: correct = (str(user_ans).strip() == str(q['correct_answer']).strip())
        
        if concept not in scores: scores[concept] = [0, 0]
        scores[concept][1] += 1 
        if correct: scores[concept][0] += 1 
    
    db_scores = {k: tuple(v) for k, v in scores.items()}
    db.update_progress_tracker(project_name, db_scores)

def display_and_grade_quiz(project_name, quiz_json_str):
    quiz_data = safe_json_parse(quiz_json_str)
    if quiz_data is None: return

    questions = quiz_data.get('questions', [])
    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Interactive Quiz')}")
    
    with st.form(key='quiz_form'):
        for i, q in enumerate(questions):
            q_id = q['id']
            st.markdown(f"**{i+1}. {q.get('primary_concept', '')}:** {q['question_text']}")
            
            if q['type'] == 'MCQ':
                opts = [opt.split(': ')[1] if ': ' in opt else opt for opt in q['options']]
                st.session_state.user_answers[q_id] = st.radio("Answer:", opts, key=f"q_{q_id}", index=None, disabled=st.session_state.quiz_submitted)
                if st.session_state.user_answers[q_id] in opts:
                    idx = opts.index(st.session_state.user_answers[q_id])
                    st.session_state.user_answers[q_id] = ['A','B','C','D'][idx]
            else:
                st.session_state.user_answers[q_id] = st.radio("Answer:", ["True", "False"], key=f"q_{q_id}", index=None, disabled=st.session_state.quiz_submitted)

            if st.session_state.quiz_submitted:
                correct = q['correct_answer']
                user = st.session_state.user_answers.get(q_id)
                match = (user == correct) if q['type'] == 'MCQ' else (str(user) == str(correct))
                if match: st.markdown(f'<div class="feedback-box correct-feedback">✅ Correct!</div>', unsafe_allow_html=True)
                else: st.markdown(f'<div class="feedback-box incorrect-feedback">❌ Incorrect. Answer: {correct}.<br>{q.get("detailed_explanation","")}</div>', unsafe_allow_html=True)
            st.markdown("---")

        if st.form_submit_button("Submit Quiz", disabled=st.session_state.quiz_submitted):
            process_and_update_progress(project_name, questions, st.session_state.user_answers)
            st.session_state.quiz_submitted = True
            st.rerun()

    if st.button("🔄 Reset Quiz"):
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

# --- SIDEBAR ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    if "GROQ_API_KEY" in st.secrets: st.session_state.groq_api_key = st.secrets["GROQ_API_KEY"]
    key_input = st.text_input("Groq API Key", type="password", value=st.session_state.groq_api_key or "")
    if key_input: st.session_state.groq_api_key = key_input

    st.markdown("---")
    for p in db.load_all_projects():
        if st.button(f"📄 {p}"):
            st.session_state.current_project = p
            st.session_state.quiz_submitted = False
            st.session_state.quiz_data = None
            st.rerun()
    
    if st.button("➕ Create New Project", type="primary"):
        st.session_state.current_project = None
        st.rerun()

if not st.session_state.groq_api_key:
    st.warning("Please configure your Groq API Key.")
    st.stop()
    
client = Groq(api_key=st.session_state.groq_api_key)

# --- MAIN APP LOGIC ---
if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    uploaded_file = st.file_uploader("Upload PDF Document", type="pdf")
    
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1: project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2: level = st.select_slider("Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
        
        if st.button("✨ Create Project"):
            with st.spinner("Processing..."):
                # Using smart extraction to handle both text and scans for Note Generation as well
                raw_text, _ = extract_content_smart(uploaded_file)
                if len(raw_text) > 50:
                    notes = generate_study_notes(raw_text, level, client)
                    ana = generate_analogies(notes, client)
                    db.save_project(project_name, level, notes, raw_text, analogy_data=json.dumps({"default": ana}))
                    st.session_state.current_project = project_name
                    st.rerun()
                else:
                    st.error("⚠️ Document is scanned/empty. Currently, Notes generation requires text. Exam Analysis can handle scans.")

else:
    proj = db.get_project_details(st.session_state.current_project)
    if proj:
        practice_data = json.loads(proj.get('practice_data') or "{}")
        analogy_data = json.loads(proj.get('analogy_data') or "{}")

        st.title(f"📘 {proj['name']}")
        tab1, tab_ana, tab_exam, tab_prac, tab_prog = st.tabs(["📖 Notes", "💡 Analogies", "📈 Exam Analysis", "🧠 Practice", "📊 Progress"])
        
        with tab1: st.markdown(proj['notes'])

        with tab_ana:
            st.subheader("Analogies")
            st.markdown(analogy_data.get('default', ""))
            if st.button("Refresh Analogies"):
                new_a = generate_analogies(proj['notes'], client)
                db.update_analogy_data(proj['name'], "default", new_a)
                st.rerun()
            st.divider()
            t_req = st.text_input("Request Analogy:")
            if st.button("Generate") and t_req:
                res = generate_specific_analogy(t_req, client)
                st.markdown(res)

        with tab_exam:
            st.header("Exam Analysis")
            
            # --- FIX: Use unique variable 'uploaded_pdf' for this tab ---
            uploaded_pdf = st.file_uploader("Upload Past Paper", type="pdf", key="exam_up")
            
            if uploaded_pdf:
                # --- FIX: Use uploaded_pdf.file_id to prevent NameError ---
                if st.session_state.last_uploaded_exam_pdf_id != uploaded_pdf.file_id:
                    with st.spinner("Reading PDF..."):
                        txt, imgs = extract_content_smart(uploaded_pdf)
                        st.session_state.exam_analysis_content_cache = (txt, imgs)
                        st.session_state.last_uploaded_exam_pdf_id = uploaded_pdf.file_id
                
                txt_c, img_c = st.session_state.exam_analysis_content_cache
                if img_c: st.warning("📷 Scanned PDF detected. Using Vision Model.")
                else: st.success("📄 Text PDF detected. Using Standard Model.")
                
                if st.button("Analyze Paper"):
                    res = analyze_exam_paper(txt_c, img_c, client)
                    db.update_exam_analysis_data(proj['name'], "latest", res)
                    st.session_state.exam_analysis_text = res
                    st.rerun()
            
            disp = st.session_state.exam_analysis_text or json.loads(proj.get('exam_analysis') or "{}").get('latest')
            if disp: st.markdown(disp)

        with tab_prac:
            st.header("Practice Tools")
            st1, st2 = st.tabs(["Theory Q&A", "Quiz"])
            
            with st1:
                c1, c2, c3 = st.columns(3)
                with c1: 
                    if st.button("Short Q&A"):
                        q = generate_qna(proj['notes'], "short", 0, client)
                        db.update_practice_data(proj['name'], "short_qna", q)
                        st.session_state.qna_display_key = "short_qna"
                        st.session_state.qna_content = q
                        st.rerun()
                with c2: 
                    if st.button("Long Q&A"):
                        q = generate_qna(proj['notes'], "long", 0, client)
                        db.update_practice_data(proj['name'], "long_qna", q)
                        st.session_state.qna_display_key = "long_qna"
                        st.session_state.qna_content = q
                        st.rerun()
                with c3:
                    marks = st.number_input("Marks", 1, 20, 5)
                    if st.button("Custom Q&A"):
                        q = generate_qna(proj['notes'], "custom", marks, client)
                        st.session_state.qna_content = q
                        st.rerun()
                
                d_key = st.session_state.qna_display_key
                content = st.session_state.qna_content or practice_data.get(d_key, "") if d_key else st.session_state.qna_content
                if content: st.markdown(content)

            with st2:
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("General Quiz"):
                        q = generate_interactive_drills(proj['notes'], client)
                        if q:
                            db.update_practice_data(proj['name'], "quiz", q)
                            st.session_state.quiz_data = q
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()
                with c2:
                    weak = st.session_state.weak_topics
                    if st.button(f"Focus Quiz ({len(weak)})", disabled=not weak):
                        q = generate_focused_drills(proj['notes'], weak, client)
                        if q:
                            db.update_practice_data(proj['name'], "quiz", q)
                            st.session_state.quiz_data = q
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()
                
                q_content = st.session_state.quiz_data or practice_data.get('quiz')
                if q_content: display_and_grade_quiz(proj['name'], q_content)

        with tab_prog:
            st.header("Progress")
            if st.button("Clear Data"):
                db.reset_progress_tracker(proj['name'])
                st.rerun()
            
            tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            data = []
            weak_list = []
            for k,v in tracker.items():
                acc = (v['correct']/v['total']*100) if v['total'] > 0 else 0
                status = "🟢 Strong" if acc > 80 else "🔴 Weak"
                if acc <= 80: weak_list.append(k)
                data.append({"Topic": k, "Score": f"{acc:.1f}%", "Attempts": v['total'], "Status": status})
            
            st.session_state.weak_topics = weak_list
            if data: st.dataframe(data)
            else: st.info("No data yet.")
