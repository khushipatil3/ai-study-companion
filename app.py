import streamlit as st
import fitz # PyMuPDF
from groq import Groq
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
        Creates the database table if it doesn't exist and handles schema migration
        for 'practice_data' and the NEW 'analogy_data' column.
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
                practice_data TEXT,
                analogy_data TEXT 
            )
        ''')

        # 2. SCHEMA MIGRATION: Check and add 'practice_data' column
        try:
            c.execute("SELECT practice_data FROM projects LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                st.warning("Database migration: Adding 'practice_data' column.")
                c.execute("ALTER TABLE projects ADD COLUMN practice_data TEXT DEFAULT '{}'")
            else:
                raise e 

        # 3. SCHEMA MIGRATION: Check and add the NEW 'analogy_data' column
        try:
            c.execute("SELECT analogy_data FROM projects LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                st.warning("Database migration: Adding 'analogy_data' column.")
                c.execute("ALTER TABLE projects ADD COLUMN analogy_data TEXT DEFAULT '{}'")
            else:
                raise e 
        
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text, practice_data="{}", analogy_data="{}"):
        """Saves a new project or updates an existing one."""
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data, analogy_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data, analogy_data))
        conn.commit()
        conn.close()

    def update_project_json_field(self, name, field_name, key, content):
        """Updates a specific key within a JSON field (e.g., practice_data or analogy_data)."""
        project_data = self.get_project_details(name)
        if not project_data or field_name not in project_data:
            return

        # Safely load existing data, defaulting to an empty dict
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
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row[0],
                "level": row[1],
                "notes": row[2],
                "raw_text": row[3],
                "progress": row[4],
                "practice_data": row[5],
                "analogy_data": row[6] # NEW
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
# New state for interactive quiz
if 'quiz_data' not in st.session_state:
    st.session_state.quiz_data = None
if 'quiz_submitted' not in st.session_state:
    st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state:
    st.session_state.user_answers = {}


# --- LLM Functions (Retained from previous response) ---

def extract_content_text_only(uploaded_file):
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

def generate_analogies(notes, client):
    system_prompt = """You are a creative tutor specializing in making complex scientific (Physics, Chemistry, Biology) and technical topics instantly relatable. 
    Your task is to identify 5 key concepts from the provided study notes. For each concept, provide a detailed, clear, real-life analogy. 
    Format the output strictly as a list of concepts and their analogies in clear Markdown. 
    Use the format: '**[Concept Title]**' followed by 'Analogy: [The detailed analogy]'.
    """
    notes_truncated = notes[:10000]
    
    try:
        with st.spinner("Generating core concepts and analogies..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate 5 analogies based on the following notes: {notes_truncated}"}
                ],
                temperature=0.7
            )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating analogies: {e}"

def generate_specific_analogy(topic, client):
    system_prompt = f"""You are a creative tutor. Your task is to provide a single, detailed, and clear real-life analogy for the concept: '{topic}'. 
    The analogy must be highly relatable. Output only the analogy in clear Markdown, starting with the header '### Analogy for {topic}'.
    """
    
    try:
        with st.spinner(f"Generating analogy for '{topic}'..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate a detailed real-life analogy for the topic: {topic}"}
                ],
                temperature=0.6
            )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating analogy: {e}"

def generate_qna(notes, q_type, marks, client):
    q_type_text = ""
    if q_type == "short":
        q_type_text = "5 questions requiring concise, short-answer responses (approx. 50-75 words each). Format each as Q: followed by A:."
    elif q_type == "long":
        q_type_text = "3 questions requiring detailed, long-answer responses (approx. 150-250 words each). Format each as Q: followed by A:."
    elif q_type == "custom":
        q_type_text = f"5 questions suitable for an exam where each question is worth approximately {marks} marks. The length and detail should match typical answers for that mark value. Format each as Q: followed by A:."
        
    system_prompt = f"You are a study guide generator. Your task is to analyze the provided study notes and generate {q_type_text} The output must be pure markdown."
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

def generate_interactive_drills(notes, client):
    system_prompt = """You are a quiz master for technical subjects. Based on the notes provided, generate a quiz with 10 questions total.
    The quiz must consist of:
    1. 5 Multiple Choice Questions (MCQs), each with 4 options (A, B, C, D).
    2. 5 True or False Questions (T/F).

    Crucially, for every question, you MUST provide a 'concept_explanation'. This explanation must be a brief (1-2 sentence) summary of the core concept the question is testing, used for instant feedback.

    The entire output MUST be a single JSON object. No other text, markdown, or commentary is allowed outside the JSON structure.

    JSON Format MUST be:
    {
      "quiz_title": "Interactive Practice Drill",
      "questions": [
        {
          "id": 1,
          "type": "MCQ",
          "question_text": "...",
          "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "concept_explanation": "The core concept being tested is X, which..."
        },
        {
          "id": 6,
          "type": "T/F",
          "question_text": "...",
          "options": ["True", "False"],
          "correct_answer": "False",
          "concept_explanation": "..."
        }
      ]
    }
    """
    
    notes_truncated = notes[:15000]

    try:
        with st.spinner("Generating interactive practice drills with explanations..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate 10 questions in strict JSON format based on these notes: {notes_truncated}"}
                ],
                temperature=0.8
            )
        return completion.choices[0].message.content
    except Exception as e:
        st.error(f"Error generating JSON practice drills: {e}")
        return None

# --- UI INTERACTIVE LOGIC (MODIFIED FOR IN-PLACE FEEDBACK) ---

def display_and_grade_quiz(quiz_json_str):
    """Renders the interactive quiz, collects answers, and shows instant feedback *in-place*."""
    try:
        quiz_data = json.loads(quiz_json_str)
        questions = quiz_data.get('questions', [])
    except json.JSONDecodeError:
        st.error("Could not parse quiz data. The model did not return valid JSON.")
        return

    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Interactive Quiz')}")
    st.markdown("Select your answers and click **Submit Quiz** for instant feedback.")

    # Dictionary to store user's choices
    user_answers = st.session_state.user_answers
    
    # Render quiz form
    with st.form(key='quiz_form'):
        
        # Use a list to store the rendered elements for questions and feedback
        rendered_elements = [] 

        for q in questions:
            q_id = q['id']
            question_key = f"q_{q_id}"
            
            # Start rendering the question in a container
            q_container = st.empty()
            with q_container.container():
                st.markdown(f"**Question {q_id}:** {q['question_text']}")
                
                # Render question based on type
                options = q['options']
                
                user_choice = None

                # T/F questions are simplified to Radio buttons
                if q['type'] == 'T/F':
                    options_display = ["True", "False"] 
                    # Use the stored answer if it exists for pre-selection
                    default_index = options_display.index(user_answers.get(q_id)) if user_answers.get(q_id) in options_display else None
                    
                    user_choice = st.radio(
                        "Your Answer:", 
                        options_display, 
                        key=question_key,
                        index=default_index,
                        disabled=st.session_state.quiz_submitted
                    )
                
                # MCQ questions
                elif q['type'] == 'MCQ':
                    # Extracting just the answer option text (A: ..., B: ...)
                    options_display = [opt.split(': ')[1] if ': ' in opt else opt for opt in options] 
                    
                    # If we have a stored letter answer (A, B, C, D), pre-select the corresponding text
                    default_index = None
                    stored_answer_letter = user_answers.get(q_id)
                    if stored_answer_letter in ['A', 'B', 'C', 'D']:
                        try:
                            index = ['A', 'B', 'C', 'D'].index(stored_answer_letter)
                            default_index = index
                        except ValueError:
                            pass # If somehow the index is wrong

                    user_choice_text = st.radio(
                        "Your Answer:", 
                        options_display, 
                        key=question_key,
                        index=default_index,
                        disabled=st.session_state.quiz_submitted
                    )
                    
                    # Map the selected text back to the option letter (A, B, C, D)
                    if user_choice_text:
                        try:
                            index = options_display.index(user_choice_text)
                            user_choice = ['A', 'B', 'C', 'D'][index]
                        except ValueError:
                            user_choice = None
                    else:
                        user_choice = None

            # Store the current choice regardless of submission state
            user_answers[q_id] = user_choice
            
            # *** NEW: RENDER FEEDBACK IN-PLACE ***
            if st.session_state.quiz_submitted:
                
                correct_answer = q['correct_answer']
                concept_explanation = q['concept_explanation']
                
                # Determine the displayed correct answer
                if q['type'] == 'T/F':
                    correct_display = correct_answer
                elif q['type'] == 'MCQ':
                    correct_full_option = next((opt for opt in q['options'] if opt.startswith(correct_answer + ':')), 'N/A')
                    correct_display = f"**{correct_answer}:** {correct_full_option.split(': ')[-1]}"
                else:
                    correct_display = correct_answer

                # Grading logic
                is_correct = False
                if user_choice:
                    if q['type'] == 'MCQ':
                        is_correct = (user_choice == correct_answer)
                    elif q['type'] == 'T/F':
                        is_correct = (user_choice.strip() == correct_answer.strip())

                
                # Render the feedback box
                feedback_html = ""
                
                if is_correct:
                    feedback_html = f'<div class="feedback-box correct-feedback"><p class="correct">✅ **CORRECT!**</p></div>'
                else:
                    user_selected = user_choice if user_choice else "Not answered"
                    feedback_html = f'''
                    <div class="feedback-box incorrect-feedback">
                        <p class="incorrect">❌ **INCORRECT.**</p>
                        <p><strong>Your Choice:</strong> {user_selected}</p>
                        <p><strong>Correct Answer:</strong> {correct_display}</p>
                        <p><strong>Concept Review:</strong> {concept_explanation}</p>
                    </div>
                    '''
                
                st.markdown(feedback_html, unsafe_allow_html=True)
            
            st.markdown("---") # Separator after each question


        # Submission button
        submit_button = st.form_submit_button(label='✅ Submit Quiz & Get Feedback', type="primary", disabled=st.session_state.quiz_submitted)
        reset_button = st.form_submit_button(label='🔄 Reset Quiz', type="secondary")

    if submit_button:
        st.session_state.quiz_submitted = True
        st.session_state.user_answers = user_answers # Save current state
        st.rerun() # Rerun to display the in-place results
        
    if reset_button:
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

    if st.session_state.quiz_submitted:
        score = 0
        for q in questions:
            user_answer = st.session_state.user_answers.get(q['id'])
            correct_answer = q['correct_answer']
            
            is_correct = False
            if user_answer:
                if q['type'] == 'MCQ':
                    is_correct = (user_answer == correct_answer)
                elif q['type'] == 'T/F':
                    is_correct = (user_answer.strip() == correct_answer.strip())
            
            if is_correct:
                score += 1

        st.success(f"## Final Score: {score}/{len(questions)} 🎉")
        if score == len(questions):
            st.balloons()


# --- MAIN APP EXECUTION (Sidebar and Layout functions remain the same) ---

db = StudyDB() 

# --- SIDEBAR (NAVIGATION) ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    st.markdown("---")

    # --- API Key Handling ---
    final_api_key = None
    
    # Check Streamlit Secrets (for deployed app)
    if "GROQ_API_KEY" in st.secrets:
        final_api_key = st.secrets["GROQ_API_KEY"]
        st.success("🔑 API Key Loaded from Secrets.")
    
    # Check Session State (for user input)
    if not final_api_key and st.session_state.groq_api_key:
        final_api_key = st.session_state.groq_api_key
        st.success("🔑 API Key is configured in this session.")
        
    # Handle User Input
    with st.expander("⚙️ Groq API Key Settings", expanded=not bool(final_api_key)):
        key_display_value = final_api_key if final_api_key else ""
        
        api_key_input = st.text_input(
            "Groq API Key (Recommended: Set in Secrets)", 
            type="password", 
            value=key_display_value,
            key="api_key_input" # Unique key for this widget
        )
        
        if st.session_state.api_key_input and st.session_state.api_key_input != st.session_state.groq_api_key:
            st.session_state.groq_api_key = st.session_state.api_key_input
            st.rerun() 
        elif not st.session_state.api_key_input and st.session_state.groq_api_key:
            st.session_state.groq_api_key = None
            st.rerun()

    # Set the final flag
    api_key_configured = bool(final_api_key)

    st.markdown("---")
    
    # LOAD PROJECTS FROM DATABASE
    saved_projects = db.load_all_projects()
    
    if saved_projects:
        st.subheader("📁 Saved Projects")
        for project_name in saved_projects:
            # Use a slightly more visually distinct button
            if st.button(f"📄 **{project_name}**", use_container_width=True, key=f"btn_{project_name}"):
                st.session_state.current_project = project_name
                st.session_state.quiz_submitted = False # Reset quiz state when switching projects
                st.session_state.user_answers = {} # Clear answers
                st.rerun()
        st.markdown("---")
                
    if st.button("➕ Create New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN APP LOGIC ---

if not api_key_configured:
    st.warning("🚨 Please configure your Groq API Key in the sidebar settings to start.")
    st.stop()
    
try:
    # Initialize Groq client using the determined key
    client = Groq(api_key=final_api_key)
except Exception as e:
    st.error(f"❌ Error initializing Groq client. Please check your API key. Details: {e}")
    st.stop()


# VIEW 1: CREATE NEW PROJECT
if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    st.markdown("### Upload a document (PDF) and define your study level.")
    
    uploaded_file = st.file_uploader("Upload PDF Document", type="pdf")
    
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2:
            level = st.select_slider("Detail Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
            
        if st.button("✨ Create & Generate Study Guide", type="primary"):
            
            # 1. Text Extraction
            with st.spinner("Step 1: Extracting text from PDF..."):
                raw_text = extract_content_text_only(uploaded_file)
            
            # 2. Generate Notes
            if len(raw_text) > 50:
                with st.spinner("Step 2: Synthesizing notes with Groq LLM..."):
                    notes = generate_study_notes(raw_text, level, client)

                # 3. Generate Default Analogies
                with st.spinner("Step 3: Generating initial analogies and key concepts..."):
                    default_analogies = generate_analogies(notes, client)

                # 4. SAVE TO DATABASE
                # Save the generated analogies under the 'default' key in analogy_data
                analogy_data = json.dumps({"default": default_analogies})
                db.save_project(project_name, level, notes, raw_text, analogy_data=analogy_data)
                
                st.session_state.current_project = project_name
                st.success("✅ Project created, notes and analogies generated!")
                st.balloons()
                st.rerun()
            else:
                st.error("⚠️ Could not read sufficient text from document.")


# VIEW 2: PROJECT DASHBOARD (UPDATED WORKSPACE)
else:
    # Fetch data from DB
    project_data = db.get_project_details(st.session_state.current_project)
    
    if project_data:
        # Load JSON fields
        practice_data = json.loads(project_data.get('practice_data') or "{}")
        analogy_data = json.loads(project_data.get('analogy_data') or "{}")

        # Header
        col_header, col_btn = st.columns([3, 1])
        with col_header:
            st.title(f"📘 {project_data['name']}")
            st.markdown(f"**Level:** *{project_data['level']}*")
        with col_btn:
            st.download_button("💾 Export Notes (.md)", project_data['notes'], file_name=f"{project_data['name']}_notes.md", use_container_width=True)

        st.markdown("---")

        # Tabs for Tools
        tab1, tab_analogy, tab2, tab3 = st.tabs(["📖 Study Notes", "💡 Analogies & Concepts", "🧠 Practices", "📊 Progress Tracker"])
        
        # --- TAB 1: STUDY NOTES ---
        with tab1:
            st.header("Comprehensive Study Guide")
            st.markdown(project_data['notes'])
            
        # --- TAB: ANALOGIES & CONCEPTS ---
        with tab_analogy:
            st.header("Real-Life Analogies for Better Understanding")
            
            # 1. Default Analogies Section
            st.subheader("Default Concepts and Analogies")
            default_analogies = analogy_data.get('default', "No default analogies found. Click 'Generate New Analogies' to create them.")
            
            if st.button("🔄 Generate Default Analogies", help="Overwrite existing default analogies with new ones based on the notes."):
                default_analogies = generate_analogies(project_data['notes'], client)
                db.update_analogy_data(project_data['name'], "default", default_analogies)
                st.rerun()

            st.markdown(default_analogies)
            st.markdown("---")
            
            # 2. User-Requested Analogy Section
            st.subheader("Request a Specific Analogy")
            topic_request = st.text_input("Enter a specific concept (e.g., 'Principle of Superposition', 'Le Chatelier's Principle'):", key="analogy_topic_input")
            
            if st.button("🎯 Explain with Analogy"):
                if topic_request:
                    new_analogy = generate_specific_analogy(topic_request, client)
                    
                    # Store the requested analogy using the topic as the key
                    db.update_analogy_data(project_data['name'], topic_request, new_analogy)
                    st.session_state.analogy_request = topic_request
                    st.session_state.analogy_content = new_analogy
                    st.rerun()
                else:
                    st.warning("Please enter a concept to request an analogy.")
                    
            # Display requested analogy (either newly generated or from session)
            if st.session_state.get('analogy_request'):
                st.markdown(st.session_state.analogy_content)
            elif topic_request in analogy_data:
                 st.markdown(analogy_data[topic_request])


        # --- TAB 2: PRACTICES ---
        with tab2:
            st.header("Practice Tools")
            # New sub-tabs for Theory and Practice
            sub_tab1, sub_tab2 = st.tabs(["📝 Theory Q&A", "🎯 Interactive Quiz"])
            
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
                    if st.button("Generate Short Answer (5 Qs)", key="btn_short", use_container_width=True):
                        qna_content = generate_qna(project_data['notes'], "short", 0, client)
                        db.update_practice_data(project_data['name'], "short_qna", qna_content)
                        st.session_state.qna_display_key = "short_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()
                
                # --- LONG ANSWER ---
                with col_long:
                    if st.button("Generate Long Answer (3 Qs)", key="btn_long", use_container_width=True):
                        qna_content = generate_qna(project_data['notes'], "long", 0, client)
                        db.update_practice_data(project_data['name'], "long_qna", qna_content)
                        st.session_state.qna_display_key = "long_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()

                # --- CUSTOM ANSWER ---
                with col_custom:
                    st.session_state.theory_marks = st.number_input("Custom Mark Value", min_value=1, max_value=25, value=st.session_state.theory_marks, key="mark_input")
                    custom_key = f"custom_qna_{st.session_state.theory_marks}"
                    if st.button(f"Generate Custom ({st.session_state.theory_marks} Marks)", key="btn_custom", type="secondary", use_container_width=True):
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
                    display_content = st.session_state.qna_content
                elif display_key in practice_data:
                    display_content = practice_data[display_key]
                else:
                    display_content = practice_data.get("long_qna") or practice_data.get("short_qna")

                if display_content:
                    st.markdown(display_content)
                else:
                    st.info("Select a generation type above to create your Theory Q&A!")


            with sub_tab2: # INTERACTIVE QUIZ
                st.subheader("Interactive Practice Quiz (MCQ & T/F)")
                
                if st.button("Generate New Interactive Quiz", type="primary", key="btn_interactive_drills"):
                    quiz_content = generate_interactive_drills(project_data['notes'], client)
                    if quiz_content:
                        db.update_practice_data(project_data['name'], "interactive_quiz", quiz_content)
                        st.session_state.quiz_data = quiz_content
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()

                st.divider()
                
                # Load quiz data from session state or database on tab load
                if st.session_state.quiz_data is None and practice_data.get('interactive_quiz'):
                    st.session_state.quiz_data = practice_data.get('interactive_quiz')

                if st.session_state.quiz_data:
                    # Display the interactive quiz and handle grading
                    display_and_grade_quiz(st.session_state.quiz_data)
                else:
                    st.info("Click the button above to generate a new interactive quiz.")

        # --- TAB 3: PROGRESS TRACKER ---
        with tab3:
            st.header("Study Progress")
            st.metric("Completion Rate", f"{project_data['progress']}%")
            st.progress(project_data['progress'])
            st.info("Future features here will track performance on quizzes and time spent studying.")
            
    else:
        st.error("⚠️ Error loading project data.")
