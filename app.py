import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- MODEL CONSTANT ---
# Current stable Groq model for fast, high-quality responses.
GROQ_MODEL = "llama-3.1-8b-instant" 

# --- CONFIGURABLE THRESHOLDS ---
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3          # Used for 'Low Data' message, no longer blocks adaptive logic

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
        for 'practice_data', 'analogy_data', and 'exam_analysis'.
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
                analogy_data TEXT,
                exam_analysis TEXT
            )
        ''')

        # 2. SCHEMA MIGRATION: Check and add columns
        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
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

    def save_project(self, name, level, notes, raw_text, practice_data="{}", analogy_data="{}", exam_analysis="{}"):
        """Saves a new project or updates an existing one."""
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data, analogy_data, exam_analysis))
        conn.commit()
        conn.close()

    def update_project_json_field(self, name, field_name, key, content):
        """Updates a specific key within a JSON field (e.g., practice_data, analogy_data, or exam_analysis)."""
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
        
    def update_exam_analysis_data(self, name, key, content):
        return self.update_project_json_field(name, 'exam_analysis', key, content)


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
        # Ensure we select all columns, including 'exam_analysis'
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis FROM projects WHERE name=?", (name,))
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
                "analogy_data": row[6],
                "exam_analysis": row[7]
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

    def reset_progress_tracker(self, project_name):
        """Clears the progress_tracker field for a given project."""
        project_data = self.get_project_details(project_name)
        if not project_data:
            return
            
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({}) # Set tracker to empty JSON
        
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
if 'exam_analysis_text' not in st.session_state:
    st.session_state.exam_analysis_text = None
if 'exam_analysis_pdf_content' not in st.session_state: 
    st.session_state.exam_analysis_pdf_content = ""
if 'last_uploaded_exam_pdf_id' not in st.session_state:
    st.session_state.last_uploaded_exam_pdf_id = None
if 'weak_topics' not in st.session_state: 
    st.session_state.weak_topics = []
if 'focus_quiz_active' not in st.session_state: 
    st.session_state.focus_quiz_active = False


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
        return """Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Output strictly Markdown. Insert  tags for every concept that would be better understood with a visual aid, using a detailed description for X."""

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
        # Check for API key error and report it clearly
        if 'invalid_api_key' in str(e):
             st.error("❌ API Key Error: Your Groq API key is invalid or expired. Please check your settings in the sidebar.")
        # Specific Groq error handling (e.g., context window exceeded, though unlikely at 15k)
        if 'context_length' in str(e):
             st.error("❌ Context Length Error: The notes provided are too long for the model, even after truncation. Please simplify your notes.")
        return None


def generate_interactive_drills(notes, client):
    """Generates general interactive practice drills (MCQ, T/F) in a strict JSON format."""
    
    system_prompt = """You are a quiz master for technical subjects. Based on the notes provided, generate a quiz with 10 questions total.
    Crucially, for a **GENERAL QUIZ**, ensure the 10 questions cover the **widest possible range of high-level course topics** present in the notes.
    The quiz must consist of: 5 Multiple Choice Questions (MCQs), each with 4 options (A, B, C, D). 5 True or False Questions (T/F).

    For every question, you MUST provide a 'primary_concept' and a 'detailed_explanation'.
    - The 'primary_concept' MUST be a **single, short, high-level canonical term** from the notes (e.g., 'A* Search', 'Supervised Learning', 'Logistic Regression'). **DO NOT USE SENTENCES OR LONG DESCRIPTIONS.** This is crucial for clean score tracking.
    - The 'detailed_explanation' is the brief feedback (1-2 sentence) for the user.

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
          "primary_concept": "Search Algorithms", 
          "detailed_explanation": "A* search is an informed search algorithm..."
        },
        {...}
      ]
    }
    """
    notes_truncated = notes[:15000]

    with st.spinner("Generating general practice drills..."):
        return _attempt_quiz_generation(system_prompt, notes_truncated, client)

def generate_focused_drills(notes, weak_topics, client):
    """
    Generates adaptive drills focusing only on weak topics.
    HARDENED PROMPT to prevent silent generation failure.
    """
    
    topics_list_str = ", ".join(weak_topics)
    
    # -------------------------------------------------------------------------
    # --- HARDENED SYSTEM PROMPT FOR FOCUS QUIZ ---
    system_prompt = f"""You are an ADAPTIVE quiz master for technical subjects. Based on the notes, generate a quiz with 10 questions total.
    
    ***STRICT INSTRUCTION:*** The 10 questions MUST ONLY test the following concepts. You must use these terms verbatim:
    WEAK TOPICS: {topics_list_str}
    
    The quiz must consist of a mix of Multiple Choice Questions (MCQs) and True or False Questions (T/F). Be concise in your questions and explanations.

    For every question, you MUST provide a 'primary_concept' and a 'detailed_explanation'.
    - The 'primary_concept' MUST be **one exact match** from the list: {topics_list_str}. **DO NOT ALTER OR ADD TO THESE TERMS.** This is crucial for clean score tracking.
    - The 'detailed_explanation' is the brief feedback (1-2 sentence) for the user.

    The entire output MUST be a single JSON object. No other text, markdown, or commentary is allowed outside the JSON structure.

    JSON Format MUST be:
    {{
      "quiz_title": "Adaptive Focus Drill (Weak Topics: {topics_list_str})",
      "questions": [
        {{
          "id": 1,
          "type": "MCQ",
          "question_text": "...",
          "options": ["A: ...", "B: ...", "C: ...", "D: ..."],
          "correct_answer": "B", 
          "primary_concept": "{weak_topics[0] if weak_topics else 'Concept'}", 
          "detailed_explanation": "..."
        }},
        // ... 9 more questions
      ]
    }}
    """
    # -------------------------------------------------------------------------
    
    notes_truncated = notes[:15000]

    with st.spinner(f"Generating FOCUS drills on: {topics_list_str}..."):
        # The internal helper _attempt_quiz_generation handles the API call
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
        
def analyze_past_papers(paper_content, client):
    """
    Analyzes past paper content to find key topics and repeated questions.
    This function is explicitly independent of the main study notes.
    """
    system_prompt = """You are an expert exam analyst. Your primary task is to **analyze the pattern of questions** extracted from the past exam paper content. You MUST NOT generate answers to the questions.

    Analyze the questions and the mark distribution to determine the most important topics and question patterns.

    Output must be in clear, actionable Markdown format, focusing only on the analysis:
    
    1.  **Top 5 Most Important Topics:** Extract the 5 concepts/topics that appear most frequently or are tested with the most depth in the exam questions. Rank them 1 to 5 based on frequency/weightage.
    2.  **Repeated Question Themes:** Identify questions that, while phrased differently, are essentially testing the same core information (e.g., "Explain X" and "What are the characteristics of X"). List 3-5 distinct themes.
    3.  **High-Level Strategy:** Provide a 3-point strategy for studying based *specifically* on the trends observed in the question content.

    Exam Question Content (The document you must analyze): {paper_content}
    """
    
    # Truncate content if necessary for the LLM context limit
    content_truncated = paper_content[:15000]

    try:
        with st.spinner("Analyzing past papers for trends and important topics..."):
            completion = client.chat.completions.create(
                model=GROQ_MODEL, 
                messages=[
                    {"role": "system", "content": system_prompt.format(paper_content=content_truncated)},
                    {"role": "user", "content": "Perform the exam analysis and output the results as described (Analysis only, no answers)."}
                ],
                temperature=0.4
            )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error performing exam analysis: {e}"


# --- UI INTERACTIVE LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    """
    Processes the quiz results, extracts concepts, and updates the database tracker.
    Uses 'primary_concept' for robust, high-level tracking.
    """
    concept_scores = {} # {concept: [correct_count, total_count]}
    
    # Filter for valid questions before processing
    valid_questions = [q for q in questions if isinstance(q, dict) and 'id' in q and 'primary_concept' in q]

    for q in valid_questions:
        q_id = q['id']
        
        # *** FIX for granularity: Use the high-level primary_concept directly ***
        concept = q.get('primary_concept', 'Unknown Concept - Check Quiz Data') 
        
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

        # *** FIX FOR NUMBERING: Use enumerate(valid_questions) to ensure sequential numbers (q_num) ***
        for q_index, q in enumerate(valid_questions):
            
            q_id = q['id']
            q_num = q_index + 1 # Use list index + 1 for display numbering
            question_key = f"q_{q_id}" # Use the original LLM id for consistent answer mapping
            
            # Get concept for display
            concept_display = q.get('primary_concept', 'General Concept')
            detailed_explanation = q.get('detailed_explanation', 'No detailed explanation provided.')
            
            # Question rendering
            with st.container():
                # Display the sequential q_num instead of the potentially incorrect q_id
                st.markdown(f"**Question {q_num} ({concept_display}):** {q['question_text']}")
                
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
                        <p><strong>Concept Review:</strong> {detailed_explanation}</p>
                    </div>
                    '''
                
                st.markdown(feedback_html, unsafe_allow_html=True)
            
            st.markdown("---") # Separator after each question


        col_submit, col_reset = st.columns([1, 15])
        with col_submit:
            # THIS IS THE FORM SUBMIT BUTTON
            submit_button = st.form_submit_button(label='✅ Submit Quiz', type="primary", disabled=st.session_state.quiz_submitted)
        with col_reset:
            # CORRECTED SYNTAX
            reset_button = st.form_submit_button(label='🔄 Reset Quiz', type="secondary")

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
            # If the focus quiz was active and user got 100%, disable the focus flag
            if st.session_state.focus_quiz_active:
                 st.session_state.focus_quiz_active = False # Mastery achieved!
                 st.session_state.weak_topics = [] # Clear weak topics
                 st.info("🎯 **Mastery Achieved!** You scored 100% on the focus drill. All future quizzes will be General until new weak points emerge.")
        
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
                st.session_state.exam_analysis_text = None 
                st.session_state.exam_analysis_pdf_content = "" 
                st.session_state.last_uploaded_exam_pdf_id = None
                st.session_state.weak_topics = [] # Reset weak topics on project switch
                st.session_state.focus_quiz_active = False # Reset focus flag
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
                # Save the new exam_analysis field as empty JSON
                db.save_project(project_name, level, notes, raw_text, analogy_data=analogy_data, exam_analysis="{}")
                
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
        exam_analysis_data = json.loads(project_data.get('exam_analysis') or "{}")

        # Header
        col_header, col_btn = st.columns([3, 1])
        with col_header:
            st.title(f"📘 {project_data['name']}")
            st.markdown(f"**Level:** *{project_data['level']}*")
        with col_btn:
            st.download_button("💾 Export Notes (.md)", project_data['notes'], file_name=f"{project_data['name']}_notes.md", use_container_width=True)

        st.markdown("---")

        # Tabs for Tools
        tab1, tab_analogy, tab_exam, tab2, tab3 = st.tabs(["📖 Study Notes", "💡 Analogies & Concepts", "📈 Exam Analysis", "🧠 Practices", "📊 Progress Tracker"])
        
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

        # --- TAB: EXAM ANALYSIS (FINAL REVISION) ---
        with tab_exam:
            st.header("📈 Past Paper & Question Bank Analysis")
            # Set this section to be empty until analysis is run, as requested previously
            st.markdown("""
                Upload a past paper or question bank PDF below.
                The AI will analyze the **extracted text** from the PDF to identify key trends and repeated questions.
            """)
            
            uploaded_pdf = st.file_uploader("Upload Past Paper PDF", type="pdf", key="exam_pdf_uploader")
            
            # Logic to handle PDF upload and extraction
            if uploaded_pdf:
                if not uploaded_pdf.file_id == st.session_state.get('last_uploaded_exam_pdf_id'):
                    with st.spinner("Extracting text from PDF..."):
                        pdf_text = extract_content_text_only(uploaded_pdf)
                    
                    st.session_state.exam_analysis_pdf_content = pdf_text
                    st.session_state.last_uploaded_exam_pdf_id = uploaded_file.file_id
                    
                    if len(pdf_text.strip()) < 100:
                        st.warning("⚠️ **Low Text Quality Detected.** This likely means the PDF contains scanned images of questions, which the application cannot read. The analysis will fail unless you upload a digitally created (searchable) PDF.")
                    st.info(f"Loaded **{len(pdf_text)}** characters of text for analysis.")
                    
                else:
                    st.info(f"Using previously extracted content (**{len(st.session_state.exam_analysis_pdf_content)}** characters) from the uploaded file.")

                # 3. Analysis Button
                if st.button("🎯 Run Exam Analysis", type="primary"):
                    question_content = st.session_state.exam_analysis_pdf_content
                    
                    if len(question_content.strip()) < 100:
                        st.error("The extracted text from the PDF is too short for meaningful analysis. Please check your file.")
                    else:
                        analysis_result = analyze_past_papers(
                            paper_content=question_content, 
                            client=client
                        )
                        
                        current_hash = hash(question_content)
                        analysis_key = f"analysis_{current_hash}"
                        db.update_exam_analysis_data(project_data['name'], analysis_key, analysis_result)
                        
                        st.session_state.exam_analysis_text = analysis_result
                        st.rerun() 
            else:
                st.session_state.exam_analysis_pdf_content = ""
                st.session_state.last_uploaded_exam_pdf_id = None
            
            st.divider()
            
            # Display stored or generated analysis
            analysis_to_display = st.session_state.get('exam_analysis_text')
            
            if analysis_to_display:
                st.subheader("AI Exam Analysis Report")
                st.markdown(analysis_to_display)
            else:
                st.info("Upload a past paper PDF and click 'Run Exam Analysis' to generate a report.")


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


            with sub_tab2: # INTERACTIVE QUIZ (GENERAL & ADAPTIVE)
                st.subheader("Adaptive Practice Quiz (MCQ & T/F)")
                
                # Dynamic quiz generation options based on weak topics
                weak_topics = st.session_state.weak_topics
                
                # *** NEW CHECK FOR CORRUPTED TOPICS ***
                is_data_corrupted = any(len(t) > 50 for t in weak_topics) # A single topic name should not be > 50 chars
                
                if is_data_corrupted:
                    st.error("🚨 **CRITICAL DATA ERROR:** Your progress tracker contains corrupted topic names (long sentences/descriptions from old quizzes). The Focus Quiz cannot be generated until this is fixed.")
                    st.markdown("⚠️ **Action Required:** Switch to the **📊 Progress Tracker** tab and click the **⚠️ Clear Progress Data** button to wipe the bad history and enable the Focus Quiz.")
                    st.session_state.focus_quiz_active = False # Disable focus mode until clean
                    st.session_state.quiz_type = 'general' # Force general fallback
                
                elif weak_topics and not st.session_state.focus_quiz_active:
                    st.warning(f"💡 **Recommendation:** We've identified **{len(weak_topics)}** weak topic(s). Focus on these first!")
                    st.session_state.quiz_type = 'focused' # Default to focused if weak topics exist
                elif st.session_state.focus_quiz_active:
                    st.error("🚨 **FOCUS MODE ACTIVE:** You must score 100% on the current Focus Drill to return to General Quizzes.")
                    st.session_state.quiz_type = 'focused'
                else:
                    st.success("🎉 All tested concepts are strong! Ready for a General Quiz.")
                    st.session_state.quiz_type = 'general' # Default to general
                
                # --- QUIZ SELECTION BUTTONS ---
                col_focus, col_general = st.columns([1, 1])

                with col_focus:
                    focus_disabled = not weak_topics and not st.session_state.focus_quiz_active or is_data_corrupted
                    if st.button(f"Focus Quiz ({len(weak_topics)})", key="btn_focus_select", type="secondary", disabled=focus_disabled, use_container_width=True):
                        st.session_state.quiz_type = 'focused'
                        st.session_state.focus_quiz_active = True
                        st.session_state.quiz_data = None # Clear previous quiz
                        st.session_state.quiz_submitted = False
                        st.session_state.user_answers = {}
                        st.rerun()

                with col_general:
                    general_disabled = st.session_state.focus_quiz_active
                    if st.button("General Quiz (Mixed)", key="btn_general_select", type="secondary", disabled=general_disabled, use_container_width=True):
                        st.session_state.quiz_type = 'general'
                        st.session_state.focus_quiz_active = False
                        st.session_state.quiz_data = None # Clear previous quiz
                        st.session_state.quiz_answers = {}
                        st.rerun()

                st.markdown("---")
                
                # --- GENERATION BUTTON ---
                if st.session_state.quiz_type == 'focused' and (weak_topics or st.session_state.focus_quiz_active) and not is_data_corrupted:
                    # Show generate button for FOCUS QUIZ
                    if st.button(f"Generate New **FOCUS** Quiz on ({len(weak_topics)} Topics)", type="primary", use_container_width=True, key="btn_generate_focused"):
                        
                        quiz_content = generate_focused_drills(project_data['notes'], weak_topics, client)
                        
                        if quiz_content:
                            db.update_practice_data(project_data['name'], "interactive_quiz_current", quiz_content)
                            st.session_state.quiz_data = quiz_content
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()
                        else:
                            st.error("Focus Quiz generation failed. Please try a General Quiz or ensure your API key is correct.")
                
                elif st.session_state.quiz_type == 'general' and not is_data_corrupted:
                    # Show generate button for GENERAL QUIZ
                    if st.button("Generate New **GENERAL** Quiz", type="primary", use_container_width=True, key="btn_generate_general"):
                        
                        quiz_content = generate_interactive_drills(project_data['notes'], client)
                        
                        if quiz_content:
                            db.update_practice_data(project_data['name'], "interactive_quiz_current", quiz_content)
                            st.session_state.quiz_data = quiz_content
                            st.session_state.quiz_submitted = False
                            st.session_state.user_answers = {}
                            st.rerun()
                        else:
                            st.error("General Quiz generation failed. Check notes/API key.")

                st.divider()
                
                # Load the currently active quiz
                quiz_content_stored = st.session_state.quiz_data or practice_data.get('interactive_quiz_current')

                if quiz_content_stored and not is_data_corrupted:
                    display_and_grade_quiz(project_data['name'], quiz_content_stored)
                elif not is_data_corrupted:
                    st.info("Select a quiz type and click 'Generate' to start your practice.")


        # --- TAB 3: PROGRESS TRACKER ---
        with tab3:
            st.header("📊 Study Progress Tracker")
            
            progress_tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            
            # Reset button for progress tracker
            if st.button("⚠️ Clear Progress Data", type="secondary", help="This will delete all your quiz scores and progress history for this project. Use this to reset the adaptive logic after a code update or data corruption."):
                db.reset_progress_tracker(project_data['name'])
                st.session_state.weak_topics = []
                st.session_state.focus_quiz_active = False
                st.session_state.quiz_data = None
                st.session_state.quiz_submitted = False
                st.info("✅ Progress tracker data cleared successfully. Please start a new General Quiz.")
                st.rerun()
            
            st.markdown("---")
            
            # Reset weak topics list for fresh recalculation
            current_weak_topics = []
            is_corrupted_in_tracker = False

            if not progress_tracker:
                st.info("Attempt the interactive quizzes to start tracking your performance by concept.")
            else:
                st.subheader("Performance Breakdown by Concept (High-Level Topics)")
                
                # Prepare data for display
                progress_list = []
                for concept, stats in progress_tracker.items():
                    total = stats['total']
                    correct = stats['correct']
                    percentage = (correct / total) * 100 if total > 0 else 0
                    
                    # Check for corruption here as well
                    if len(concept) > 50:
                        is_corrupted_in_tracker = True
                        status = "❗ CORRUPTED DATA"
                    else:
                        status = ""
                        
                        # Apply adaptive logic only if data is clean
                        if percentage < WEAK_TOPIC_ACCURACY_THRESHOLD * 100:
                            status = "Weak Point 🚨"
                            current_weak_topics.append(concept)
                        elif percentage == 100:
                            status = "Strong Concept 💪"
                        elif percentage >= WEAK_TOPIC_ACCURACY_THRESHOLD * 100:
                            status = "Good Progress 👍"
                            
                        # Add Low Data warning separately
                        if total < WEAK_TOPIC_MIN_ATTEMPTS:
                            if "Weak Point" not in status:
                                 status = "New/Low Data (Test More) 💡"
                            else:
                                 status += " (Low Data)"

                    progress_list.append({
                        "Concept": concept,
                        "Accuracy": f"{percentage:.1f}%",
                        "Attempts": total,
                        "Status": status
                    })
                
                # Update the session state with the new list of weak topics
                st.session_state.weak_topics = current_weak_topics

                # Sort by Status (Corrupted first, then Weak)
                sorted_progress = sorted(progress_list, key=lambda x: (x['Status'] != "❗ CORRUPTED DATA", x['Status'] != "Weak Point 🚨", x['Status'] != "New/Low Data (Test More) 💡", x['Accuracy']), reverse=False)

                st.dataframe(
                    sorted_progress,
                    column_order=["Concept", "Accuracy", "Attempts", "Status"],
                    hide_index=True,
                    use_container_width=True
                )
                
                # Show weak topics for explicit feedback
                if is_corrupted_in_tracker:
                    st.error("### 🚨 CORRUPTED DATA DETECTED! You must click the '⚠️ Clear Progress Data' button above to fix the broken topic list and enable the Focus Quiz.")
                    st.session_state.focus_quiz_active = False
                elif st.session_state.weak_topics:
                    
                    st.error(f"### 🚨 Weak Points Identified ({len(st.session_state.weak_topics)} Topics):\n\nYour scores show you need more practice on:\n* " + "\n* ".join(st.session_state.weak_topics))
                    st.markdown("Go to the **Interactive Quiz** tab and select the **Focus Quiz** option to start an adaptive practice session on these specific topics. **This will be the default quiz type until mastery is achieved.**")
                    
                    # Ensure focus is automatically activated for aggressive adaptation
                    if not st.session_state.focus_quiz_active:
                         st.session_state.focus_quiz_active = True
                         st.session_state.quiz_data = None # Clear current quiz to force new focused one
                         st.info("Focus Mode Activated. Please switch to the **Interactive Quiz** tab to begin a focused drill.")

                else:
                    st.success("### Great work! All tested concepts are currently Strong or Good Progress. Continue with General Quizzes!")
                    st.session_state.focus_quiz_active = False # Ensure focus is off if no weak topics

            
    else:
        st.error("⚠️ Error loading project data.")
