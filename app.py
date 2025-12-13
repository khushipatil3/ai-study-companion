import streamlit as st
import fitz # PyMuPDF for PDF processing
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

        # 2. SCHEMA MIGRATION: Check and add columns
        for col_name in ['practice_data', 'analogy_data']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError as e:
                if "no such column" in str(e):
                    # st.warning(f"Database migration: Adding '{col_name}' column.")
                    c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} TEXT DEFAULT '{{}}'")
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
        # Ensure we select all columns
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
                "analogy_data": row[6]
            }
        return None
        
    def update_progress_tracker(self, project_name, concept_scores):
        """
        Updates the progress tracker JSON field within practice_data.
        concept_scores is a dict: {topic: (correct_count, total_count)}
        """
        project_data = self.get_project_details(project_name)
        if not project_data:
            return

        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        
        # Load or initialize the tracker
        tracker = json.loads(practice_dict.get('progress_tracker') or "{}")

        for concept, (correct, total) in concept_scores.items():
            if concept not in tracker:
                tracker[concept] = {"correct": 0, "total": 0}
            
            tracker[concept]["correct"] += correct
            tracker[concept]["total"] += total
        
        # Save updated tracker back into practice_data
        practice_dict['progress_tracker'] = json.dumps(tracker)

        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            UPDATE projects SET practice_data = ? WHERE name = ?
        ''', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()


db = StudyDB() # Initialize DB

# --- SESSION STATE ---
if 'current_project' not in st.session_state:
    st.session_state.current_project = None
if 'theory_marks' not in st.session_state:
    st.session_state.theory_marks = 5
if 'groq_api_key' not in st.session_state: 
    st.session_state.groq_api_key = None 
# State for interactive quiz
if 'quiz_data' not in st.session_state:
    st.session_state.quiz_data = None
if 'quiz_submitted' not in st.session_state:
    st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state:
    st.session_state.user_answers = {}
if 'quiz_type' not in st.session_state:
    st.session_state.quiz_type = 'general'


# --- HELPER FUNCTION FOR ROBUST JSON PARSING ---
def safe_json_parse(json_str):
    """Safely extracts and parses JSON content from a string, handling LLM noise."""
    if not json_str:
        return None
    
    # Attempt to find the clean JSON block (removes '```json' and leading/trailing noise)
    try:
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        
        if start_index == -1 or end_index == -1:
            # If no curly braces found, try to parse the whole thing anyway
            return json.loads(json_str.strip())

        clean_json_str = json_str[start_index:end_index + 1]
        # Remove markdown code fence markers if they exist
        if clean_json_str.startswith('```json'):
            clean_json_str = clean_json_str[len('```json'):].strip()
        if clean_json_str.endswith('```'):
            clean_json_str = clean_json_str[:-len('```')].strip()

        # Final check for valid JSON (empty check is inside the try block)
        if not clean_json_str:
            return None
            
        return json.loads(clean_json_str)
    
    except json.JSONDecodeError as e:
        # print(f"JSON Decode Error: {e}")
        return None
    except Exception as e:
        # print(f"Unexpected JSON cleaning error: {e}")
        return None


# --- LLM Functions ---

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

def _attempt_quiz_generation(system_prompt, notes_truncated, client):
    """Internal helper to call the Groq API with given prompt and notes."""
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate 10 questions in strict JSON format based on these notes: {notes_truncated}"}
            ],
            response_format={"type": "json_object"}, # Enforce JSON output
            temperature=0.8 # Use 0.8 for a good mix of question types
        )
        return completion.choices[0].message.content
    except Exception as e:
        return None


def generate_interactive_drills(notes, client):
    """Generates general interactive practice drills (MCQ, T/F) in a strict JSON format."""
    
    system_prompt = """You are a quiz master for technical subjects. Based on the notes provided, generate a quiz with 10 questions total.
    The quiz must consist of: 5 Multiple Choice Questions (MCQs), each with 4 options (A, B, C, D). 5 True or False Questions (T/F).

    Crucially, for every question, you MUST provide a 'concept_explanation'. This explanation must be a brief (1-2 sentence) summary of the core concept the question is testing, used for instant feedback.

    The entire output MUST be a single JSON object. No other text, markdown, or commentary is allowed outside the JSON structure.

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
          "concept_explanation": "The core concept being tested is X, which..."
        },
        {...}
      ]
    }
    """
    notes_truncated = notes[:15000]

    with st.spinner("Generating general practice drills..."):
        return _attempt_quiz_generation(system_prompt, notes_truncated, client)


def generate_adaptive_drills(notes, client, weak_topics):
    """
    Generates a quiz targeted at weak topics identified by the progress tracker, with a single strict attempt.
    If it fails, it returns None, triggering the fallback to the general quiz in the main loop.
    """
    weak_topics_list = ", ".join(weak_topics)
    notes_truncated = notes[:15000]

    # --- Attempt 1: Strict Target ---
    system_prompt_strict = f"""You are an adaptive quiz master. Your primary goal is to re-test the user on their known weak areas to reinforce learning.
    The user's identified weak topics are: {weak_topics_list}.
    Based on the notes, generate a quiz with 10 questions total, ensuring that **at least 7 of the 10 questions focus directly on these weak topics.**
    The quiz must consist of: 5 Multiple Choice Questions (MCQs). 5 True or False Questions (T/F).
    Provide 'concept_explanation' for every question. The entire output MUST be a single JSON object.
    JSON Format MUST match the general quiz format, but the title should be 'Interactive Practice Drill (Targeted)'.
    """
    
    with st.spinner(f"Generating targeted quiz for weak topics: {weak_topics_list} (Strict Focus)..."):
        quiz_content = _attempt_quiz_generation(system_prompt_strict, notes_truncated, client)

    # Validate if attempt 1 was successful
    if quiz_content and safe_json_parse(quiz_content):
        return quiz_content
    
    # Return None on failure to trigger the fallback to General Quiz in the calling function
    return None

        
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
            final_notes += f"(Error during generation: {e})\n\n---\n\n"
    status_text.empty()
    bar.empty()
    return final_notes

def generate_analogies(notes, client):
    system_prompt = """You are a creative tutor specializing in making complex scientific (Physics, Chemistry, Biology) and technical topics instantly relatable. Your task is to identify 5 key concepts from the provided study notes. For each concept, provide a detailed, clear, real-life analogy. Format the output strictly as a list of concepts and their analogies in clear Markdown. Use the format: '**[Concept Title]**' followed by 'Analogy: [The detailed analogy]'."""
    notes_truncated = notes[:10000]
    try:
        with st.spinner("Generating core concepts and analogies..."):
            completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Generate 5 analogies based on the following notes: {notes_truncated}"}], temperature=0.7)
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating analogies: {e}"

def generate_specific_analogy(topic, client):
    system_prompt = f"""You are a creative tutor. Your task is to provide a single, detailed, and clear real-life analogy for the concept: '{topic}'. The analogy must be highly relatable. Output only the analogy in clear Markdown, starting with the header '### Analogy for {topic}'."""
    try:
        with st.spinner(f"Generating analogy for '{topic}'..."):
            completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Generate a detailed real-life analogy for the topic: {topic}"}], temperature=0.6)
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
            completion = client.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Generate Q&A based on the following notes: {notes_truncated}"}], temperature=0.5)
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error generating Q&A: {e}"

# --- UI INTERACTIVE LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    """
    Processes the quiz results, extracts concepts, and updates the database tracker.
    Updates progress based on any attempt.
    """
    concept_scores = {} # {concept: [correct_count, total_count]}
    
    # Filter for valid questions before processing
    valid_questions = [q for q in questions if isinstance(q, dict) and 'options' in q and 'id' in q and 'concept_explanation' in q]

    for q in valid_questions:
        q_id = q['id']
        # Extract the core concept from the explanation string
        concept = q['concept_explanation'].split('is ')[-1].split(',')[0].strip().replace('.', '') 
        
        user_answer = user_answers.get(q_id)
        correct_answer = q['correct_answer']

        # Determine if the answer is correct
        is_correct = False
        if user_answer:
            if q['type'] == 'MCQ':
                is_correct = (user_answer == correct_answer)
            elif q['type'] == 'T/F':
                is_correct = (user_answer.strip() == correct_answer.strip())
        
        # Aggregate scores by concept
        if concept not in concept_scores:
            concept_scores[concept] = [0, 0]
        
        concept_scores[concept][1] += 1 # Increment total attempts
        if is_correct:
            concept_scores[concept][0] += 1 # Increment correct attempts
    
    # Convert list [correct, total] to tuple (correct, total) for the DB function
    db_scores = {k: tuple(v) for k, v in concept_scores.items()}
    
    # Update the database
    db.update_progress_tracker(project_name, db_scores)
    
    return db_scores # Return for immediate use in session state


def display_and_grade_quiz(project_name, quiz_json_str):
    """Renders the interactive quiz, collects answers, and shows instant feedback *in-place*."""
    
    quiz_data = safe_json_parse(quiz_json_str)
    
    if quiz_data is None:
        st.warning("Cannot display quiz. The quiz data could not be parsed correctly. Please try generating a new quiz.")
        return

    questions = quiz_data.get('questions', [])

    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Interactive Quiz')} ({st.session_state.quiz_type.capitalize()})")
    st.markdown("Select your answers and click **Submit Quiz** for instant feedback.")

    user_answers = st.session_state.user_answers
    
    # Render quiz form
    with st.form(key='quiz_form'):
        
        # Filter for valid questions to render
        valid_questions = [q for q in questions if isinstance(q, dict) and 'options' in q and 'id' in q and 'question_text' in q and 'type' in q]

        for q in valid_questions:
            
            q_id = q['id']
            question_key = f"q_{q_id}"
            
            # Question rendering
            with st.container():
                st.markdown(f"**Question {q_id}:** {q['question_text']}")
                
                options = q['options'] 
                user_choice = None

                # T/F questions
                if q['type'] == 'T/F':
                    options_display = ["True", "False"] 
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
                    options_display = [opt.split(': ')[1] if ': ' in opt else opt for opt in options] 
                    
                    default_index = None
                    stored_answer_letter = user_answers.get(q_id)
                    if stored_answer_letter in ['A', 'B', 'C', 'D']:
                        try:
                            index = ['A', 'B', 'C', 'D'].index(stored_answer_letter)
                            default_index = index
                        except ValueError:
                            pass

                    user_choice_text = st.radio(
                        "Your Answer:", 
                        options_display, 
                        key=question_key,
                        index=default_index,
                        disabled=st.session_state.quiz_submitted
                    )
                    
                    if user_choice_text:
                        try:
                            index = options_display.index(user_choice_text)
                            user_choice = ['A', 'B', 'C', 'D'][index]
                        except ValueError:
                            user_choice = None
                    else:
                        user_choice = None

            user_answers[q_id] = user_choice
            
            # RENDER FEEDBACK IN-PLACE
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


        col_submit, col_reset = st.columns([1, 15])
        with col_submit:
            # THIS IS THE FORM SUBMIT BUTTON
            submit_button = st.form_submit_button(label='✅ Submit Quiz', type="primary", disabled=st.session_state.quiz_submitted)
        with col_reset:
            reset_button = st.form_submit_button(label='🔄 Reset Quiz', type="secondary')

    if submit_button:
        # --- PROCESS QUIZ RESULTS & UPDATE TRACKER ---
        if st.session_state.current_project:
            process_and_update_progress(st.session_state.current_project, valid_questions, user_answers)
            
        st.session_state.quiz_submitted = True
        st.session_state.user_answers = user_answers 
        st.rerun() 
        
    if reset_button:
        st.session_state.quiz_submitted = False
        st.session_state.user_answers = {}
        st.rerun()

    if st.session_state.quiz_submitted:
        score = 0
        total_valid = len(valid_questions)
        
        for q in valid_questions:
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

        st.success(f"## Final Score: {score}/{total_valid} 🎉")
        if score == total_valid:
            st.balloons()
        
    return

# --- UTILITY FUNCTIONS ---

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
        except:
            full_content += f"\n--- PAGE_BREAK ---\n(Error extracting text on page {i+1})\n"
    progress_container.empty()
    bar.empty()
    return full_content

# --- SIDEBAR (NAVIGATION) ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    st.markdown("---")

    # --- API Key Handling ---
    final_api_key = None
    
    if "GROQ_API_KEY" in st.secrets:
        final_api_key = st.secrets["GROQ_API_KEY"]
        st.success("🔑 API Key Loaded from Secrets.")
    
    if not final_api_key and st.session_state.groq_api_key:
        final_api_key = st.session_state.groq_api_key
        st.success("🔑 API Key is configured in this session.")
        
    with st.expander("⚙️ Groq API Key Settings", expanded=not bool(final_api_key)):
        key_display_value = final_api_key if final_api_key else ""
        api_key_input = st.text_input(
            "Groq API Key (Recommended: Set in Secrets)", 
            type="password", 
            value=key_display_value,
            key="api_key_input"
        )
        if st.session_state.api_key_input and st.session_state.api_key_input != st.session_state.groq_api_key:
            st.session_state.groq_api_key = st.session_state.api_key_input
            st.rerun() 
        elif not st.session_state.api_key_input and st.session_state.groq_api_key:
            st.session_state.groq_api_key = None
            st.rerun()

    api_key_configured = bool(final_api_key)

    st.markdown("---")
    
    # LOAD PROJECTS FROM DATABASE
    saved_projects = db.load_all_projects()
    
    if saved_projects:
        st.subheader("📁 Saved Projects")
        for project_name in saved_projects:
            if st.button(f"📄 **{project_name}**", use_container_width=True, key=f"btn_{project_name}"):
                st.session_state.current_project = project_name
                st.session_state.quiz_submitted = False 
                st.session_state.user_answers = {} 
                st.session_state.quiz_data = None 
                st.session_state.quiz_type = 'general' 
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
            
            with st.spinner("Step 1: Extracting text from PDF..."):
                raw_text = extract_content_text_only(uploaded_file)
            
            if len(raw_text) > 50:
                with st.spinner("Step 2: Synthesizing notes with Groq LLM..."):
                    notes = generate_study_notes(raw_text, level, client)

                with st.spinner("Step 3: Generating initial analogies and key concepts..."):
                    default_analogies = generate_analogies(notes, client)

                analogy_data = json.dumps({"default": default_analogies})
                db.save_project(project_name, level, notes, raw_text, analogy_data=analogy_data)
                
                st.session_state.current_project = project_name
                st.success("✅ Project created, notes and analogies generated!")
                st.balloons()
                st.rerun()
            else:
                st.error("⚠️ Could not read sufficient text from document.")


# VIEW 2: PROJECT DASHBOARD
else:
    project_data = db.get_project_details(st.session_state.current_project)
    
    if project_data:
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
            
            st.subheader("Default Concepts and Analogies")
            default_analogies = analogy_data.get('default', "No default analogies found. Click 'Generate New Analogies' to create them.")
            
            if st.button("🔄 Generate Default Analogies", help="Overwrite existing default analogies with new ones based on the notes."):
                default_analogies = generate_analogies(project_data['notes'], client)
                db.update_analogy_data(project_data['name'], "default", default_analogies)
                st.rerun()

            st.markdown(default_analogies)
            st.markdown("---")
            
            st.subheader("Request a Specific Analogy")
            topic_request = st.text_input("Enter a specific concept:", key="analogy_topic_input")
            
            if st.button("🎯 Explain with Analogy"):
                if topic_request:
                    new_analogy = generate_specific_analogy(topic_request, client)
                    db.update_analogy_data(project_data['name'], topic_request, new_analogy)
                    st.session_state.analogy_request = topic_request
                    st.session_state.analogy_content = new_analogy
                    st.rerun()
                else:
                    st.warning("Please enter a concept to request an analogy.")
                    
            if st.session_state.get('analogy_request'):
                st.markdown(st.session_state.analogy_content)
            elif topic_request in analogy_data:
                 st.markdown(analogy_data[topic_request])


        # --- TAB 2: PRACTICES ---
        with tab2:
            st.header("Practice Tools")
            sub_tab1, sub_tab2 = st.tabs(["📝 Theory Q&A", "🎯 Interactive Quiz"])
            
            with sub_tab1: # THEORY Q&A 
                st.subheader("Generate Question & Answers")
                
                col_short, col_long, col_custom = st.columns(3)

                if 'qna_display_key' not in st.session_state: st.session_state.qna_display_key = None
                if 'qna_content' not in st.session_state: st.session_state.qna_content = None

                with col_short:
                    if st.button("Generate Short Answer (5 Qs)", key="btn_short", use_container_width=True):
                        qna_content = generate_qna(project_data['notes'], "short", 0, client)
                        db.update_practice_data(project_data['name'], "short_qna", qna_content)
                        st.session_state.qna_display_key = "short_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()
                
                with col_long:
                    if st.button("Generate Long Answer (3 Qs)", key="btn_long", use_container_width=True):
                        qna_content = generate_qna(project_data['notes'], "long", 0, client)
                        db.update_practice_data(project_data['name'], "long_qna", qna_content)
                        st.session_state.qna_display_key = "long_qna"
                        st.session_state.qna_content = qna_content
                        st.rerun()

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


            with sub_tab2: # INTERACTIVE QUIZ (MODIFIED FOR SMART GENERATION)
                st.subheader("Interactive Practice Quiz (MCQ & T/F)")
                
                progress_tracker = json.loads(practice_data.get('progress_tracker') or "{}")
                
                # Identify Weak Topics (Accuracy < 100%)
                weak_topics = []
                for concept, stats in progress_tracker.items():
                    # Check for any tested concept where not all attempts were correct
                    if stats['total'] > 0 and stats['correct'] / stats['total'] < 1.0: 
                        weak_topics.append(concept)
                
                # --- SINGLE SMART GENERATION BUTTON ---
                
                button_label = f"🧠 Generate Smart Quiz (Targeting {len(weak_topics)} Weak Points)" if weak_topics else "Generate New General Quiz"
                button_type = "primary" if weak_topics else "secondary"
                
                st.info(f"The next quiz will attempt to target your weak points ({len(weak_topics)}) if available, otherwise a general quiz is generated.")

                if st.button(button_label, type=button_type, key="btn_smart_drills", use_container_width=True):
                    
                    quiz_content = None
                    quiz_type = 'general'
                    
                    # 1. Attempt Adaptive Quiz (if weak points exist)
                    if weak_topics:
                        quiz_content = generate_adaptive_drills(project_data['notes'], client, weak_topics)
                        
                        if quiz_content and safe_json_parse(quiz_content):
                            quiz_type = 'adaptive'
                            st.success(f"✅ Successfully generated Adaptive Quiz targeting: {', '.join(weak_topics)}")
                        else:
                            st.error("Targeted quiz generation failed. Falling back to General Quiz for reliability.")
                            quiz_content = None # Ensure it attempts general generation
                            
                    # 2. Fallback to General Quiz (if no weak points or adaptive failed)
                    if quiz_content is None:
                        quiz_content = generate_interactive_drills(project_data['notes'], client)
                        quiz_type = 'general'
                        if weak_topics:
                            st.warning("Generated General Quiz. Try the Adaptive Quiz again after a successful General run.")

                    if quiz_content:
                        db.update_practice_data(project_data['name'], "interactive_quiz_current", quiz_content)
                        st.session_state.quiz_data = quiz_content
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.session_state.quiz_type = quiz_type
                        st.rerun()
                    else:
                        st.error("Quiz generation failed completely. Please check your notes or API key.")


                st.divider()
                
                # Load the currently active quiz
                if st.session_state.quiz_data:
                    display_and_grade_quiz(project_data['name'], st.session_state.quiz_data)
                elif practice_data.get('interactive_quiz_current'):
                    # Load the last generated quiz on session rerun if no active quiz
                    quiz_content_stored = practice_data.get('interactive_quiz_current')
                    st.session_state.quiz_data = quiz_content_stored
                    # Determine quiz type based on title for display consistency
                    quiz_type_stored = 'adaptive' if 'Targeted' in json.loads(quiz_content_stored).get('quiz_title', '') else 'general'
                    st.session_state.quiz_type = quiz_type_stored
                    display_and_grade_quiz(project_data['name'], st.session_state.quiz_data)
                else:
                    st.info("Click the 'Generate New Quiz' button above to start your practice.")

        # --- TAB 3: PROGRESS TRACKER ---
        with tab3:
            st.header("📊 Study Progress Tracker")
            
            progress_tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            
            if not progress_tracker:
                st.info("Attempt the interactive quizzes to start tracking your performance by concept.")
            else:
                st.subheader("Performance Breakdown by Concept")
                
                # Prepare data for display
                progress_list = []
                for concept, stats in progress_tracker.items():
                    total = stats['total']
                    correct = stats['correct']
                    percentage = (correct / total) * 100 if total > 0 else 0
                    
                    status = ""
                    if total == 0:
                        status = "Untested"
                    elif percentage == 100:
                        status = "Strong Concept 💪"
                    else: # percentage < 100
                        status = "Weak Point 🚨"
                    
                    progress_list.append({
                        "Concept": concept,
                        "Accuracy": f"{percentage:.1f}%",
                        "Attempts": total,
                        "Status": status
                    })
                
                # Sort by Status (Weak first, then Strong)
                sorted_progress = sorted(progress_list, key=lambda x: (x['Status'] != "Weak Point 🚨", x['Status'] == "Strong Concept 💪"), reverse=False)

                st.dataframe(
                    sorted_progress,
                    column_order=["Concept", "Accuracy", "Attempts", "Status"],
                    hide_index=True,
                    use_container_width=True
                )
                
                # Show weak topics for explicit feedback
                weak_topics_for_display = [p['Concept'] for p in sorted_progress if p['Status'] == "Weak Point 🚨"]
                
                if weak_topics_for_display:
                    st.error(f"### 🚨 Weak Points Identified:\n\nReview the notes and analogies for these weak concepts to improve: \n* " + "\n* ".join(weak_topics_for_display))
                else:
                    st.success("### Great work! All tested concepts are currently Strong Concepts. Keep up the practice!")
            
    else:
        st.error("⚠️ Error loading project data.")
