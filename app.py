import streamlit as st
import fitz # PyMuPDF
from groq import Groq
import sqlite3
import json

# --- CONFIGURATION ---
GROQ_MODEL = "llama-3.1-8b-instant"
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80

# --- DATABASE LAYER ---
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
                practice_data TEXT,
                analogy_data TEXT,
                theory_data TEXT
            )
        ''')
        # Check for theory_data column
        try:
            c.execute("SELECT theory_data FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN theory_data TEXT DEFAULT '{}'")
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text):
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, practice_data, analogy_data, theory_data)
            VALUES (?, ?, ?, ?, '{}', '{}', '{}')
        ''', (name, level, notes, raw_text))
        conn.commit()
        conn.close()

    def get_project(self, name):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT * FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3], 
                    "practice_data": row[4], "analogy_data": row[5], "theory_data": row[6]}
        return None

    def update_field(self, name, field, key, content):
        data = self.get_project(name)
        field_dict = json.loads(data.get(field) or "{}")
        field_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f"UPDATE projects SET {field} = ? WHERE name = ?", (json.dumps(field_dict), name))
        conn.commit()
        conn.close()

db = StudyDB()

# --- LLM CORE ---
def generate_content(prompt, client, is_json=False):
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": "You are an expert academic tutor. Follow instructions strictly."},
                      {"role": "user", "content": prompt}],
            response_format={"type": "json_object"} if is_json else None,
            temperature=0.3
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"API Error: {e}")
        return None

# --- UI LOGIC ---
st.set_page_config(page_title="Mastery Study Companion", layout="wide")

with st.sidebar:
    st.title("🎓 Study Partner")
    api_key = st.text_input("Groq API Key", type="password")
    client = Groq(api_key=api_key) if api_key else None
    
    st.divider()
    projects = db.connect().execute("SELECT name FROM projects").fetchall()
    names = [p[0] for p in projects]
    selected = st.selectbox("Your Units", ["New Unit"] + names)
    
    if selected != "New Unit":
        st.session_state.current_project = selected
        mode = st.radio("Navigation", ["Dashboard", "Notes", "Analogies", "Theory Hub", "Practice"])
    else:
        st.session_state.current_project = None
        mode = "Create"

if mode == "Create":
    st.header("🚀 Initialize New Unit")
    file = st.file_uploader("Upload PDF", type="pdf")
    if file and client:
        name = st.text_input("Unit Name")
        if st.button("Generate Study Plan"):
            with st.spinner("Extracting & Synthesizing..."):
                doc = fitz.open(stream=file.read(), filetype="pdf")
                raw = "\n".join([page.get_text() for page in doc])
                notes = generate_content(f"Summarize this text into structured study notes: {raw[:12000]}", client)
                db.save_project(name, "Intermediate", notes, raw)
                st.rerun()

elif st.session_state.current_project:
    data = db.get_project(st.session_state.current_project)
    
    if mode == "Notes":
        st.header("📖 Study Notes")
        st.markdown(data['notes'])

    elif mode == "Analogies":
        st.header("💡 Analogy Engine")
        col1, col2 = st.columns([2, 1])
        
        with col2:
            st.subheader("Missing a topic?")
            custom_q = st.text_input("Type topic for analogy...")
            if st.button("Explain Custom Topic"):
                ans = generate_content(f"Explain '{custom_q}' using a relatable real-world analogy based on this context: {data['notes'][:2000]}", client)
                st.info(ans)

        with col1:
            if st.button("🔍 Generate All Major Analogies"):
                ana = generate_content(f"Identify all major concepts in these notes and provide a real-world analogy for each. Do not skip main topics. Context: {data['notes'][:6000]}", client)
                db.update_field(data['name'], 'analogy_data', 'all', ana)
                st.rerun()
            st.markdown(json.loads(data['analogy_data']).get('all', "Click above to generate analogies for all main topics."))

    elif mode == "Theory Hub":
        st.header("📝 Theory Q&A Hub")
        c1, c2, c3 = st.columns(3)
        
        if c1.button("Generate Short Q&A (2-3 Marks)"):
            qna = generate_content(f"Generate important short-answer questions (2-3 marks each) and their answers based on: {data['notes'][:6000]}", client)
            db.update_field(data['name'], 'theory_data', 'short', qna)
            st.rerun()
            
        if c2.button("Generate Long Q&A (5-10 Marks)"):
            qna = generate_content(f"Generate important long-answer questions (depth-focused, 5-10 marks each) and their answers based on: {data['notes'][:6000]}", client)
            db.update_field(data['name'], 'theory_data', 'long', qna)
            st.rerun()
            
        with c3:
            marks = st.number_input("Enter Marks for Custom Answer", min_value=1, max_value=20, value=5)
            topic = st.text_input("Topic for Custom Q&A")
            if st.button(f"Generate {marks}-Mark Answer"):
                qna = generate_content(f"Write a comprehensive exam-style answer worth exactly {marks} marks for the topic '{topic}' using these notes: {data['notes'][:4000]}", client)
                st.write(qna)

        t_data = json.loads(data['theory_data'])
        tab1, tab2 = st.tabs(["Short Answers", "Long Answers"])
        tab1.markdown(t_data.get('short', "No short questions generated."))
        tab2.markdown(t_data.get('long', "No long questions generated."))

    elif mode == "Practice":
        st.header("🎯 MCQ Drills")
        # (Existing Quiz Logic remains here, ensuring questions are derived strictly from notes)
