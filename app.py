import streamlit as st
import fitz  # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- MODEL CONSTANTS ---
# Text Model for Notes/Quizzes
GROQ_MODEL = "llama-3.1-8b-instant"
# Vision Model for Scanned Exams
GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"

# --- CONFIGURABLE THRESHOLDS ---
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80  # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3           # Used for 'Low Data' message

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
                practice_data TEXT DEFAULT '{}',
                analogy_data TEXT DEFAULT '{}',
                exam_analysis TEXT DEFAULT '{}'
            )
        ''')
        # Simple schema migration check
        try:
            c.execute("SELECT practice_data, analogy_data, exam_analysis FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            try:
                c.execute("ALTER TABLE projects ADD COLUMN practice_data TEXT DEFAULT '{}'")
            except: pass
            try:
                c.execute("ALTER TABLE projects ADD COLUMN analogy_data TEXT DEFAULT '{}'")
            except: pass
            try:
                c.execute("ALTER TABLE projects ADD COLUMN exam_analysis TEXT DEFAULT '{}'")
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
        
    def update_practice_data(self, name, key, content):
        return self.update_project_json_field(name, 'practice_data', key, content)
        
    def update_analogy_data(self, name, key, content):
        return self.update_project_json_field(name, 'analogy_data', key, content)
        
    def update_exam_analysis_data(self, name, key, content):
        return self.update_project_json_field(name, 'exam_analysis', key, content)

    def load_all_projects(self):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name FROM projects")
        projects = [row[0] for row in c.fetchall()]
        conn.close()
        return projects

    def get_project_details(self, name):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT * FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            # Map based on schema order
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
        self.save_project(project_data['name'], project_data['level'], project_data['notes'], 
                         project_data['raw_text'], practice_data=json.dumps(practice_dict),
                         analogy_data=project_data['analogy_data'], exam_analysis=project_data['exam_analysis'])

    def reset_progress_tracker(self, project_name):
        project_data = self.get_project_details(project_name)
        if not project_data: return
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({}) 
        self.update_practice_data(project_name, 'progress_tracker', json.dumps({}))

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
if 'weak_topics' not in st.session_state: st.session_state.weak_topics = []
if 'focus_quiz_active' not in st.session_state: st.session_state.focus_quiz_active = False

# --- HELPER FUNCTIONS ---
def safe_json_parse(json_str):
    if not json_str: return None
    try:
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1: return json.loads(json_str.strip())
        clean_json_str = json_str[start_index:end_index + 1]
        return json.loads(clean_json_str)
    except: return None

# --- VISION & PDF HELPERS ---
def pdf_page_to_base64(page):
    """Converts a PyMuPDF page to a base64 encoded PNG image."""
    pix = page.get_pixmap()
    img_bytes = pix.tobytes("png")
    return base64.b64encode(img_bytes).decode('utf-8')

def extract_text_with_fallback(uploaded_file):
    """
    Extracts text. If text is sparse (scanned), returns a flag indicating vision needed.
    Returns: (text_content, is_scanned_images_list_or_None)
    """
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_text = ""
    is_scanned = False
    
    # Check first few pages to see if it's scanned
    total_text_len = 0
    check_limit = min(3, len(doc))
    for i in range(check_limit):
        total_text_len += len(doc[i].get_text())
    
    # If average chars per page < 100, assume scanned
    if total_text_len / check_limit < 100:
        is_scanned = True
        
    if is_scanned:
        # Return list of base64 images for vision model
        images = []
        # Limit to first 5 pages to prevent token overflow in Vision models
        page_limit = min(len(doc), 5) 
        for i in range(page_limit):
            images.append(pdf_page_to_base64(doc[i]))
        return "", images
    else:
        # Standard text extraction
        for page in doc:
            full_text += page.get_text() + "\n--- PAGE_BREAK ---\n"
        return full_text, None

# --- LLM FUNCTIONS ---

def get_system_prompt(level):
    return f"Act as a Tutor for {level} level. Output strictly Markdown."

def generate_interactive_drills(notes, client):
    system_prompt = """Generate a JSON quiz with 10 questions (5 MCQ, 5 T/F) based on the notes.
    Format: {"quiz_title": "...", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A:..","B:.."], "correct_answer": "A", "primary_concept": "Short Concept Name", "detailed_explanation": "..."}]}"""
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Notes: {notes[:15000]}"}
            ], response_format={"type": "json_object"}
        )
        return completion.choices[0].message.content
    except Exception as e: return None

def generate_focused_drills(notes, weak_topics, client):
    topics_str = ", ".join(weak_topics)
    system_prompt = f"""Generate a JSON quiz with 10 questions strictly focusing on these Weak Topics: {topics_str}.
    Format: {"quiz_title": "Adaptive Drill", "questions": [{"id": 1, "type": "MCQ", "question_text": "...", "options": ["A:..","B:.."], "correct_answer": "A", "primary_concept": "Must match a weak topic", "detailed_explanation": "..."}]}"""
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Notes: {notes[:15000]}"}
            ], response_format={"type": "json_object"}
        )
        return completion.choices[0].message.content
    except: return None

def generate_study_notes(raw_text, level, client):
    prompt = f"Summarize these notes for a {level} student. Use Markdown. Content: {raw_text[:25000]}"
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3)
        return completion.choices[0].message.content
    except Exception as e: return f"Error: {e}"

def generate_analogies(notes, client):
    system_prompt = "Identify 5 key concepts and provide real-life analogies for them. Output Markdown."
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": notes[:10000]}])
        return completion.choices[0].message.content
    except: return "Error generating analogies."

def generate_specific_analogy(topic, client):
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Give a real-life analogy for: {topic}"}])
        return completion.choices[0].message.content
    except: return "Error."

def generate_qna(notes, q_type, marks, client):
    prompt = f"Generate {q_type} questions based on these notes."
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"{prompt} Content: {notes[:15000]}"}])
        return completion.choices[0].message.content
    except: return "Error."

def analyze_exam_paper(content, is_images, client):
    """
    Analyzes exam paper. Handles both text content and list of base64 images.
    """
    system_prompt_text = """
    You are an expert exam strategist. Analyze the provided exam paper content.
    Output a structured Markdown report with:
    1. 🚨 **High-Priority Topics:** (The top 3-5 themes that appear most frequently or carry the most marks).
    2. 📝 **Question Patterns:** (Are they mostly definitions, derivations, or problem-solving? Are questions repeated?).
    3. 🎯 **Strategic Advice:** (What should the student focus on to pass vs. to top the exam?).
    """
    
    if is_images:
        # VISION MODEL PATH
        images_payload = []
        for img_b64 in content: # content is a list of base64 strings
            images_payload.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })
        
        # Add text prompt to the payload
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "This is a scanned exam paper. Analyze it and identify the most important topics, repeated questions, and study focus areas. Format as a Markdown report."}
                ] + images_payload
            }
        ]
        
        try:
            with st.spinner("👀 AI Vision is reading the scanned paper..."):
                completion = client.chat.completions.create(
                    model=GROQ_VISION_MODEL,
                    messages=messages,
                    temperature=0.4,
                    max_tokens=2000
                )
            return completion.choices[0].message.content
        except Exception as e:
            return f"Error analyzing scanned paper: {e}. (Ensure your API Key supports Vision models)"

    else:
        # TEXT MODEL PATH
        try:
            with st.spinner("Analyzing exam text..."):
                completion = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt_text},
                        {"role": "user", "content": f"Paper Content: {content[:20000]}"}
                    ]
                )
            return completion.choices[0].message.content
        except Exception as e:
            return f"Error analyzing text paper: {e}"

# --- UI LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    concept_scores = {}
    valid_questions = [q for q in questions if 'id' in q and 'primary_concept' in q]
    for q in valid_questions:
        concept = q.get('primary_concept', 'General')
        user_ans = user_answers.get(q['id'])
        correct = False
        if q['type'] == 'MCQ': correct = (user_ans == q['correct_answer'])
        elif q['type'] == 'T/F': correct = (str(user_ans) == str(q['correct_answer']))
        
        if concept not in concept_scores: concept_scores[concept] = [0, 0]
        concept_scores[concept][1] += 1
        if correct: concept_scores[concept][0] += 1
            
    db_scores = {k: tuple(v) for k, v in concept_scores.items()}
    db.update_progress_tracker(project_name, db_scores)

def display_quiz_interface(project_name):
    quiz_data = safe_json_parse(st.session_state.quiz_data)
    if not quiz_data: return
    
    questions = quiz_data.get('questions', [])
    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Quiz')}")
    
    with st.form("quiz_form"):
        for i, q in enumerate(questions):
            st.markdown(f"**Q{i+1}:** {q['question_text']}")
            qid = q['id']
            if q['type'] == 'MCQ':
                opts = [o.split(': ')[1] if ': ' in o else o for o in q['options']]
                st.session_state.user_answers[qid] = st.radio("Choose:", opts, key=f"q_{qid}", index=None)
            else:
                st.session_state.user_answers[qid] = st.radio("True/False:", ["True", "False"], key=f"q_{qid}", index=None)
            st.markdown("---")
            
        submitted = st.form_submit_button("Submit Quiz")
        if submitted:
            process_and_update_progress(project_name, questions, st.session_state.user_answers)
            st.success("Quiz Submitted! Check Progress Tab.")
            st.rerun()

# --- MAIN APP ---

with st.sidebar:
    st.title("📚 AI Companion")
    
    # API Key
    api_key = st.text_input("Groq API Key", type="password", value=st.session_state.groq_api_key or "")
    if api_key: st.session_state.groq_api_key = api_key
    
    st.markdown("---")
    projects = db.load_all_projects()
    if projects:
        st.subheader("Your Projects")
        for p in projects:
            if st.button(f"📂 {p}", key=p):
                st.session_state.current_project = p
                st.session_state.quiz_data = None
                st.session_state.weak_topics = []
                st.rerun()
    
    if st.button("➕ New Project"):
        st.session_state.current_project = None
        st.rerun()

if not st.session_state.groq_api_key:
    st.warning("Please enter your Groq API Key in the sidebar.")
    st.stop()

client = Groq(api_key=st.session_state.groq_api_key)

# VIEW: NEW PROJECT
if not st.session_state.current_project:
    st.title("🚀 New Study Project")
    up_file = st.file_uploader("Upload Course PDF", type="pdf")
    if up_file and st.button("Create Project"):
        with st.spinner("Processing..."):
            text, _ = extract_text_with_fallback(up_file)
            if not text: text = "Scanned document detected. Notes generated from limited text."
            notes = generate_study_notes(text, "Intermediate", client)
            db.save_project(up_file.name, "Intermediate", notes, text)
            st.session_state.current_project = up_file.name
            st.rerun()

# VIEW: DASHBOARD
else:
    project = db.get_project_details(st.session_state.current_project)
    st.title(f"📘 {project['name']}")
    
    tab1, tab2, tab3, tab4 = st.tabs(["📖 Notes", "📈 Exam Analysis", "🧠 Practice", "📊 Progress"])
    
    with tab1:
        st.markdown(project['notes'])
        
    with tab2:
        st.header("🕵️‍♀️ Exam Paper Analysis")
        st.info("Upload a past paper (PDF). **Supports both selectable text and scanned images!**")
        
        exam_file = st.file_uploader("Upload Exam PDF", type="pdf", key="exam_upload")
        
        if exam_file and st.button("🔍 Analyze Paper"):
            # 1. Extract content (Text or Images)
            text_content, image_content = extract_text_with_fallback(exam_file)
            
            # 2. Analyze based on type
            if image_content:
                st.warning("📷 Scanned Document Detected! Switching to Llama 3.2 Vision Model...")
                analysis = analyze_exam_paper(image_content, True, client)
            else:
                st.success("📄 Text Document Detected. Analyzing text...")
                analysis = analyze_exam_paper(text_content, False, client)
                
            # 3. Save and display
            db.update_exam_analysis_data(project['name'], "latest", analysis)
            st.markdown("### 📝 Analysis Report")
            st.markdown(analysis)
            
        # Display previous analysis
        prev_analysis = json.loads(project.get('exam_analysis') or "{}").get('latest')
        if prev_analysis and not exam_file:
            st.markdown("### 📝 Last Analysis Report")
            st.markdown(prev_analysis)

    with tab3:
        st.header("Practice Arena")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Generate General Quiz"):
                q_json = generate_interactive_drills(project['notes'], client)
                if q_json:
                    st.session_state.quiz_data = q_json
                    st.session_state.user_answers = {}
                    st.rerun()
        with c2:
            weak = st.session_state.weak_topics
            if st.button(f"Generate Weakness Drill ({len(weak)})", disabled=not weak):
                q_json = generate_focused_drills(project['notes'], weak, client)
                if q_json:
                    st.session_state.quiz_data = q_json
                    st.session_state.user_answers = {}
                    st.rerun()
        
        if st.session_state.quiz_data:
            display_quiz_interface(project['name'])
            
    with tab4:
        st.header("Tracker")
        tracker = json.loads(json.loads(project.get('practice_data') or "{}").get('progress_tracker') or "{}")
        if st.button("Clear Tracker"):
            db.reset_progress_tracker(project['name'])
            st.rerun()
            
        weak_list = []
        for concept, stats in tracker.items():
            acc = (stats['correct']/stats['total'])*100 if stats['total']>0 else 0
            status = "🟢 Strong" if acc > 80 else "🔴 Weak"
            if acc <= 80: weak_list.append(concept)
            st.markdown(f"**{concept}**: {acc:.1f}% ({stats['correct']}/{stats['total']}) - {status}")
            
        st.session_state.weak_topics = weak_list
