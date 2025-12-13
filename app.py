import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64
import sqlite3
import json
import re
from datetime import date, timedelta

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .reportview-container { margin-top: -2em; }
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] { height: 50px; white-space: pre-wrap; background-color: #f0f2f6; border-radius: 4px 4px 0px 0px; gap: 1px; padding-top: 10px; padding-bottom: 10px; }
    .stTabs [aria-selected="true"] { background-color: #ffffff; }
    
    /* Quiz Recap Box */
    .recap-box {
        background-color: #e8f4f8;
        padding: 15px;
        border-radius: 5px;
        border-left: 5px solid #00aaff;
        margin-top: 5px;
        margin-bottom: 20px;
        font-size: 0.95em;
    }
    .analogy-box {
        background-color: #fffacd;
        padding: 15px;
        border-radius: 5px;
        border-left: 5px solid #daa520;
        margin-top: 15px;
        margin-bottom: 10px;
        font-size: 0.95em;
    }
    .confidence-low {
        color: #ff4b4b; /* Red */
        font-weight: bold;
    }
    .confidence-high {
        color: #26b27e; /* Green */
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

# --- DATABASE LAYER ---
def init_db():
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS projects (
        name TEXT PRIMARY KEY, level TEXT, notes TEXT, raw_text TEXT, progress INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS quiz_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT, topic_tag TEXT, question_type TEXT, is_correct INTEGER, confidence TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # New table for Spaced Repetition (SRS) tracking
    c.execute('''
        CREATE TABLE IF NOT EXISTS srs_schedule (
            project_name TEXT,
            topic_tag TEXT,
            next_review DATE,
            interval_days INTEGER,
            FOREIGN KEY(project_name) REFERENCES projects(name),
            PRIMARY KEY (project_name, topic_tag)
        )
    ''')
    conn.commit()
    conn.close()

def save_project_to_db(name, level, notes, raw_text):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress) VALUES (?, ?, ?, ?, ?)', 
              (name, level, notes, raw_text, 0))
    conn.commit()
    conn.close()

def log_quiz_result(project_name, topic, q_type, correct, confidence):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('INSERT INTO quiz_performance (project_name, topic_tag, question_type, is_correct, confidence) VALUES (?, ?, ?, ?, ?)',
              (project_name, topic, q_type, 1 if correct else 0, confidence))
    
    # --- SRS LOGIC ---
    current_date = date.today()
    interval_days = 0
    
    # Check current interval or set default
    existing_interval = c.execute('SELECT interval_days FROM srs_schedule WHERE project_name=? AND topic_tag=?', (project_name, topic)).fetchone()
    if existing_interval:
        interval_days = existing_interval[0]
    
    # Update Interval based on performance
    if correct:
        if confidence == 'High': # Confident & Right = Mastered, wait longer
            new_interval = max(interval_days * 2, 5) # Double the interval, minimum 5 days
        else: # Unsure but Right = Solid, small increase
            new_interval = max(interval_days + 1, 2) # Add 1 day, minimum 2 days
    else: # Incorrect = Reset or shorten interval
        new_interval = 1 # Review tomorrow
        
    next_review = current_date + timedelta(days=new_interval)
    
    c.execute('''
        INSERT OR REPLACE INTO srs_schedule (project_name, topic_tag, next_review, interval_days)
        VALUES (?, ?, ?, ?)
    ''', (project_name, topic, next_review.isoformat(), new_interval))
    
    conn.commit()
    conn.close()

def get_topics_for_review(project_name):
    """Finds topics that are due for review today or have low accuracy."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    today = date.today().isoformat()
    
    # 1. Topics due for review (based on SRS schedule)
    srs_topics = c.execute('''
        SELECT topic_tag FROM srs_schedule
        WHERE project_name = ? AND next_review <= ?
    ''', (project_name, today)).fetchall()
    
    # 2. Topics with lowest overall accuracy (less than 60%)
    weak_topics = c.execute('''
        SELECT topic_tag FROM quiz_performance
        WHERE project_name = ?
        GROUP BY topic_tag
        HAVING AVG(is_correct) < 0.6
    ''', (project_name,)).fetchall()
    
    conn.close()
    
    # Combine and return unique topics for adaptive quiz generation
    all_topics = set([t[0] for t in srs_topics] + [t[0] for t in weak_topics])
    return list(all_topics)

def get_topic_performance(project_name):
    """Returns weak (<60%) and strong (>60%) topics."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''SELECT topic_tag, AVG(is_correct) as accuracy FROM quiz_performance WHERE project_name = ? GROUP BY topic_tag ORDER BY accuracy ASC''', (project_name,))
    data = c.fetchall()
    conn.close()
    
    weak = [row[0] for row in data if row[1] < 0.6]
    strong = [row[0] for row in data if row[1] >= 0.6]
    return weak, strong

def get_project_details(name):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE name=?", (name,))
    row = c.fetchone()
    conn.close()
    if row: return {"name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3], "progress": row[4]}
    return None

def load_all_projects():
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute("SELECT name FROM projects")
    return [row[0] for row in c.fetchall()]

init_db()

# --- HELPER FUNCTIONS (Extractors and Generators) ---
def encode_image(pix):
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content_with_vision(uploaded_file, client):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    bar = st.progress(0)
    for i, page in enumerate(doc):
        bar.progress((i + 1) / len(doc))
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            img_str = encode_image(pix)
            chat = client.chat.completions.create(
                messages=[{"role": "user", "content": [{"type": "text", "text": "Transcribe page."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}}]}],
                model="meta-llama/llama-4-scout-17b-16e-instruct"
            )
            full_content += f"\n--- PAGE_BREAK ---\n{chat.choices[0].message.content}\n"
        except: full_content += ""
    bar.empty()
    return full_content

def generate_study_notes(raw_text, level, client):
    prompt = f"Create a comprehensive {level} study guide in Markdown based on this content: {raw_text[:25000]}"
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}], temperature=0.3
        )
        return completion.choices[0].message.content
    except: return "Error generating notes."

def generate_analogy(topic_name, client):
    prompt = f"Create a single, simple, real-life analogy or metaphor to help a student understand the concept of '{topic_name}'. Use a common example (like cooking, driving, or organizing). Do not use introductory phrases. Keep it to 2-3 sentences max."
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}], temperature=0.7
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Could not generate analogy: {str(e)}"

def clean_json_string(json_str):
    json_str = json_str.strip()
    if json_str.startswith("```json"): json_str = json_str[7:]
    if json_str.endswith("```"): json_str = json_str[:-3]
    return json_str.strip()

def generate_objective_quiz(raw_text, focus_topics, client):
    if not raw_text or len(raw_text) < 100:
        return {"error": "Text too short. Please re-upload PDF."}

    context_text = raw_text[:6000]
    focus_prompt = f"PRIORITY: Focus strictly on these topics: {', '.join(focus_topics)}" if focus_topics else "Cover general concepts."
    
    prompt = f"""
    Create a JSON object with 5 practice questions (MCQ/TrueFalse) based on the text.
    {focus_prompt}
    
    IMPORTANT: For each question, provide an 'explanation' field (2-sentence recap of the concept).
    
    Format:
    {{
        "questions": [
            {{
                "type": "MCQ", "question": "Question text...", "options": ["A", "B", "C", "D"],
                "correct_option": "A", "topic": "Topic Name", "explanation": "Concept recap here..."
            }}
        ]
    }}
    
    CONTENT: {context_text}
    Output ONLY valid JSON.
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}], temperature=0.5, response_format={"type": "json_object"}
        )
        content = completion.choices[0].message.content
        cleaned_content = clean_json_string(content)
        return json.loads(cleaned_content)
    except Exception as e:
        return {"error": f"Quiz Generation Failed: {str(e)}"}

def generate_theory_questions(raw_text, q_type, marks, num_q, client):
    if not raw_text: return "Error: No text available."

    context_text = raw_text[:6000]
    length_instruction = "Answer in 2-3 sentences." if q_type == "Short" else "Answer in 2 paragraphs."
    if q_type == "Custom": length_instruction = f"These are {marks}-mark questions. Detail matches marks."
    
    prompt = f"""
    Create {num_q} {q_type} Answer Theory Questions based on the text.
    Provide the Ideal Answer for each.
    {length_instruction}
    
    Format:
    ### Q1: [Question]
    **Ideal Answer:** [Answer]
    
    CONTENT: {context_text}
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}], temperature=0.4
        )
        return completion.choices[0].message.content
    except Exception as e: return f"Error generating theory: {str(e)}"

# --- PYQ ANALYZER LOGIC ---
def analyze_pyq_pdf(pyq_file, client):
    """Analyzes the PYQ PDF to find frequent topics."""
    doc = fitz.open(stream=pyq_file.read(), filetype="pdf")
    pyq_text = ""
    for page in doc:
        pyq_text += page.get_text()
    
    prompt = f"""
    Analyze the following past examination questions and identify the top 5 most frequently tested topics or concepts.
    
    Format the output as a Markdown list:
    * Topic 1 (Frequency: High) - Focus on Definition and Application.
    * Topic 2 (Frequency: Medium) - Focus on Examples and Comparison.
    ...
    
    PAST PAPER CONTENT: {pyq_text[:10000]}
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}], temperature=0.2
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error analyzing PYQ: {str(e)}"


# --- MAIN UI ---

if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'api_key' not in st.session_state: st.session_state.api_key = None
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state: st.session_state.user_answers = {}
if 'analogy_result' not in st.session_state: st.session_state.analogy_result = ""
if 'pyq_analysis' not in st.session_state: st.session_state.pyq_analysis = None


with st.sidebar:
    st.title("🗂️ My Library")
    if not st.session_state.api_key:
        key = st.text_input("Groq API Key", type="password")
        if key: st.session_state.api_key = key; st.rerun()
    
    st.divider()
    saved = load_all_projects()
    for p in saved:
        if st.button(f"📄 {p}", use_container_width=True):
            st.session_state.current_project = p
            st.session_state.quiz_data = None 
            st.session_state.quiz_submitted = False
            st.session_state.analogy_result = ""
            st.rerun()
    if st.button("+ New Project"): st.session_state.current_project = None; st.rerun()
    
    if st.session_state.get('current_project'):
        with open("study_db.sqlite", "rb") as f:
            st.download_button("📥 Backup Library", f, "study_db.sqlite")

if not st.session_state.api_key: st.warning("Enter API Key to start."); st.stop()
client = Groq(api_key=st.session_state.api_key)

# PAGE 1: UPLOAD
if st.session_state.current_project is None:
    st.title("🚀 New Project")
    up = st.file_uploader("Upload PDF", type="pdf")
    if up:
        name = st.text_input("Project Name", value=up.name.split('.')[0])
        level = st.select_slider("Level", ["Basic", "Intermediate", "Advanced"], value="Intermediate")
        if st.button("Generate"):
            text = extract_content_with_vision(up, client)
            notes = generate_study_notes(text, level, client)
            save_project_to_db(name, level, notes, text)
            st.session_state.current_project = name
            st.rerun()

# PAGE 2: DASHBOARD
else:
    data = get_project_details(st.session_state.current_project)
    
    if not data['raw_text'] or len(data['raw_text']) < 50:
        st.error("⚠️ Warning: This project has no readable text. Quiz generation will fail.")

    st.header(f"📘 {data['name']}")
    
    tab1, tab2, tab3, tab4 = st.tabs(["📖 Study Notes", "📝 Practice", "📊 Analytics", "🎯 Exam Hacker (PYQ)"])
    
    with tab1:
        st.subheader("📝 Study Guide")
        
        # --- ANALOGY TOOL INTEGRATION ---
        col_a1, col_a2 = st.columns([3, 1])
        with col_a1:
            analogy_topic = st.text_input("Need a simple analogy? Type a concept:", placeholder="e.g., 'Sparse Matrix' or 'Diffusion'")
        with col_a2:
            st.text("")
            if st.button("💡 Get Analogy", use_container_width=True) and analogy_topic:
                with st.spinner(f"Generating analogy for '{analogy_topic}'..."):
                    result = generate_analogy(analogy_topic, client)
                    st.session_state.analogy_result = (analogy_topic, result)
        
        if st.session_state.analogy_result:
            topic, result = st.session_state.analogy_result
            st.markdown(f"""
            <div class="analogy-box">
                <b>Analogy for: {topic}</b><br>
                {result}
            </div>
            """, unsafe_allow_html=True)
            
        st.markdown(data['notes'])
        
    with tab2:
        st.subheader("🎯 Active Practice")
        mode = st.radio("Select Mode:", ["Objective (Interactive)", "Theory (Study Mode)"], horizontal=True)
        
        # Get topics due for review (SRS + Weakness)
        focus_topics = get_topics_for_review(data['name'])

        if mode == "Objective (Interactive)":
            col1, col2 = st.columns([3, 1])
            with col1:
                if focus_topics:
                    st.warning(f"Adaptively focusing on {len(focus_topics)} critical topics due for review.")
                else:
                    st.info("No urgent reviews. Generating general quiz.")

            with col2:
                if st.button("🔄 Generate New Quiz"):
                    with st.spinner("Generating adaptive questions..."):
                        q_data = generate_objective_quiz(data['raw_text'], focus_topics, client)
                        if "error" in q_data: st.error(q_data["error"])
                        else: 
                            st.session_state.quiz_data = q_data
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
            
            # QUIZ DISPLAY LOGIC
            if st.session_state.get('quiz_data') and "questions" in st.session_state.quiz_data:
                qs = st.session_state.quiz_data['questions']
                
                # State 1: Quiz NOT Submitted (Show Form)
                if not st.session_state.quiz_submitted:
                    with st.form("quiz_form"):
                        for i, q in enumerate(qs):
                            st.markdown(f"**Q{i+1}: {q['question']}**")
                            # --- METCOG CONFIDENCE SLIDER (New Feature) ---
                            confidence = st.select_slider(
                                "How sure are you?", 
                                options=['Low', 'Medium', 'High'],
                                key=f"conf_{i}"
                            )
                            st.session_state.user_answers[f'conf_{i}'] = confidence 
                            # ---------------------------------------------

                            if q['type'] == 'MCQ':
                                st.radio("Choose:", q['options'], key=f"q_input_{i}", index=None)
                            else:
                                st.radio("Choose:", ["True", "False"], key=f"q_input_{i}", index=None)
                            st.divider()
                        
                        submitted = st.form_submit_button("Submit Answers")
                        
                        if submitted:
                            for i in range(len(qs)):
                                st.session_state.user_answers[i] = st.session_state.get(f"q_input_{i}")
                            st.session_state.quiz_submitted = True
                            st.rerun()

                # State 2: Quiz SUBMITTED (Show Results Inline)
                else:
                    score = 0
                    for i, q in enumerate(qs):
                        user_ans = st.session_state.user_answers.get(i)
                        confidence = st.session_state.user_answers.get(f'conf_{i}', 'N/A')
                        correct = (user_ans == q['correct_option'])
                        if correct: score += 1
                        
                        # Log with Confidence for SRS
                        log_quiz_result(data['name'], q.get('topic', 'General'), q['type'], correct, confidence)
                        
                        # RENDER RESULT CARD
                        with st.container():
                            st.markdown(f"""<div class="question-box">
                            <b>Q{i+1}: {q['question']}</b><br>
                            Your Answer: {user_ans} | 
                            Confidence: <span class="{'confidence-high' if confidence == 'High' else 'confidence-low'}">{confidence}</span>
                            </div>""", unsafe_allow_html=True)
                            
                            if correct:
                                st.success(f"✅ Correct! The answer is {q['correct_option']}.")
                            else:
                                st.error(f"❌ Incorrect. The correct answer is **{q['correct_option']}**.")
                            
                            # RECAP BOX
                            explanation = q.get('explanation', 'Review the notes for this topic.')
                            st.markdown(f"""
                            <div class="recap-box">
                                <b>💡 Quick Recap:</b> {explanation}
                            </div>
                            """, unsafe_allow_html=True)
                            st.write("---")
                            
                    st.metric("Final Score", f"{score}/{len(qs)}")
                    if st.button("Take Another Quiz"):
                        st.session_state.quiz_submitted = False
                        st.session_state.quiz_data = None
                        st.rerun()
        
        else: # THEORY MODE
            st.info("Study ideal answers for theory questions.")
            
            col_t1, col_t2, col_t3 = st.columns(3)
            with col_t1:
                t_type = st.selectbox("Type", ["Short Answer", "Long Answer", "Custom"])
            with col_t2:
                marks = 5
                if t_type == "Custom": marks = st.number_input("Marks Weightage", 1, 20, 5)
            with col_t3:
                num_q = st.number_input("Number of Questions", 1, 10, 3)
            
            if st.button("Generate Theory Questions"):
                with st.spinner("Thinking..."):
                    res = generate_theory_questions(data['raw_text'], t_type, marks, num_q, client)
                    st.markdown(res)

    with tab3:
        st.subheader("📈 Performance Analysis (Adaptive Learning)")
        
        weak_spots, strong_spots = get_topic_performance(data['name'])
        
        col_w, col_s = st.columns(2)
        
        with col_w:
            st.error("⚠️ Weak Areas (Focus Here)")
            if weak_spots:
                for t in weak_spots:
                    st.markdown(f"- **{t}** (Needs Review)")
            else:
                st.write("No persistent weak areas detected yet! Keep practicing.")
                
        with col_s:
            st.success("✅ Strong Areas (Review Less Often)")
            if strong_spots:
                for t in strong_spots:
                    st.markdown(f"- **{t}** (Mastered)")
            else:
                st.write("Take a quiz to identify your strengths.")
                
        st.caption("Note: Low accuracy and low confidence scores flag a topic as a Weak Area for the next quiz.")

    with tab4:
        st.subheader("🎯 Exam Hacker: Previous Year Questions (PYQ) Analysis")
        st.info("Upload 1-3 PDFs of past question papers to analyze exam trends and repeated topics.")
        
        pyq_upload = st.file_uploader("Upload PYQ PDF", type="pdf", key="pyq_upload")
        
        if pyq_upload:
            if st.button("📊 Analyze Exam Pattern"):
                with st.spinner("Scanning past papers for high-frequency topics..."):
                    pyq_output = analyze_pyq_pdf(pyq_upload, client)
                    st.session_state.pyq_analysis = pyq_output
        
        if st.session_state.pyq_analysis:
            st.success("Top Exam Topics Identified:")
            st.markdown(st.session_state.pyq_analysis)
            st.caption("Use these prioritized topics to guide your study focus.")
