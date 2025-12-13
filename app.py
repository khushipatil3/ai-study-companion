import streamlit as st
import fitz # PyMuPDF
from groq import Groq # FIX: Corrected import from GroQ to Groq
import sqlite3
import json
import base64 

# --- MODEL CONSTANT ---
# Current stable Groq model for fast, high-quality responses.
GROQ_MODEL = "llama-3.1-8b-instant" 

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .reportview-container { margin-top: -2em; }
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
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
        Creates the database table if it doesn't exist.
        Includes SCHEMA MIGRATION logic to add the 'practice_data' column if it's missing.
        """
        conn = self.connect()
        c = conn.cursor()
        
        # 1. CREATE TABLE IF NOT EXISTS - Use the latest schema definition
        c.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                level TEXT,
                notes TEXT,
                raw_text TEXT,
                progress INTEGER DEFAULT 0,
                practice_data TEXT 
            )
        ''')

        # 2. SCHEMA MIGRATION: Check and add the 'practice_data' column if it's missing
        try:
            # Try to select the new column. If the column is missing, this will raise an OperationalError.
            c.execute("SELECT practice_data FROM projects LIMIT 1")
        except sqlite3.OperationalError as e:
            # Check the error message to confirm it's a "no such column" error
            if "no such column" in str(e):
                st.warning("Performing database migration: Adding 'practice_data' column.")
                c.execute("ALTER TABLE projects ADD COLUMN practice_data TEXT DEFAULT '{}'")
            else:
                # Re-raise other unexpected OperationalErrors
                raise e 
        
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text, practice_data="{}"):
        """Saves a new project or updates an existing one."""
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data))
        conn.commit()
        conn.close()

    def update_practice_data(self, name, key, content):
        """Updates a specific part of the practice_data JSON field."""
        project_data = self.get_project_details(name)
        if not project_data or 'practice_data' not in project_data:
            return

        # Safely load existing practice data, defaulting to an empty dict
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict[key] = content
        
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            UPDATE projects SET practice_data = ? WHERE name = ?
        ''', (json.dumps(practice_dict), name))
        conn.commit()
        conn.close()

    def load_all_projects(self):
        """Fetches a list of all project names."""
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name FROM projects")
        projects = [row[0] for row in c.fetchall()]
        conn.close()
        return projects

    def get_project_details(self, name):
        """Fetches the full details of a specific project."""
        conn = self.connect()
        c = conn.cursor()
        # Ensure we select all columns, including the new one
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row[0],
                "level": row[1],
                "notes": row[2],
                "raw_text": row[3],
                "progress": row[4],
                "practice_data": row[5] 
            }
        return None

db = StudyDB() # Initialize DB

# --- SESSION STATE ---
if 'current_project' not in st.session_state:
    st.session_state.current_project = None
if 'theory_marks' not in st.session_state:
    st.session_state.theory_marks = 5
if 'groq_api_key' not in st.session_state: 
    st.session_state.groq_api_key = None 

# --- HELPER FUNCTIONS ---

def extract_content_text_only(uploaded_file):
    """
    Extracts text from a PDF file using PyMuPDF (text-only extraction for Groq's LLMs).
    """
    uploaded_file.seek(0)
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    progress_container = st.empty()
    bar = st.progress(0)
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        bar.progress((i + 1) / total_pages)
        progress_container.caption(f"📄 Extracting Text from Page {i+1} of {total_pages}...")
        try:
            text = page.get_text("text") 
            full_content += f"\n--- PAGE_BREAK ---\n{text}\n"
        except Exception as e:
            full_content += f"\n--- PAGE_BREAK ---\n(Error extracting text on page {i+1}: {e})\n"

    progress_container.empty()
    bar.empty()
    return full_content

def get_system_prompt(level):
    if level == "Basic":
        return """Act as a Tutor. GOAL: Pass the exam. Focus on definitions, brevity, and outlines. Output strictly Markdown. If you see text describing a diagram, use an 

[Image of X]
 tag where X is a detailed description of the diagram."""
    elif level == "Intermediate":
        return """Act as a Professor. GOAL: Solid understanding. Use detailed definitions, process steps, and exam tips. Output strictly Markdown. Insert 

[Image of X]
 tags frequently where X is a detailed description of a relevant diagram or concept."""
    else: # Advanced
        return """Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Output strictly Markdown. Insert  tags for every concept that would be better understood with a visual aid, using a detailed description for X."""

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
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            final_notes += completion.choices[0].message.content + "\n\n---\n\n"
        except Exception as e:
            final_notes += f"(Error during generation: {e})\n\n---\n\n"
            
    status_text.empty()
    bar.empty()
    return final_notes

# --- NEW GENERATION FUNCTIONS ---

def generate_qna(notes, q_type, marks, client):
    """Generates Theory Q&A based on the notes."""
    q_type_text = ""
    if q_type == "short":
        q_type_text = "5 questions requiring concise, short-answer responses (approx. 50-75 words each). Format each as Q: followed by A:."
    elif q_type == "long":
        q_type_text = "3 questions requiring detailed, long-answer responses (approx. 150-250 words each). Format each as Q: followed by A:."
    elif q_type == "custom":
        q_type_text = f"5 questions suitable for an exam where each question is worth approximately {marks} marks. The length and detail should match typical answers for that mark value. Format each as Q: followed by A:."
        
    system_prompt = f"You are a study guide generator. Your task is to analyze the provided study notes and generate {q_type_text} The output must be pure markdown."
    
    # Truncate notes for API call limit 
    notes_truncated = notes[:15000]
    
    try:
        with st.spinner(f"Generating {q_type} Q&A from notes..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate Q&A based on the following notes: {notes_truncated}"}
                ],
                temperature=0.5
            )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating Q&A: {e}"

def generate_practice_drills(notes, client):
    """Generates mixed practice drills (MCQ, Fill-in-the-Blanks, True/False)."""
    
    system_prompt = "You are a quiz master. Generate the following content based on the study notes: 5 Multiple Choice Questions (MCQs) with 4 options and the correct answer clearly marked. 5 Fill-in-the-Blank questions. 5 True or False questions. Clearly label each section (MCQS, FILL IN THE BLANKS, TRUE/FALSE). Provide all answers in a separate 'ANSWERS' section at the end. Output must be pure markdown."
    
    notes_truncated = notes[:15000]

    try:
        with st.spinner("Generating mixed practice drills..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate practice drills based on the following notes: {notes_truncated}"}
                ],
                temperature=0.7
            )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating practice drills: {e}"

# --- SIDEBAR (NAVIGATION) ---
with st.sidebar:
    st.title("🗂️ My Library")
    
    # --- API Key Handling ---
    final_api_key = None
    
    # 1. Check Streamlit Secrets (for deployed app)
    if "GROQ_API_KEY" in st.secrets:
        final_api_key = st.secrets["GROQ_API_KEY"]
        st.success("API Key Loaded from Secrets.")
    
    # 2. Check Session State (for user input)
    if not final_api_key and st.session_state.groq_api_key:
        final_api_key = st.session_state.groq_api_key
        st.success("API Key is configured in this session.")
        
    # 3. Handle User Input
    with st.expander("⚙️ Settings: Groq API Key", expanded=not bool(final_api_key)):
        # If a key exists (from secrets or session), pre-fill the input
        key_display_value = final_api_key if final_api_key else ""
        
        api_key_input = st.text_input(
            "Groq API Key (Set in Secrets for deployment)", 
            type="password", 
            value=key_display_value
        )
        
        # Logic to update session state only if input changes
        if api_key_input and api_key_input != st.session_state.groq_api_key:
            st.session_state.groq_api_key = api_key_input
            st.rerun() 
        # If input is empty but session state has a key (user deleted it), clear session state
        elif not api_key_input and st.session_state.groq_api_key:
            st.session_state.groq_api_key = None
            st.rerun()

    # Set the final flag
    api_key_configured = bool(final_api_key)

    st.divider()
    
    # LOAD PROJECTS FROM DATABASE
    saved_projects = db.load_all_projects()
    
    if saved_projects:
        st.write("### Saved Projects")
        for project_name in saved_projects:
            if st.button(f"📄 {project_name}", use_container_width=True, key=f"btn_{project_name}"):
                st.session_state.current_project = project_name
                st.rerun()
                
    if st.button("+ New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN APP LOGIC ---

if not api_key_configured:
    st.warning("Please configure your Groq API Key in the sidebar or via Streamlit Secrets to start.")
    st.stop()
    
try:
    # Initialize Groq client using the determined key
    client = Groq(api_key=final_api_key)
except Exception as e:
    st.error(f"Error initializing Groq client. Please check the key. Details: {e}")
    st.stop()


# VIEW 1: CREATE NEW PROJECT
if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    st.markdown("### Upload a document to add it to your library.")
    
    uploaded_file = st.file_uploader("Upload PDF", type="pdf")
    
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2:
            level = st.select_slider("Detail Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
            
        if st.button("✨ Create & Generate"):
            
            # 1. Text Extraction
            with st.spinner("Step 1: Extracting text from PDF..."):
                raw_text = extract_content_text_only(uploaded_file)
            
            # 2. Generate Notes
            if len(raw_text) > 50:
                with st.spinner("Step 2: Synthesizing notes with Groq LLM..."):
                    notes = generate_study_notes(raw_text, level, client)
                
                # 3. SAVE TO DATABASE
                db.save_project(project_name, level, notes, raw_text)
                
                st.session_state.current_project = project_name
                st.success("Project created and notes generated!")
                st.rerun()
            else:
                st.error("Could not read sufficient text from document.")


# VIEW 2: PROJECT DASHBOARD (UPDATED WORKSPACE)
else:
    # Fetch data from DB
    project_data = db.get_project_details(st.session_state.current_project)
    
    if project_data:
        # Load practice data from JSON
        practice_data = json.loads(project_data.get('practice_data') or "{}")

        # Header
        col_header, col_btn = st.columns([3, 1])
        with col_header:
            st.title(f"📘 {project_data['name']}")
            st.caption(f"Level: {project_data['level']} | Status: ✅ Saved in Database")
        with col_btn:
            st.download_button("💾 Export Notes", project_data['notes'], file_name=f"{project_data['name']}.md")

        # Tabs for Tools
        tab1, tab2, tab3 = st.tabs(["📖 Study Notes", "🧠 Practices", "📊 Progress Tracker"])
        
        with tab1:
            st.markdown(project_data['notes'])
            
        with tab2:
            # New sub-tabs for Theory and Practice
            sub_tab1, sub_tab2 = st.tabs(["📝 Theory Q&A", "🎯 Practice Drills"])
            
            with sub_tab1: # THEORY Q&A
                st.subheader("Generate Question & Answers")
                
                col_short, col_long, col_custom = st.columns(3)

                # Initialize display keys if they don't exist
                if 'qna_display_key' not in st.session_state:
                    st.session_state.qna_display_key = None
                if 'qna_content' not in st.session_state:
                    st.session_state.qna_content = None

                # --- SHORT ANSWER ---
                with col_short:
                    if st.button("Generate Short Answer (5 Qs)", key="btn_short"):
                        qna_content = generate_qna(project_data['notes'], "short", 0, client)
                        db.update_practice_data(project_data['name'], "short_qna", qna_content)
                        st.session_state.qna_display_key = "short_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()
                
                # --- LONG ANSWER ---
                with col_long:
                    if st.button("Generate Long Answer (3 Qs)", key="btn_long"):
                        qna_content = generate_qna(project_data['notes'], "long", 0, client)
                        db.update_practice_data(project_data['name'], "long_qna", qna_content)
                        st.session_state.qna_display_key = "long_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()

                # --- CUSTOM ANSWER ---
                with col_custom:
                    st.session_state.theory_marks = st.number_input("Custom Mark Value", min_value=1, max_value=25, value=st.session_state.theory_marks, key="mark_input")
                    custom_key = f"custom_qna_{st.session_state.theory_marks}"
                    if st.button(f"Generate Custom ({st.session_state.theory_marks} Marks)", key="btn_custom"):
                        qna_content = generate_qna(project_data['notes'], "custom", st.session_state.theory_marks, client)
                        db.update_practice_data(project_data['name'], custom_key, qna_content)
                        st.session_state.qna_display_key = custom_key
                        st.session_state.qna_content = qna_content
                        st.rerun()

                st.divider()

                # Display Logic
                display_content = ""
                display_key = st.session_state.get('qna_display_key')

                if display_key and st.session_state.qna_content:
                    # 1. Prioritize content generated in the current session
                    display_content = st.session_state.qna_content
                elif display_key in practice_data:
                    # 2. Fallback to content saved in the database based on the last selected key
                    display_content = practice_data[display_key]
                else:
                    # 3. Fallback to showing any saved content
                    display_content = practice_data.get("long_qna") or practice_data.get("short_qna")

                if display_content:
                    st.markdown(display_content)
                else:
                    st.info("Select a generation type above to create your Theory Q&A!")


            with sub_tab2: # PRACTICE DRILLS (MCQ, Fill-in, T/F)
                st.subheader("Mixed Practice Drills")
                
                if st.button("Generate Mixed Drills (MCQ, Fill, T/F)", type="primary", key="btn_drills"):
                    drills_content = generate_practice_drills(project_data['notes'], client)
                    db.update_practice_data(project_data['name'], "practice_drills", drills_content)
                    st.session_state.practice_drills = drills_content
                    st.rerun()

                st.divider()

                if 'practice_drills' in st.session_state:
                    st.markdown(st.session_state.practice_drills)
                elif practice_data.get('practice_drills'):
                    st.markdown(practice_data.get('practice_drills'))
                else:
                    st.info("Click the button above to generate a full set of practice drills.")

        with tab3:
            st.metric("Completion", f"{project_data['progress']}%")
            st.progress(project_data['progress'])
            st.write("Track your reading and practice scores here.")
    else:
        st.error("Error loading project.")
