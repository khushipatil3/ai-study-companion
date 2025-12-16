import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64 

# --- MODEL CONSTANT ---
GROQ_MODEL = "llama-3.1-8b-instant" 

# --- CONFIGURABLE THRESHOLDS ---
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3          # Used for 'Low Data' message

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
        # Schema migration checks
        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
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
            # Map index to column name based on CREATE TABLE order
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
if 'exam_analysis_pdf_content' not in st.session_state: st.session_state.exam_analysis_pdf_content = ""
if 'last_uploaded_exam_pdf_id' not in st.session_state: st.session_state.last_uploaded_exam_pdf_id = None
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
    except Exception: return None

# --- LLM Functions ---

def get_system_prompt(level):
    base = "Act as a Tutor. Output strictly Markdown."
    if level == "Intermediate": base = "Act as a Professor. Use detailed definitions."
    elif level == "Advanced": base = "Act as a Subject Matter Expert. Explain nuances."
    # Force single line string return
    return f"{base} Insert
 tags for visual aids."

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
        if 'invalid_api_key' in str(e): st.error("❌ API Key Error: Check your settings.")
        elif 'context_length' in str(e): st.error("❌ Context Length Error: Notes too long.")
        else: st.error(f"Generation Error: {e}")
        return None

# --- NEW: SYLLABUS GENERATOR FOR CONSISTENCY ---
def generate_syllabus(notes, client):
    """Generates a master list of standard concepts to enforce naming consistency."""
    system_prompt = """You are a curriculum designer. Analyze the provided notes and extract a list of 20-30 distinct, high-level, canonical topic names (e.g. 'Recursion', 'Memory Management', 'Linear Regression'). 
    Avoid variations (do NOT list 'Recursion' and 'Recursive Functions' separately).
    Output strictly a JSON object with a single key 'topics' containing the list of strings.
    Example: {"topics": ["Concept A", "Concept B", ...]}
    """
    notes_truncated = notes[:15000]
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Extract the canonical syllabus topics from these notes: {notes_truncated}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.3 
        )
        data = safe_json_parse(completion.choices[0].message.content)
        return data.get('topics', []) if data else []
    except Exception as e:
        st.error(f"Error generating syllabus: {e}")
        return []

def generate_interactive_drills(notes, syllabus_list, client):
    """Generates general practice drills using the MASTER SYLLABUS for consistency."""
    
    syllabus_str = ", ".join(syllabus_list) if syllabus_list else "General Topics"
    
    system_prompt = f"""You are a quiz master. Generate a quiz with 10 questions (5 MCQ, 5 T/F).
    
    *** CRITICAL INSTRUCTION ***
    For every question, the 'primary_concept' field MUST be exactly one of the strings from this APPROVED SYLLABUS LIST: 
    [{syllabus_str}]
    
    Do NOT invent new topic names. Do NOT use synonyms. You MUST pick from the list provided.
    
    JSON Format MUST be:
    {{
      "quiz_title": "Interactive Practice Drill (General)",
      "questions": [
        {{
          "id": 1, "type": "MCQ", "question_text": "...", "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "primary_concept": "EXACT_TERM_FROM_SYLLABUS", 
          "detailed_explanation": "..."
        }}
      ]
    }}
    """
    notes_truncated = notes[:15000]
    with st.spinner("Generating consistent practice drills..."):
        return _attempt_quiz_generation(system_prompt, notes_truncated, client)

def generate_focused_drills(notes, weak_topics, client):
    topics_list_str = ", ".join(weak_topics)
    system_prompt = f"""You are an ADAPTIVE quiz master. Generate 10 questions testing ONLY these WEAK TOPICS: {topics_list_str}
    
    The 'primary_concept' for each question MUST be an exact match from the list: {topics_list_str}.
    
    JSON Format MUST be:
    {{
      "quiz_title": "Adaptive Focus Drill",
      "questions": [
        {{
          "id": 1, "type": "MCQ", "question_text": "...", "options": ["..."], "correct_answer": "...",
          "primary_concept": "{weak_topics[0] if weak_topics else 'Concept'}",
          "detailed_explanation": "..."
        }}
      ]
    }}
    """
    notes_truncated = notes[:15000]
    with st.spinner(f"Generating FOCUS drills on: {topics_list_str}..."):
        return _attempt_quiz_generation(system_prompt, notes_truncated, client)

def generate_study_notes(raw_text, level, client):
    pages = raw_text.split("--- PAGE_BREAK ---")
    pages = [p for p in pages if len(p.strip()) > 50]
    batch_size = 15 
    batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
    final_notes = f"# 📘 {level} Study Guide\n\n"
    
    status_text = st.empty()
    bar = st.progress(0)
    for i, batch in enumerate(batches):
        bar.progress((i + 1) / len(batches))
        status_text.caption(f"🧠 Synthesizing Batch {i+1}/{len(batches)}...")
        prompt = f"""{get_system_prompt(level)}\nCONTENT: {"\n".join(batch)}\nOutput strictly Markdown."""
        try:
            completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3)
            final_notes += completion.choices[0].message.content + "\n\n---\n\n"
        except Exception as e:
            final_notes += f"(Error: {e})\n\n---\n\n"
    status_text.empty()
    bar.empty()
    return final_notes

def generate_analogies(notes, client):
    system_prompt = "Identify 5 key concepts and provide real-life analogies. Format as Markdown list: '**[Concept]** Analogy: ...'"
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Notes: {notes[:10000]}"}], temperature=0.7)
        return completion.choices[0].message.content
    except Exception: return "Error generating analogies."

def generate_specific_analogy(topic, client):
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": "Provide a detailed analogy."}, {"role": "user", "content": f"Analogy for: {topic}"}], temperature=0.6)
        return completion.choices[0].message.content
    except Exception: return "Error."

def generate_qna(notes, q_type, marks, client):
    desc = "short answers" if q_type == "short" else "long answers"
    if q_type == "custom": desc = f"{marks} marks questions"
    try:
        completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": f"Generate {desc}. Markdown only."}, {"role": "user", "content": f"Notes: {notes[:15000]}"}], temperature=0.5)
        return completion.choices[0].message.content
    except Exception: return "Error generating Q&A."
        
def analyze_past_papers(paper_content, client):
    system_prompt = """Analyze the exam questions. Output Markdown: 1. Top 5 Topics, 2. Repeated Themes, 3. Strategy. No answers."""
    try:
        with st.spinner("Analyzing past papers..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Content: {paper_content[:15000]}"}],
                temperature=0.4
            )
        return completion.choices[0].message.content
    except Exception as e: return f"Error: {e}"

# --- UI INTERACTIVE LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    concept_scores = {} 
    valid_questions = [q for q in questions if isinstance(q, dict) and 'id' in q and 'primary_concept' in q]

    for q in valid_questions:
        q_id = q['id']
        concept = q.get('primary_concept', 'General') 
        user_answer = user_answers.get(q_id)
        correct_answer = q['correct_answer']

        is_correct = False
        if user_answer:
            if q['type'] == 'MCQ': is_correct = (user_answer == correct_answer)
            elif q['type'] == 'T/F': is_correct = (user_answer.strip() == correct_answer.strip())
        
        if concept not in concept_scores: concept_scores[concept] = [0, 0]
        concept_scores[concept][1] += 1 
        if is_correct: concept_scores[concept][0] += 1 
    
    db_scores = {k: tuple(v) for k, v in concept_scores.items()}
    db.update_progress_tracker(project_name, db_scores)
    return db_scores

def display_and_grade_quiz(project_name, quiz_json_str):
    quiz_data = safe_json_parse(quiz_json_str)
    if not quiz_data:
        st.warning("Quiz data corrupted.")
        return

    questions = quiz_data.get('questions', [])
    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Quiz')} ({st.session_state.quiz_type.capitalize()})")
    
    user_answers = st.session_state.user_answers
    with st.form(key='quiz_form'):
        valid_questions = [q for q in questions if 'options' in q and 'id' in q]
        
        for q_index, q in enumerate(valid_questions):
            q_id = q['id']
            q_num = q_index + 1
            question_key = f"q_{q_id}"
            
            st.markdown(f"**Q{q_num} ({q.get('primary_concept')}):** {q['question_text']}")
            
            options = q['options'] 
            user_choice = None
            
            if q['type'] == 'T/F':
                options_display = ["True", "False"] 
                default_idx = options_display.index(user_answers.get(q_id)) if user_answers.get(q_id) in options_display else None
                user_choice = st.radio("Answer:", options_display, key=question_key, index=default_idx, disabled=st.session_state.quiz_submitted, label_visibility="collapsed")
            
            elif q['type'] == 'MCQ':
                options_display = [opt.split(': ')[1] if ': ' in opt else opt for opt in options] 
                stored_val = user_answers.get(q_id)
                default_idx = ['A','B','C','D'].index(stored_val) if stored_val in ['A','B','C','D'] else None
                
                choice_text = st.radio("Answer:", options_display, key=question_key, index=default_idx, disabled=st.session_state.quiz_submitted, label_visibility="collapsed")
                if choice_text:
                     try:
                        idx = options_display.index(choice_text)
                        user_choice = ['A','B','C','D'][idx]
                     except: pass

            user_answers[q_id] = user_choice
            
            if st.session_state.quiz_submitted:
                correct = q['correct_answer']
                is_correct = (user_choice == correct) if q['type'] == 'MCQ' else (str(user_choice) == str(correct))
                
                if is_correct:
                    st.success("✅ Correct!")
                else:
                    st.error(f"❌ Incorrect. Answer: {correct}")
                    st.caption(f"💡 {q.get('detailed_explanation')}")
            st.markdown("---")

        c1, c2 = st.columns([1, 6])
        with c1: submit = st.form_submit_button("✅ Submit", type="primary", disabled=st.session_state.quiz_submitted)
        with c2: reset = st.form_submit_button("🔄 Reset", type="secondary")

    if submit:
        process_and_update_progress(project_name, valid_questions, user_answers)
        st.session_state.quiz_submitted = True
        st.session_state.user_answers = user_answers 
        st.rerun() 
        
    if reset:
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

def extract_content_text_only(uploaded_file):
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    text = ""
    for page in doc:
        try: text += f"\n--- PAGE_BREAK ---\n{page.get_text('text')}\n"
        except: pass
    return text

# --- SIDEBAR ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    
    final_api_key = st.secrets.get("GROQ_API_KEY")
    if not final_api_key and st.session_state.groq_api_key: final_api_key = st.session_state.groq_api_key
    
    with st.expander("⚙️ Settings", expanded=not bool(final_api_key)):
        key_input = st.text_input("Groq API Key", type="password", value=final_api_key or "")
        if key_input != st.session_state.groq_api_key:
            st.session_state.groq_api_key = key_input
            st.rerun()

    st.divider()
    saved_projects = db.load_all_projects()
    if saved_projects:
        st.subheader("Projects")
        for p in saved_projects:
            if st.button(f"📄 {p}", key=f"btn_{p}", use_container_width=True):
                st.session_state.current_project = p
                st.session_state.quiz_submitted = False 
                st.session_state.user_answers = {} 
                st.session_state.quiz_data = None 
                st.session_state.quiz_type = 'general' 
                st.session_state.weak_topics = []
                st.session_state.focus_quiz_active = False
                st.rerun()
                
    if st.button("➕ New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN LOGIC ---

if not final_api_key:
    st.warning("Please configure API Key.")
    st.stop()

client = Groq(api_key=final_api_key)

if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    uploaded_file = st.file_uploader("Upload PDF", type="pdf")
    
    if uploaded_file:
        c1, c2 = st.columns(2)
        p_name = c1.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        level = c2.select_slider("Level", ["Basic", "Intermediate", "Advanced"], value="Intermediate")
        
        if st.button("✨ Generate Study Guide"):
            with st.spinner("Extracting & Synthesizing..."):
                raw_text = extract_content_text_only(uploaded_file)
                if len(raw_text) > 50:
                    notes = generate_study_notes(raw_text, level, client)
                    
                    # --- NEW: Generate Syllabus immediately ---
                    syllabus_list = generate_syllabus(notes, client)
                    practice_init = json.dumps({"syllabus": syllabus_list}) # Store syllabus in practice_data
                    
                    default_analogies = generate_analogies(notes, client)
                    analogy_data = json.dumps({"default": default_analogies})
                    
                    db.save_project(p_name, level, notes, raw_text, practice_data=practice_init, analogy_data=analogy_data)
                    st.session_state.current_project = p_name
                    st.rerun()
                else: st.error("Text extraction failed.")

else:
    project_data = db.get_project_details(st.session_state.current_project)
    if project_data:
        practice_data = json.loads(project_data.get('practice_data') or "{}")
        analogy_data = json.loads(project_data.get('analogy_data') or "{}")
        
        # --- NEW: Self-Healing Syllabus Check ---
        # If an old project doesn't have a syllabus, generate it now.
        syllabus_list = practice_data.get('syllabus')
        if not syllabus_list:
            with st.spinner("⚙️ Optimizing project for consistency (One-time setup)..."):
                syllabus_list = generate_syllabus(project_data['notes'], client)
                db.update_practice_data(project_data['name'], "syllabus", syllabus_list)
                st.rerun()

        st.title(f"📘 {project_data['name']}")
        
        tab1, tab_analogy, tab_exam, tab2, tab3 = st.tabs(["📖 Notes", "💡 Analogies", "📈 Exam Analysis", "🧠 Practice", "📊 Progress"])
        
        with tab1: st.markdown(project_data['notes'])
        
        with tab_analogy:
            st.markdown(analogy_data.get('default', ""))
            topic = st.text_input("Request Analogy for:")
            if st.button("Generate Analogy") and topic:
                res = generate_specific_analogy(topic, client)
                st.markdown(res)

        with tab_exam:
            st.markdown("### Upload Past Paper for Analysis")
            exam_pdf = st.file_uploader("Upload Exam PDF", type="pdf", key="exam_up")
            if exam_pdf and st.button("Run Analysis"):
                txt = extract_content_text_only(exam_pdf)
                res = analyze_past_papers(txt, client)
                db.update_exam_analysis_data(project_data['name'], "latest", res)
                st.session_state.exam_analysis_text = res
                st.rerun()
            
            stored_analysis = json.loads(project_data.get('exam_analysis') or "{}").get('latest')
            if stored_analysis: st.markdown(stored_analysis)

        with tab2: # PRACTICE TAB
            st.subheader("Interactive Quiz")
            weak_topics = st.session_state.weak_topics
            
            # --- Check for corrupted data in weak_topics ---
            is_corrupted = any(len(t) > 50 for t in weak_topics)
            
            if is_corrupted:
                st.error("🚨 Corrupted Data Detected in Progress Tracker. Please go to 'Progress' tab and Clear Data.")
            else:
                c_focus, c_gen = st.columns(2)
                
                # FOCUS BUTTON
                disable_focus = not weak_topics and not st.session_state.focus_quiz_active
                if c_focus.button(f"Focus Quiz ({len(weak_topics)} Weak Topics)", disabled=disable_focus, use_container_width=True):
                    st.session_state.quiz_type = 'focused'
                    st.session_state.focus_quiz_active = True
                    st.session_state.quiz_data = None
                    st.session_state.quiz_submitted = False
                    st.session_state.user_answers = {}
                    st.rerun()

                # GENERAL BUTTON
                if c_gen.button("General Quiz (Mixed Topics)", disabled=st.session_state.focus_quiz_active, use_container_width=True):
                    st.session_state.quiz_type = 'general'
                    st.session_state.focus_quiz_active = False
                    st.session_state.quiz_data = None
                    st.session_state.quiz_submitted = False
                    st.session_state.user_answers = {}
                    st.rerun()

                # GENERATE ACTIONS
                if st.session_state.quiz_type == 'focused' and (weak_topics or st.session_state.focus_quiz_active):
                     if st.button("Generate New Focus Quiz"):
                        q_content = generate_focused_drills(project_data['notes'], weak_topics, client)
                        if q_content:
                            db.update_practice_data(project_data['name'], "interactive_quiz_current", q_content)
                            st.session_state.quiz_data = q_content
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()
                            
                elif st.session_state.quiz_type == 'general':
                    if st.button("Generate New General Quiz"):
                        # *** UPDATED CALL WITH SYLLABUS ***
                        q_content = generate_interactive_drills(project_data['notes'], syllabus_list, client)
                        if q_content:
                            db.update_practice_data(project_data['name'], "interactive_quiz_current", q_content)
                            st.session_state.quiz_data = q_content
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()

                # DISPLAY QUIZ
                current_quiz = st.session_state.quiz_data or practice_data.get('interactive_quiz_current')
                if current_quiz:
                    display_and_grade_quiz(project_data['name'], current_quiz)

        with tab3: # PROGRESS TAB
            st.header("📊 Progress Tracker")
            if st.button("⚠️ Clear Progress Data (Fix Corruption)", type="secondary"):
                db.reset_progress_tracker(project_data['name'])
                st.session_state.weak_topics = []
                st.session_state.focus_quiz_active = False
                st.success("Tracker reset.")
                st.rerun()
            
            tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            if tracker:
                data = []
                current_weak = []
                for k, v in tracker.items():
                    acc = (v['correct']/v['total'])*100
                    status = "Good"
                    if acc < 80: 
                        status = "Weak 🚨"
                        current_weak.append(k)
                    if len(k) > 50: status = "CORRUPTED"
                    
                    data.append({"Topic": k, "Accuracy": f"{acc:.1f}%", "Attempts": v['total'], "Status": status})
                
                st.session_state.weak_topics = current_weak
                st.dataframe(data, use_container_width=True)
            else:
                st.info("No practice data yet.")
