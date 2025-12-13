import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64
import sqlite3
import json
import re

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
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT, topic_tag TEXT, question_type TEXT, is_correct INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def save_project_to_db(name, level, notes, raw_text):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress) VALUES (?, ?, ?, ?, ?)', 
              (name, level, notes, raw_text, 0))
    conn.commit()
    conn.close()

def log_quiz_result(project_name, topic, q_type, correct):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('INSERT INTO quiz_performance (project_name, topic_tag, question_type, is_correct) VALUES (?, ?, ?, ?)',
              (project_name, topic, q_type, 1 if correct else 0))
    conn.commit()
    conn.close()

def get_weak_areas(project_name):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''SELECT topic_tag, AVG(is_correct) as accuracy FROM quiz_performance WHERE project_name = ? GROUP BY topic_tag ORDER BY accuracy ASC''', (project_name,))
    data = c.fetchall()
    conn.close()
    return [row[0] for row in data if row[1] < 0.6]

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

# --- HELPER FUNCTIONS ---
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
    # This still uses the High-Quality 70B Model for Notes
    prompt = f"Create a comprehensive {level} study guide in Markdown based on this content: {raw_text[:25000]}"
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}], temperature=0.3
        )
        return completion.choices[0].message.content
    except: return "Error generating notes."

# --- QUIZ & THEORY GENERATORS (OPTIMIZED) ---

def clean_json_string(json_str):
    """Removes markdown code blocks if the AI adds them."""
    json_str = json_str.strip()
    if json_str.startswith("```json"):
        json_str = json_str[7:]
    if json_str.endswith("```"):
        json_str = json_str[:-3]
    return json_str.strip()

def generate_objective_quiz(raw_text, weak_topics, client):
    if not raw_text or len(raw_text) < 100:
        return {"error": "Text too short. Please re-upload PDF."}

    # OPTIMIZATION: Use smaller context & smaller model
    context_text = raw_text[:5000]
    focus_prompt = f"Focus specifically on these weak topics: {', '.join(weak_topics)}" if weak_topics else "Cover all topics evenly."
    
    prompt = f"""
    Create a JSON object with 5 practice questions based on the text.
    {focus_prompt}
    
    Format:
    {{
        "questions": [
            {{
                "type": "MCQ",
                "question": "Question text...",
                "options": ["A", "B", "C", "D"],
                "correct_option": "A",
                "topic": "Topic Name"
            }},
            {{
                "type": "TrueFalse",
                "question": "Statement...",
                "correct_option": "True",
                "topic": "Topic Name"
            }}
        ]
    }}
    
    CONTENT: {context_text}
    Output ONLY valid JSON.
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant", # ✅ CHEAPER MODEL
            messages=[{"role": "user", "content": prompt}], temperature=0.5, response_format={"type": "json_object"}
        )
        content = completion.choices[0].message.content
        cleaned_content = clean_json_string(content)
        return json.loads(cleaned_content)
    except Exception as e:
        return {"error": f"Quiz Generation Failed: {str(e)}"}

def generate_theory_questions(raw_text, q_type, marks, client):
    if not raw_text: return "Error: No text available."

    # OPTIMIZATION: Use smaller context & smaller model
    context_text = raw_text[:5000]

    length_instruction = "Answer in 2-3 sentences." if q_type == "Short" else "Answer in 2 paragraphs."
    if q_type == "Custom": length_instruction = f"These are {marks}-mark questions. Detail matches marks."
    
    prompt = f"""
    Create 3 {q_type} Answer Theory Questions based on the text.
    Provide the Ideal Answer for each.
    {length_instruction}
    
    Format:
    ### Q1: [Question]
    **Answer:** [Ideal Answer]
    
    CONTENT: {context_text}
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant", # ✅ CHEAPER MODEL
            messages=[{"role": "user", "content": prompt}], temperature=0.4
        )
        return completion.choices[0].message.content
    except Exception as e: return f"Error generating theory: {str(e)}"

# --- MAIN UI ---

if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'api_key' not in st.session_state: st.session_state.api_key = None

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
            st.rerun()
    if st.button("+ New Project"): st.session_state.current_project = None; st.rerun()
    
    # BACKUP BUTTON
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
    
    tab1, tab2, tab3 = st.tabs(["📖 Study Notes", "📝 Practice", "📊 Analytics"])
    
    with tab1:
        st.markdown(data['notes'])
        
    with tab2:
        st.subheader("🎯 Active Practice")
        mode = st.radio("Select Mode:", ["Objective (Interactive)", "Theory (Study Mode)"], horizontal=True)
        
        if mode == "Objective (Interactive)":
            col1, col2 = st.columns([3, 1])
            with col1:
                st.info("Test your knowledge. Results are tracked to identify weak areas.")
            with col2:
                if st.button("🔄 Generate New Quiz"):
                    weak_spots = get_weak_areas(data['name'])
                    with st.spinner("Generating adaptive questions..."):
                        q_data = generate_objective_quiz(data['raw_text'], weak_spots, client)
                        
                        if "error" in q_data:
                            st.error(q_data["error"])
                        else:
                            st.session_state.quiz_data = q_data
            
            if st.session_state.get('quiz_data') and "questions" in st.session_state.quiz_data:
                qs = st.session_state.quiz_data['questions']
                if not qs:
                    st.warning("AI generated 0 questions. Try again.")
                else:
                    with st.form("quiz_form"):
                        for i, q in enumerate(qs):
                            st.markdown(f"**Q{i+1}: {q['question']}**")
                            if q['type'] == 'MCQ':
                                st.radio("Choose:", q['options'], key=f"q{i}", index=None)
                            else:
                                st.radio("Choose:", ["True", "False"], key=f"q{i}", index=None)
                            st.divider()
                        
                        if st.form_submit_button("Submit Answers"):
                            score = 0
                            for i, q in enumerate(qs):
                                user_ans = st.session_state.get(f"q{i}")
                                correct = (user_ans == q['correct_option'])
                                if correct: score += 1
                                log_quiz_result(data['name'], q.get('topic', 'General'), q['type'], correct)
                                
                                if correct:
                                    st.success(f"Q{i+1}: Correct! ✅")
                                else:
                                    st.error(f"Q{i+1}: Incorrect ❌. Answer: {q['correct_option']}")
                            st.metric("Your Score", f"{score}/{len(qs)}")
        
        else: # THEORY MODE
            st.info("Study ideal answers for theory questions.")
            col_t1, col_t2 = st.columns(2)
            with col_t1:
                t_type = st.selectbox("Type", ["Short Answer", "Long Answer", "Custom"])
            with col_t2:
                marks = 5
                if t_type == "Custom": marks = st.number_input("Marks Weightage", 1, 20, 5)
            
            if st.button("Generate Theory Questions"):
                with st.spinner("Thinking..."):
                    res = generate_theory_questions(data['raw_text'], t_type, marks, client)
                    st.markdown(res)

    with tab3:
        st.subheader("📈 Performance Tracker")
        conn = sqlite3.connect('study_db.sqlite')
        try:
            res = conn.execute("SELECT AVG(is_correct), COUNT(*) FROM quiz_performance WHERE project_name=?", (data['name'],)).fetchone()
            acc = round((res[0] or 0) * 100, 1)
            total = res[1]
            col_a, col_b = st.columns(2)
            col_a.metric("Overall Accuracy", f"{acc}%")
            col_b.metric("Questions Attempted", total)
            
            topics = conn.execute("SELECT topic_tag, AVG(is_correct) FROM quiz_performance WHERE project_name=? GROUP BY topic_tag", (data['name'],)).fetchall()
            if topics:
                t_names = [t[0] for t in topics]
                t_scores = [t[1] * 100 for t in topics]
                st.bar_chart({t: s for t, s in zip(t_names, t_scores)})
            else:
                st.caption("No quiz data yet. Take a quiz to see analytics!")
        except: st.error("No data.")
        conn.close()
