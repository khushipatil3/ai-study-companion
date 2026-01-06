import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- MODEL CONSTANTS ---
# Current stable Groq model for fast, high-quality responses.
GROQ_MODEL = "llama-3.1-8b-instant" 
# Vision Model for Scanned Exams
GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"

# --- CONFIGURABLE THRESHOLDS ---
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3          # Used for 'Low Data' message

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    /* Customizing the main background and text for a softer look */
    .main {
        background-color: #f0f2f6; /* Light gray background */
        color: #1c1e21; /* Dark text */
    }
    /* Customizing sidebar background */
    .css-1d3f8rz {
        background-color: #ffffff; /* White sidebar */
    }
    /* Hide default streamlit elements */
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
    /* Highlight the Correct/Incorrect feedback */
    .correct {
        color: green;
        font-weight: bold;
    }
    .incorrect {
        color: red;
        font-weight: bold;
    }
    .feedback-box {
        padding: 10px;
        margin: 5px 0;
        border-radius: 5px;
    }
    .correct-feedback {
        background-color: #e6ffe6; /* Light green */
        border-left: 5px solid green;
    }
    .incorrect-feedback {
        background-color: #ffe6e6; /* Light red */
        border-left: 5px solid red;
    }
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
        """
        Creates the database table if it doesn't exist and handles schema migration.
        """
        conn = self.connect()
        c = conn.cursor()
        
        # 1. CREATE TABLE IF NOT EXISTS
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

        # 2. SCHEMA MIGRATION: Check and add columns safely
        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                # Add column if missing
                try:
                    c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} TEXT DEFAULT '{{}}'")
                except:
                    pass
        
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text, practice_data="{}", analogy_data="{}", exam_analysis="{}"):
        """Saves a new project or updates an existing one."""
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data, analogy_data, exam_analysis))
        conn.commit()
        conn.close()

    def update_project_json_field(self, name, field_name, key, content):
        """Updates a specific key within a JSON field."""
        project_data = self.get_project_details(name)
        if not project_data or field_name not in project_data:
            return

        data_dict = json.loads(project_data.get(field_name) or "{}")
        data_dict[key] = content
        
        conn = self.connect()
        c = conn.cursor()
        c.execute(f'''
            UPDATE projects SET {field_name} = ? WHERE name = ?
        ''', (json.dumps(data_dict), name))
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
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis FROM projects WHERE name=?", (name,))
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
            if concept not in tracker:
                tracker[concept] = {"correct": 0, "total": 0}
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

db = StudyDB() # Initialize DB

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
if 'last_uploaded_exam_id' not in st.session_state: st.session_state.last_uploaded_exam_id = None
if 'weak_topics' not in st.session_state: st.session_state.weak_topics = []
if 'focus_quiz_active' not in st.session_state: st.session_state.focus_quiz_active = False


# --- HELPER FUNCTION FOR ROBUST JSON PARSING ---
def safe_json_parse(json_str):
    if not json_str: return None
    try:
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1: return json.loads(json_str.strip())
        clean_json_str = json_str[start_index:end_index + 1]
        if clean_json_str.startswith('```json'): clean_json_str = clean_json_str[len('```json'):].strip()
        if clean_json_str.endswith('```'): clean_json_str = clean_json_str[:-len('```')].strip()
        return json.loads(clean_json_str)
    except: return None

# --- VISION & PDF PROCESSING ---

def pdf_page_to_base64(page):
    """Converts a PyMuPDF page to a base64 encoded PNG image."""
    pix = page.get_pixmap()
    img_bytes = pix.tobytes("png")
    return base64.b64encode(img_bytes).decode('utf-8')

def extract_content_smart(uploaded_file):
    """
    Intelligently extracts content. 
    Returns: (text_content, images_list_b64)
    If text is sufficient, images_list_b64 is None.
    If text is sparse (scan), text_content is empty string and images_list_b64 is populated.
    """
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_text = ""
    
    # Heuristic: Check density of first few pages
    total_text_len = 0
    check_limit = min(3, len(doc))
    for i in range(check_limit):
        total_text_len += len(doc[i].get_text())
    
    is_scanned = (total_text_len / check_limit < 50) if check_limit > 0 else True
    
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

# --- LLM Functions ---

def get_system_prompt(level):
    if level == "Basic":
        return """Act as a Tutor. GOAL: Pass the exam. Focus on definitions, brevity, and outlines. Output strictly Markdown. If you see text describing a diagram, use an 

[Image of X]
 tag where X is a detailed description."""
    elif level == "Intermediate":
        return """Act as a Professor. GOAL: Solid understanding. Use detailed definitions, process steps, and exam tips. Output strictly Markdown. Insert 

[Image of X]
 tags frequently where X is a detailed description of a relevant diagram."""
    else: # Advanced
        return """Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Output strictly Markdown. Insert  tags for every concept that would be better understood with a visual aid."""

def _attempt_quiz_generation(system_prompt, notes_truncated, client):
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate 10 questions in strict JSON format based on these notes: {notes_truncated}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.8 
        )
        return completion.choices[0].message.content
    except Exception as e:
        if 'invalid_api_key' in str(e):
             st.error("❌ API Key Error: Your Groq API key is invalid or expired.")
        return None

def generate_interactive_drills(notes, client):
    system_prompt = """You are a quiz master. Generate a quiz with 10 questions total (5 MCQ, 5 T/F).
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
    return _attempt_quiz_generation(system_prompt, notes[:15000], client)

def generate_focused_drills(notes, weak_topics, client):
    topics_list_str = ", ".join(weak_topics)
    system_prompt = f"""You are an ADAPTIVE quiz master. Generate 10 questions focusing ONLY on: {topics_list_str}.
    JSON Format MUST be:
    {
      "quiz_title": "Adaptive Focus Drill",
      "questions": [
        {
          "id": 1,
          "type": "MCQ", 
          "question_text": "...",
          "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "primary_concept": "Exact Match From List", 
          "detailed_explanation": "..."
        }
      ]
    }
    """
    return _attempt_quiz_generation(system_prompt, notes[:15000], client)

def generate_study_notes(raw_text, level, client):
    prompt = f"""{get_system_prompt(level)}\nCONTENT: {raw_text[:25000]}\nOutput strictly Markdown."""
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3)
        return completion.choices[0].message.content
    except Exception as e:
        return f"(Error: {e})"

def generate_analogies(notes, client):
    system_prompt = """Identify 5 key concepts. For each, provide a real-life analogy. Format: '**[Concept]**' followed by 'Analogy: [Analogy]'."""
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": notes[:10000]}])
        return completion.choices[0].message.content
    except: return "Error generating analogies."

def generate_specific_analogy(topic, client):
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Give a detailed analogy for: {topic}"}])
        return completion.choices[0].message.content
    except: return "Error."

def generate_qna(notes, q_type, marks, client):
    q_type_text = f"questions worth {marks} marks" if q_type == "custom" else f"{q_type} answer questions"
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": f"Generate {q_type_text} based on: {notes[:15000]}"}])
        return completion.choices[0].message.content
    except: return "Error."

def analyze_exam_paper(text_content, image_content_list, client):
    """
    Analyzes exam paper. Handles both text content and list of base64 images.
    """
    system_prompt_text = """
    You are an expert exam strategist. Analyze the provided exam paper content.
    Output a structured Markdown report with:
    1. 🚨 **High-Priority Topics:** (Top 3-5 themes).
    2. 📝 **Question Patterns:** (Definitions vs Problems? Repeated questions?).
    3. 🎯 **Strategic Advice:** (How to study efficiently).
    """
    
    if image_content_list:
        # --- VISION MODEL PATH ---
        images_payload = []
        for img_b64 in image_content_list: 
            images_payload.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })
        
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
        # --- TEXT MODEL PATH ---
        try:
            with st.spinner("Analyzing exam text..."):
                completion = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt_text},
                        {"role": "user", "content": f"Paper Content: {text_content[:20000]}"}
                    ]
                )
            return completion.choices[0].message.content
        except Exception as e:
            return f"Error analyzing text paper: {e}"


# --- UI INTERACTIVE LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    concept_scores = {} 
    valid_questions = [q for q in questions if 'id' in q and 'primary_concept' in q]

    for q in valid_questions:
        concept = q.get('primary_concept', 'General') 
        user_answer = user_answers.get(q['id'])
        correct_answer = q['correct_answer']

        is_correct = False
        if user_answer:
            if q['type'] == 'MCQ': is_correct = (user_answer == correct_answer)
            elif q['type'] == 'T/F': is_correct = (str(user_answer).strip() == str(correct_answer).strip())
        
        if concept not in concept_scores: concept_scores[concept] = [0, 0]
        concept_scores[concept][1] += 1 
        if is_correct: concept_scores[concept][0] += 1 
    
    db_scores = {k: tuple(v) for k, v in concept_scores.items()}
    db.update_progress_tracker(project_name, db_scores)
    return db_scores

def display_and_grade_quiz(project_name, quiz_json_str):
    quiz_data = safe_json_parse(quiz_json_str)
    if quiz_data is None:
        st.warning("Quiz data error.")
        return

    questions = quiz_data.get('questions', [])
    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Interactive Quiz')}")
    user_answers = st.session_state.user_answers
    
    with st.form(key='quiz_form'):
        valid_questions = [q for q in questions if 'id' in q]
        for i, q in enumerate(valid_questions):
            q_id = q['id']
            st.markdown(f"**{i+1}. {q.get('primary_concept', '')}:** {q['question_text']}")
            
            if q['type'] == 'MCQ':
                options_display = [opt.split(': ')[1] if ': ' in opt else opt for opt in q['options']]
                user_answers[q_id] = st.radio("Answer:", options_display, key=f"q_{q_id}", index=None, disabled=st.session_state.quiz_submitted)
                # Map back to A, B, C, D for checking
                if user_answers[q_id]:
                    try:
                        idx = options_display.index(user_answers[q_id])
                        user_answers[q_id] = ['A','B','C','D'][idx]
                    except: pass
            else:
                user_answers[q_id] = st.radio("Answer:", ["True", "False"], key=f"q_{q_id}", index=None, disabled=st.session_state.quiz_submitted)

            # Feedback
            if st.session_state.quiz_submitted:
                correct = q['correct_answer']
                user_choice = user_answers.get(q_id)
                is_right = False
                if q['type'] == 'MCQ': is_right = (user_choice == correct)
                else: is_right = (str(user_choice) == str(correct))
                
                if is_right: st.markdown(f'<div class="feedback-box correct-feedback"><p class="correct">✅ Correct!</p></div>', unsafe_allow_html=True)
                else: st.markdown(f'<div class="feedback-box incorrect-feedback"><p class="incorrect">❌ Incorrect. Answer: {correct}.</p><p>{q.get("detailed_explanation","")}</p></div>', unsafe_allow_html=True)
            st.markdown("---")

        if st.form_submit_button("Submit Quiz", disabled=st.session_state.quiz_submitted):
            process_and_update_progress(project_name, valid_questions, user_answers)
            st.session_state.quiz_submitted = True
            st.session_state.user_answers = user_answers
            st.rerun()

    if st.button("🔄 Reset Quiz"):
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()


# --- SIDEBAR ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    st.markdown("---")
    
    # API KEY
    final_api_key = None
    if "GROQ_API_KEY" in st.secrets: final_api_key = st.secrets["GROQ_API_KEY"]
    if not final_api_key and st.session_state.groq_api_key: final_api_key = st.session_state.groq_api_key
    
    with st.expander("⚙️ Settings", expanded=not bool(final_api_key)):
        key_input = st.text_input("Groq API Key", type="password", value=final_api_key or "")
        if key_input: st.session_state.groq_api_key = key_input

    st.markdown("---")
    saved_projects = db.load_all_projects()
    if saved_projects:
        st.subheader("📁 Saved Projects")
        for p in saved_projects:
            if st.button(f"📄 {p}", key=f"btn_{p}", use_container_width=True):
                st.session_state.current_project = p
                st.session_state.quiz_submitted = False
                st.session_state.quiz_data = None
                st.session_state.exam_analysis_text = None
                st.session_state.weak_topics = []
                st.rerun()
    
    if st.button("➕ Create New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN APP LOGIC ---

if not st.session_state.groq_api_key:
    st.warning("🚨 Please configure your Groq API Key in the sidebar.")
    st.stop()
    
client = Groq(api_key=st.session_state.groq_api_key)

# VIEW 1: CREATE NEW PROJECT
if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    uploaded_file = st.file_uploader("Upload PDF Document", type="pdf")
    
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1: project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2: level = st.select_slider("Detail Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
        
        if st.button("✨ Create & Generate Study Guide", type="primary"):
            with st.spinner("Extracting content..."):
                # Use smart extraction for main notes too, but we usually just want text for notes
                # If scanned, this will fail for notes generation unless we used vision for notes (expensive).
                # Fallback: Just try text extraction.
                raw_text, _ = extract_content_smart(uploaded_file)
            
            if len(raw_text) > 50:
                with st.spinner("Synthesizing notes..."):
                    notes = generate_study_notes(raw_text, level, client)
                    analogies = generate_analogies(notes, client)
                    db.save_project(project_name, level, notes, raw_text, analogy_data=json.dumps({"default": analogies}))
                    st.session_state.current_project = project_name
                    st.rerun()
            else:
                st.error("⚠️ Document text is too sparse. This appears to be a scanned file. Currently, main study note generation requires searchable text, though Exam Analysis supports scans.")

# VIEW 2: DASHBOARD
else:
    project_data = db.get_project_details(st.session_state.current_project)
    if project_data:
        practice_data = json.loads(project_data.get('practice_data') or "{}")
        analogy_data = json.loads(project_data.get('analogy_data') or "{}")

        st.title(f"📘 {project_data['name']}")
        
        tab1, tab_analogy, tab_exam, tab2, tab3 = st.tabs(["📖 Notes", "💡 Analogies", "📈 Exam Analysis", "🧠 Practice", "📊 Progress"])
        
        # --- TAB 1: NOTES ---
        with tab1:
            st.markdown(project_data['notes'])

        # --- TAB 2: ANALOGIES ---
        with tab_analogy:
            st.subheader("Concept Analogies")
            st.markdown(analogy_data.get('default', ""))
            if st.button("Refresh Analogies"):
                new_a = generate_analogies(project_data['notes'], client)
                db.update_analogy_data(project_data['name'], "default", new_a)
                st.rerun()
                
            st.divider()
            t_req = st.text_input("Request specific analogy:")
            if st.button("Generate") and t_req:
                res = generate_specific_analogy(t_req, client)
                st.markdown(res)

        # --- TAB 3: EXAM ANALYSIS (VISION ENABLED) ---
        with tab_exam:
            st.header("📈 Exam Paper Analysis")
            st.info("Upload a past paper. **Supports both text PDFs and Scanned Images!**")
            
            uploaded_pdf = st.file_uploader("Upload Past Paper PDF", type="pdf", key="exam_pdf_uploader")
            
            if uploaded_pdf:
                # Cache content to prevent re-reading on every rerun
                if st.session_state.last_uploaded_exam_id != uploaded_pdf.file_id:
                    with st.spinner("Reading PDF..."):
                        txt, imgs = extract_content_smart(uploaded_pdf)
                        st.session_state.exam_analysis_content_cache = (txt, imgs)
                        # --- FIX: USE CORRECT VARIABLE NAME 'uploaded_pdf' ---
                        st.session_state.last_uploaded_exam_id = uploaded_pdf.file_id 
                
                txt_content, img_content = st.session_state.exam_analysis_content_cache
                
                if img_content:
                    st.warning("📷 **Scanned Document Detected.** Using Vision Model (this may take 10-20s).")
                else:
                    st.success("📄 **Text Document Detected.** Using Standard Model.")

                if st.button("🎯 Run Analysis", type="primary"):
                    analysis = analyze_exam_paper(txt_content, img_content, client)
                    db.update_exam_analysis_data(project_data['name'], "latest", analysis)
                    st.session_state.exam_analysis_text = analysis
                    st.rerun()

            st.divider()
            analysis_disp = st.session_state.exam_analysis_text or json.loads(project_data.get('exam_analysis') or "{}").get('latest')
            if analysis_disp:
                st.markdown(analysis_disp)

        # --- TAB 4: PRACTICE ---
        with tab2:
            st.subheader("Interactive Quizzes")
            
            col_gen, col_focus = st.columns(2)
            with col_gen:
                if st.button("Generate General Quiz", use_container_width=True):
                    q = generate_interactive_drills(project_data['notes'], client)
                    if q:
                        db.update_practice_data(project_data['name'], "interactive_quiz_current", q)
                        st.session_state.quiz_data = q
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()
            
            with col_focus:
                weak = st.session_state.weak_topics
                if st.button(f"Generate Focus Quiz ({len(weak)} topics)", disabled=not weak, use_container_width=True):
                    q = generate_focused_drills(project_data['notes'], weak, client)
                    if q:
                        db.update_practice_data(project_data['name'], "interactive_quiz_current", q)
                        st.session_state.quiz_data = q
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()

            current_quiz = st.session_state.quiz_data or practice_data.get('interactive_quiz_current')
            if current_quiz:
                st.divider()
                display_and_grade_quiz(project_data['name'], current_quiz)
        
        # --- TAB 5: PROGRESS ---
        with tab3:
            st.header("📊 Progress Tracker")
            tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            
            if st.button("⚠️ Clear Progress Data"):
                db.reset_progress_tracker(project_data['name'])
                st.rerun()

            progress_list = []
            current_weak = []
            for concept, stats in tracker.items():
                acc = (stats['correct'] / stats['total'] * 100) if stats['total'] > 0 else 0
                status = "🟢 Strong" if acc > 80 else "🔴 Weak Point"
                if acc <= 80: current_weak.append(concept)
                progress_list.append({"Concept": concept, "Accuracy": f"{acc:.1f}%", "Attempts": stats['total'], "Status": status})
            
            st.session_state.weak_topics = current_weak
            if progress_list:
                st.dataframe(progress_list, use_container_width=True)
            else:
                st.info("Take a quiz to see stats!")
