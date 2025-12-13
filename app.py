import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64
import sqlite3
import json
import re
from datetime import date, timedelta # Keeping timedelta for basic logic/future use
import os # Keeping OS import for safety

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
    .question-box {
        background-color: #ffffff;
        padding: 15px;
        border-radius: 10px;
        border: 1px solid #e0e0e0;
        margin-bottom: 15px;
    }
</style>
""", unsafe_allow_html=True)

# --- DATABASE LAYER ---
def init_db():
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    
    # --- WIPE AND RECREATE SIMPLE TABLES (Guaranteed stability) ---
    c.execute("DROP TABLE IF EXISTS projects")
    c.execute("DROP TABLE IF EXISTS quiz_performance")
    
    # Simple Projects table (Only 5 original columns)
    c.execute('''CREATE TABLE projects (
        name TEXT PRIMARY KEY, level TEXT, notes TEXT, raw_text TEXT, progress INTEGER DEFAULT 0
    )''')
    # Simple Quiz table (No confidence, no SRS columns)
    c.execute('''CREATE TABLE quiz_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT, topic_tag TEXT, question_type TEXT, is_correct INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

def save_project_to_db(name, level, notes, raw_text):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    # Simple INSERT for the 5-column table
    c.execute('INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress) VALUES (?, ?, ?, ?, ?)', 
              (name, level, notes, raw_text, 0))
    conn.commit()
    conn.close()

def log_quiz_result(project_name, topic, q_type, correct):
    # Log without confidence column
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('INSERT INTO quiz_performance (project_name, topic_tag, question_type, is_correct) VALUES (?, ?, ?, ?)',
              (project_name, topic, q_type, 1 if correct else 0))
    conn.commit()
    conn.close()

def get_weak_areas(project_name):
    # Retrieve weak areas based only on accuracy
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''SELECT topic_tag, AVG(is_correct) as accuracy FROM quiz_performance WHERE project_name = ? GROUP BY topic_tag ORDER BY accuracy ASC''', (project_name,))
    data = c.fetchall()
    conn.close()
    return [row[0] for row in data if row[1] < 0.6]

def get_project_details(name):
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    # Fetch from the simple 5-column table
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
    # Reset file pointer for stability on re-run
    uploaded_file.seek(0)
    
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

# --- BATCH NOTES GENERATION (Stabilized) ---
def generate_study_notes(raw_text, level, client):
    if not raw_text: return "Error generating notes."

    pages = raw_text.split("--- PAGE_BREAK ---")
    batch_size = 5 
    batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
    
    final_notes = f"# 📘 {level} Study Guide\n\n"
    
    status_text = st.empty()
    bar = st.progress(0)
    
    system_instructions = f"""Act as a Professor. Create a comprehensive {level} study guide in Markdown. Use descriptive headers."""

    for i, batch in enumerate(batches):
        bar.progress((i + 1) / len(batches))
        status_text.caption(f"🧠 Synthesizing Batch {i+1}/{len(batches)}...")
        
        batch_content = "\n".join(batch)
        limited_batch_content = batch_content[:10000]

        prompt = f"""
        {system_instructions}
        
        CONTENT TO PROCESS (Batch {i+1}):
        {limited_batch_content}
        
        Output strictly Markdown.
        """
        
        try:
            completion = client.chat.completions.create(
                model="llama-3.1-405b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            final_notes += completion.choices[0].message.content + "\n\n---\n\n"
        except Exception as e:
            final_notes += f"\n\n(Error processing batch {i+1}: {e})\n\n"
            
    status_text.empty()
    bar.empty()
    if "(Error processing batch" in final_notes:
        return "Error generating notes."
    return final_notes
# --- END BATCH NOTES GENERATION ---

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
        return {"error": "Text too short or empty. Please re-upload PDF."}

    context_text = raw_text[:6000] 
    focus_prompt = f"Focus specifically on these weak topics: {', '.join(weak_topics)}" if weak_topics else "Cover all topics evenly."
    
    prompt = f"""
    Create a JSON object with 5 practice questions (MCQ or True/False) based on the text.
    {focus_prompt}
    
    IMPORTANT: For each question, provide an 'explanation' field (2-sentence quick recap of the concept).
    
    Format:
    {{
        "questions": [
            {{
                "type": "MCQ",
                "question": "Question text...",
                "options": ["A", "B", "C", "D"],
                "correct_option": "A",
                "topic": "Topic Name",
                "explanation": "Concept recap here..."
            }}
        ]
    }}
    
    CONTENT: {context_text}
    Output ONLY valid JSON. Do not add markdown or intro text.
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
    if q_type == "Custom": length_instruction = f"These are {marks}-mark questions. The answer should be detailed enough for a {marks}-mark exam question."
    
    prompt = f"""
    Create {num_q} {q_type} Answer Theory Questions based on the text.
    Provide the Ideal Answer for each.
    {length_instruction}
    
    Format:
    ### Q1: [Question]
    **Answer:** [Ideal Answer]
    
    CONTENT: {context_text}
    """
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}], temperature=0.4
        )
        return completion.choices[0].message.content
    except Exception as e: return f"Error generating theory: {str(e)}"

# --- MAIN UI ---

if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'api_key' not in st.session_state: st.session_state.api_key = None
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state: st.session_state.user_answers = {}

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
            st.session_state.user_answers = {}
            st.rerun()
    if st.button("+ New Project"): 
        st.session_state.current_project = None
        st.session_state.quiz_data = None
        st.rerun()
    
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
            with st.spinner("Step 1/2: Extracting content with Vision Mode..."):
                text = extract_content_with_vision(up, client)
            with st.spinner("Step 2/2: Generating study notes (High Quality Model)..."):
                notes = generate_study_notes(text, level, client)
            
            save_project_to_db(name, level, notes, text)
            st.session_state.current_project = name
            st.rerun()

# PAGE 2: DASHBOARD
else:
    data = get_project_details(st.session_state.current_project)
    
    if not data or not data['raw_text'] or len(data['raw_text']) < 50:
        st.error("⚠️ Error: Could not load project data or no readable text was found. Please click '+ New Project' and upload a new document.")
        st.session_state.current_project = None
        st.stop()

    st.header(f"📘 {data['name']}")
    
    tab1, tab2, tab3 = st.tabs(["📖 Study Notes", "📝 Practice", "📊 Analytics"])
    
    with tab1:
        st.subheader("📝 Study Guide")
        st.markdown(data['notes'])
        
    with tab2:
        st.subheader("🎯 Active Practice")
        mode = st.radio("Select Mode:", ["Objective (Interactive)", "Theory (Study Mode)"], horizontal=True)
        
        weak_spots = get_weak_areas(data['name'])

        if mode == "Objective (Interactive)":
            col1, col2 = st.columns([3, 1])
            with col1:
                if weak_spots: st.warning(f"Adaptively focusing on {len(weak_spots)} weak areas.")
                else: st.info("Test your knowledge. Results are tracked to identify weak areas.")

            with col2:
                if st.button("🔄 Generate New Quiz"):
                    with st.spinner("Generating adaptive questions..."):
                        q_data = generate_objective_quiz(data['raw_text'], weak_spots, client)
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
                            # Removed Confidence Slider/Selector
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
                        correct = (user_ans == q['correct_option'])
                        if correct: score += 1
                        
                        # Log without confidence
                        log_quiz_result(data['name'], q.get('topic', 'General'), q['type'], correct)
                        
                        # RENDER RESULT CARD
                        with st.container():
                            st.markdown(f"""<div class="question-box">
                            <b>Q{i+1}: {q['question']}</b><br>
                            Your Answer: {user_ans}
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
        st.subheader("📈 Performance Analysis")
        
        # Displaying simple weak/strong areas based on simple log
        weak_spots_list = get_weak_areas(data['name'])
        
        col_w, col_s = st.columns(2)
        
        with col_w:
            st.error("⚠️ Weak Areas (Focus Here)")
            if weak_spots_list:
                for t in weak_spots_list:
                    st.markdown(f"- **{t}** (Needs Review)")
            else:
                st.write("No weak areas detected yet! Keep practicing.")
                
        with col_s:
            st.success("✅ Strong Areas")
            if weak_spots_list:
                # Simple logic to show what is NOT weak as strong
                st.write("Take more quizzes to fully define strong areas.")
            else:
                st.write("All topics appear strong so far!")
                
        st.caption("Note: The 'Generate New Quiz' button in the Practice tab will automatically prioritize your Weak Areas.")
