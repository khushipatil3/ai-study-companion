import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64
import sqlite3
import json

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
def init_db():
    """Creates the database table if it doesn't exist."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            level TEXT,
            notes TEXT,
            raw_text TEXT,
            progress INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_project_to_db(name, level, notes, raw_text):
    """Saves a new project or updates an existing one."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress)
        VALUES (?, ?, ?, ?, ?)
    ''', (name, level, notes, raw_text, 0))
    conn.commit()
    conn.close()

def load_all_projects():
    """Fetches a list of all project names."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute("SELECT name FROM projects")
    projects = [row[0] for row in c.fetchall()]
    conn.close()
    return projects

def get_project_details(name):
    """Fetches the full details of a specific project."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE name=?", (name,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "name": row[0],
            "level": row[1],
            "notes": row[2],
            "raw_text": row[3],
            "progress": row[4]
        }
    return None

# Initialize DB on app start
init_db()

# --- SESSION STATE ---
if 'current_project' not in st.session_state:
    st.session_state.current_project = None

# --- HELPER FUNCTIONS ---

def encode_image(pix):
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content_with_vision(uploaded_file, client):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    progress_container = st.empty()
    bar = st.progress(0)
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        bar.progress((i + 1) / total_pages)
        progress_container.caption(f"👁️ Scanning Page {i+1} of {total_pages}...")
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
            img_str = encode_image(pix)
            chat_completion = client.chat.completions.create(
                messages=[{
                    "role": "user", 
                    "content": [
                        {"type": "text", "text": "Transcribe this page into Markdown. Describe diagrams details."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}}
                    ]
                }],
                model="meta-llama/llama-4-scout-17b-16e-instruct",
            )
            full_content += f"\n--- PAGE_BREAK ---\n{chat_completion.choices[0].message.content}\n"
        except:
            full_content += f"\n--- PAGE_BREAK ---\n(Error on page {i+1})\n"

    progress_container.empty()
    bar.empty()
    return full_content

def get_system_prompt(level):
    # Fixed: Using triple quotes to prevent syntax errors on multi-line strings
    if level == "Basic":
        return """Act as a Tutor. GOAL: Pass the exam. Focus on definitions, brevity, and outlines. Add 

[Image of X]
 tags only for critical diagrams."""
    elif level == "Intermediate":
        return """Act as a Professor. GOAL: Solid understanding. Use detailed definitions, process steps, and exam tips. Insert 

[Image of X]
 tags frequently."""
    else:
        return """Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Insert  tags for everything."""

def generate_study_notes(raw_text, level, client):
    pages = raw_text.split("--- PAGE_BREAK ---")
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
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            final_notes += completion.choices[0].message.content + "\n\n---\n\n"
        except Exception as e:
            final_notes += f"(Error: {e})"
            
    status_text.empty()
    bar.empty()
    return final_notes

# --- SIDEBAR (NAVIGATION) ---
with st.sidebar:
    st.title("🗂️ My Library")
    
    # API Key Handling
    with st.expander("⚙️ Settings", expanded=not st.session_state.get('api_key_configured', False)):
        api_key_input = st.text_input("Groq API Key", type="password")
        if api_key_input:
            st.session_state.api_key = api_key_input
            st.session_state.api_key_configured = True
            st.success("Ready!")
    
    st.divider()
    
    # LOAD PROJECTS FROM DATABASE
    saved_projects = load_all_projects()
    
    if saved_projects:
        st.write("### Saved Projects")
        for project_name in saved_projects:
            if st.button(f"📄 {project_name}", use_container_width=True):
                st.session_state.current_project = project_name
                st.rerun()
                
    if st.button("+ New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN APP LOGIC ---

if not st.session_state.get('api_key'):
    st.warning("Please configure your API Key in the sidebar to start.")
    st.stop()
    
client = Groq(api_key=st.session_state.api_key)

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
            # 1. Vision Extract
            raw_text = extract_content_with_vision(uploaded_file, client)
            
            # 2. Generate Notes
            if len(raw_text) > 50:
                notes = generate_study_notes(raw_text, level, client)
                
                # 3. SAVE TO DATABASE (PERMANENT)
                save_project_to_db(project_name, level, notes, raw_text)
                
                st.session_state.current_project = project_name
                st.rerun()
            else:
                st.error("Could not read document.")

# VIEW 2: PROJECT DASHBOARD (THE WORKSPACE)
else:
    # Fetch data from DB
    project_data = get_project_details(st.session_state.current_project)
    
    if project_data:
        # Header
        col_header, col_btn = st.columns([3, 1])
        with col_header:
            st.title(f"📘 {project_data['name']}")
            st.caption(f"Level: {project_data['level']} | Status: ✅ Saved in Database")
        with col_btn:
            st.download_button("💾 Export Notes", project_data['notes'], file_name=f"{project_data['name']}.md")

        # Tabs for Tools
        tab1, tab2, tab3 = st.tabs(["📖 Study Notes", "🧠 Quiz & Practice", "📊 Progress Tracker"])
        
        with tab1:
            st.markdown(project_data['notes'])
            
        with tab2:
            st.info("🚧 Quiz and Flashcard modules coming in the next update!")
            
        with tab3:
            st.metric("Completion", f"{project_data['progress']}%")
            st.progress(project_data['progress'])
            st.write("Track your reading and quiz scores here.")
    else:
        st.error("Error loading project.")
