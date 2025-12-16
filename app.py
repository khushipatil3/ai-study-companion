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
        # Ensure base table exists
        c.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                level TEXT,
                notes TEXT,
                raw_text TEXT,
                practice_data TEXT DEFAULT '{}',
                analogy_data TEXT DEFAULT '{}',
                theory_data TEXT DEFAULT '{}'
            )
        ''')
        
        # Schema Migration: Add missing columns if they don't exist
        for col in [('practice_data', '{}'), ('analogy_data', '{}'), ('theory_data', '{}')]:
            try:
                c.execute(f"SELECT {col[0]} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col[0]} TEXT DEFAULT '{col[1]}'")
        
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
        c.execute("SELECT name, level, notes, raw_text, practice_data, analogy_data, theory_data FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3], 
                    "practice_data": row[4], "analogy_data": row[5], "theory_data": row[6]}
        return None

    def update_field(self, name, field, key, content):
        data = self.get_project(name)
        if not data: return
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
    if not client: return None
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert academic tutor. You synthesize high-quality study materials strictly from provided text. Ensure formatting is clear Markdown."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"} if is_json else None,
            temperature=0.4 # Balanced for creativity (analogies) and factuality (notes)
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"API Error: {e}")
        return None

# --- UI LOGIC ---
st.set_page_config(page_title="AI Study Companion", layout="wide", page_icon="🎓")

with st.sidebar:
    st.title("🎓 Study Partner")
    api_key = st.text_input("Groq API Key", type="password")
    client = Groq(api_key=api_key) if api_key else None
    
    st.divider()
    projects = db.connect().execute("SELECT name FROM projects").fetchall()
    names = [p[0] for p in projects]
    selected = st.selectbox("Your Units", ["+ New Unit"] + names)
    
    if selected != "+ New Unit":
        st.session_state.current_project = selected
        mode = st.radio("Navigation", ["📚 Notes", "💡 Analogies", "📝 Theory Hub", "🎯 Practice"])
    else:
        st.session_state.current_project = None
        mode = "Create"

# --- WORKFLOWS ---

if mode == "Create":
    st.header("🚀 Initialize New Unit")
    file = st.file_uploader("Upload PDF", type="pdf")
    if file and client:
        name = st.text_input("Unit Name")
        if st.button("Generate Study Plan") and name:
            with st.spinner("Analyzing PDF and generating notes..."):
                doc = fitz.open(stream=file.read(), filetype="pdf")
                raw = "\n".join([page.get_text() for page in doc])
                prompt = f"Create a comprehensive, structured study guide from these notes. Use clear headings, bullet points, and highlight key terms. NOTES: {raw[:15000]}"
                notes = generate_content(prompt, client)
                if notes:
                    db.save_project(name, "Intermediate", notes, raw)
                    st.session_state.current_project = name
                    st.rerun()

elif st.session_state.current_project:
    data = db.get_project(st.session_state.current_project)
    
    if mode == "📚 Notes":
        st.header(f"📖 Notes: {data['name']}")
        st.markdown(data['notes'])

    elif mode == "💡 Analogies":
        st.header("💡 Analogy Engine")
        col1, col2 = st.columns([2, 1])
        
        with col2:
            st.subheader("🎯 Custom Request")
            custom_topic = st.text_input("Missed a topic? Type it here:")
            if st.button("Explain Topic") and custom_topic:
                ans = generate_content(f"Explain the concept of '{custom_topic}' using a creative and accurate real-world analogy based on these notes: {data['notes'][:2000]}", client)
                st.info(ans)

        with col1:
            if st.button("🔍 Generate Analogies for All Main Concepts"):
                prompt = f"Identify all the primary major concepts in the following notes. For each major concept, provide a clear, relatable real-world analogy. Format as: **Concept Name** followed by 'Analogy: ...'. NOTES: {data['notes'][:7000]}"
                ana = generate_content(prompt, client)
                db.update_field(data['name'], 'analogy_data', 'all', ana)
                st.rerun()
            st.markdown(json.loads(data['analogy_data']).get('all', "No analogies generated yet. Click above to start."))

    elif mode == "📝 Theory Hub":
        st.header("📝 Theory Q&A Hub")
        c1, c2, c3 = st.columns(3)
        
        if c1.button("Generate Short Q&A (2-3 Marks)"):
            qna = generate_content(f"Generate 10 important short-answer questions (2-3 marks weightage) and their model answers based on: {data['notes'][:8000]}", client)
            db.update_field(data['name'], 'theory_data', 'short', qna)
            st.rerun()
            
        if c2.button("Generate Long Q&A (5-10 Marks)"):
            qna = generate_content(f"Generate 5 high-priority long-answer/essay-style questions (5-10 marks weightage) and their detailed model answers based on: {data['notes'][:8000]}", client)
            db.update_field(data['name'], 'theory_data', 'long', qna)
            st.rerun()
            
        with c3:
            custom_marks = st.number_input("Answer Weightage (Marks)", 1, 20, 5)
            custom_topic = st.text_input("Topic for custom answer:")
            if st.button(f"Generate {custom_marks}-Mark Answer") and custom_topic:
                qna = generate_content(f"Write a model exam answer for '{custom_topic}' worth exactly {custom_marks} marks. Adjust the length and depth of the explanation to suit the mark value. Context: {data['notes'][:4000]}", client)
                st.write(qna)

        t_data = json.loads(data['theory_data'])
        tab1, tab2 = st.tabs(["Short Answers (2-3M)", "Long Answers (5-10M)"])
        tab1.markdown(t_data.get('short', "Nothing generated yet."))
        tab2.markdown(t_data.get('long', "Nothing generated yet."))

    elif mode == "🎯 Practice":
        st.header("🎯 Interactive MCQs")
        # Ensure your existing Practice logic is here to update the Mastery Tracker
