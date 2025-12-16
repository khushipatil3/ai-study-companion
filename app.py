import streamlit as st
import fitz # PyMuPDF for PDF processing
from groq import Groq
import sqlite3
import json
import base64

# --- CONFIGURATION ---
GROQ_MODEL = "llama-3.1-8b-instant"
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80 # Below 80% is weak
WEAK_TOPIC_MIN_ATTEMPTS = 3          # Used for 'Low Data' message, no longer blocks adaptive logic

# --- DATABASE LAYER (SQLite) ---
class StudyDB:
    def __init__(self, db_name='study_db.sqlite'):
        self.db_name = db_name
        self.init_db()

    def connect(self):
        return sqlite3.connect(self.db_name)

    def init_db(self):
        """
        Creates the database table if it doesn't exist and handles schema migration.
        """
        conn = self.connect()
        c = conn.cursor()
        
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

        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError as e:
                if "no such column" in str(e):
                    c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} TEXT DEFAULT '{{}}'")
        
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

    def get_project_details(self, name):
        """Fetches the full details of a specific project."""
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3],
                "progress": row[4], "practice_data": row[5], "analogy_data": row[6],
                "exam_analysis": row[7]
            }
        return None
        
    def update_progress_tracker(self, project_name, concept_scores):
        """Updates the progress tracker JSON field within practice_data."""
        project_data = self.get_project_details(project_name)
        if not project_data: return

        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        tracker = json.loads(practice_dict.get('progress_tracker') or "{}")

        for concept, (correct, total) in concept_scores.items():
            if concept not in tracker:
                tracker[concept] = {"correct": 0, "total": 0}
            
            tracker[concept]["correct"] += correct
            tracker[concept]["total"] += total
        
        practice_dict['progress_tracker'] = json.dumps(tracker)

        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET practice_data = ? WHERE name = ? ''', (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

    def reset_progress_tracker(self, project_name):
        """Clears the progress_tracker field for a given project."""
        project_data = self.get_project_details(project_name)
        if not project_data: return
            
        practice_dict = json.loads(project_data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({}) 
        
        conn = self.connect()
        c = conn.cursor()
        c.execute(''' UPDATE projects SET practice_data = ? WHERE name = ? ''', (json.dumps(practice_dict), project_name))
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

    def update_analogy_data(self, name, key, content):
        """Updates a key within the analogy_data JSON field."""
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get('analogy_data') or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f''' UPDATE projects SET analogy_data = ? WHERE name = ? ''', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()

    def update_exam_analysis_data(self, name, key, content):
        """Updates a key within the exam_analysis JSON field."""
        project_data = self.get_project_details(name)
        if not project_data: return
        data_dict = json.loads(project_data.get('exam_analysis') or "{}")
        data_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f''' UPDATE projects SET exam_analysis = ? WHERE name = ? ''', (json.dumps(data_dict), name))
        conn.commit()
        conn.close()


db = StudyDB() # Initialize DB (single instance)

# --- UTILITY & ADAPTIVE LOGIC ---

def safe_json_parse(json_str):
    """Safely extracts and parses JSON content from a string, handling LLM noise."""
    if not json_str: return None
    try:
        start_index = json_str.find('{')
        end_index = json_str.rfind('}')
        if start_index == -1 or end_index == -1: return json.loads(json_str.strip())
        clean_json_str = json_str[start_index:end_index + 1]
        if clean_json_str.startswith('```json'): clean_json_str = clean_json_str[len('```json'):].strip()
        if clean_json_str.endswith('```'): clean_json_str = clean_json_str[:-len('```')].strip()
        if not clean_json_str: return None
        return json.loads(clean_json_str)
    except json.JSONDecodeError: return None
    except Exception: return None

def extract_content_text_only(uploaded_file):
    """Extracts text from a PDF file."""
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

def determine_weak_topics(project_data, accuracy_threshold):
    """Calculates weak topics based on the progress tracker data."""
    practice_data = json.loads(project_data.get('practice_data') or "{}")
    tracker = json.loads(practice_data.get('progress_tracker') or "{}")
    
    current_weak_topics = []
    
    for concept, stats in tracker.items():
        total = stats['total']
        correct = stats['correct']
        percentage = (correct / total) if total > 0 else 1.0
        
        # Check for corrupted topic names (long sentences/descriptions)
        if len(concept) > 50:
             continue
        
        # Adaptive Logic: Flag as weak if accuracy is low (regardless of attempts)
        if percentage < accuracy_threshold:
            current_weak_topics.append(concept)
            
    return current_weak_topics

# --- LLM API & GENERATION LOGIC ---

def initialize_client(api_key):
    """Initializes and returns the Groq client if key is valid."""
    if not api_key:
        return None
    try:
        return Groq(api_key=api_key)
    except Exception:
        return None

def _attempt_quiz_generation(system_prompt, notes_truncated, client):
    """Internal helper to call the Groq API with given prompt and notes, enforcing JSON output."""
    if not client:
        return None
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate 10 questions in strict JSON format based on these notes: {notes_truncated}"}
            ],
            response_format={"type": "json_object"}, # Enforce JSON output
            temperature=0.8
        )
        return completion.choices[0].message.content
    except Exception as e:
        if 'invalid_api_key' in str(e):
             st.error("❌ API Key Error: Your Groq API key is invalid or expired. Please check your settings in the sidebar.")
        if 'context_length' in str(e):
             st.error("❌ Context Length Error: The notes provided are too long for the model, even after truncation. Please simplify your notes.")
        return None

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
        return """Act as a Subject Matter Expert. GOAL: Mastery. Explain nuances, real-world context, and deep connections. Output strictly Markdown. Insert tags for every concept that would be better understood with a visual aid, using a detailed description for X."""

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
    
    notes_truncated = notes[:15000]
    with st.spinner(f"Generating FOCUS drills on: {topics_list_str}..."):
        return _attempt_quiz_generation(system_prompt, notes_truncated, client)


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
    
    1.  **Top 5 Most Important Topics:** Extract the 5 concepts/topics that appear most frequently or are tested with the most depth in the exam questions. Rank them 1 to 5 based on frequency/weightage.
    2.  **Repeated Question Themes:** Identify questions that, while phrased differently, are essentially testing the same core information (e.g., "Explain X" and "What are the characteristics of X"). List 3-5 distinct themes.
    3.  **High-Level Strategy:** Provide a 3-point strategy for studying based *specifically* on the trends observed in the question content.

    Exam Question Content (The document you must analyze): {paper_content}
    """
    
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


# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .main {
        background-color: #f0f2f6;
        color: #1c1e21;
    }
    .css-1d3f8rz {
        background-color: #ffffff;
    }
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
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
        background-color: #e6ffe6;
        border-left: 5px solid green;
    }
    .incorrect-feedback {
        background-color: #ffe6e6;
        border-left: 5px solid red;
    }
</style>
""", unsafe_allow_html=True)


# --- SESSION STATE ---
if 'current_project' not in st.session_state: st.session_state.current_project = None
if 'theory_marks' not in st.session_state: st.session_state.theory_marks = 5
if 'groq_api_key' not in st.session_state: st.session_state.groq_api_key = None 
if 'quiz_data' not in st.session_state: st.session_state.quiz_data = None
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'user_answers' not in st.session_state: st.session_state.user_answers = {}
if 'quiz_type' not in st.session_state: st.session_state.quiz_type = 'general'
if 'exam_analysis_text' not in st.session_state: st.session_state.exam_analysis_text = None
if 'exam_analysis_pdf_content' not in st.session_state: st.session_state.exam_analysis_pdf_content = ""
if 'last_uploaded_exam_pdf_id' not in st.session_state: st.session_state.last_uploaded_exam_pdf_id = None
if 'weak_topics' not in st.session_state: st.session_state.weak_topics = []
if 'focus_quiz_active' not in st.session_state: st.session_state.focus_quiz_active = False


# --- UI INTERACTIVE LOGIC ---

def process_and_update_progress(project_name, questions, user_answers):
    concept_scores = {}
    valid_questions = [q for q in questions if isinstance(q, dict) and 'id' in q and 'primary_concept' in q]

    for q in valid_questions:
        concept = q.get('primary_concept', 'Unknown Concept') 
        q_id = q['id']
        user_answer = user_answers.get(q_id)
        correct_answer = q['correct_answer']

        is_correct = False
        if user_answer:
            if q['type'] == 'MCQ':
                is_correct = (user_answer == correct_answer)
            elif q['type'] == 'T/F':
                is_correct = (user_answer.strip() == correct_answer.strip())
        
        if concept not in concept_scores:
            concept_scores[concept] = [0, 0]
        
        concept_scores[concept][1] += 1
        if is_correct:
            concept_scores[concept][0] += 1
    
    db_scores = {k: tuple(v) for k, v in concept_scores.items()}
    db.update_progress_tracker(project_name, db_scores)
    return db_scores


def display_and_grade_quiz(project_name, quiz_json_str):
    quiz_data = safe_json_parse(quiz_json_str)
    
    if quiz_data is None:
        st.warning("Cannot display quiz. The quiz data could not be parsed correctly. Please try generating a new quiz.")
        return

    questions = quiz_data.get('questions', [])

    st.subheader(f"🎯 {quiz_data.get('quiz_title', 'Interactive Quiz')} ({st.session_state.quiz_type.capitalize()})")
    st.markdown("Select your answers and click **Submit Quiz** for instant feedback.")

    user_answers = st.session_state.user_answers
    
    with st.form(key='quiz_form'):
        valid_questions = [q for q in questions if isinstance(q, dict) and 'options' in q and 'id' in q and 'question_text' in q and 'type' in q]

        for q_index, q in enumerate(valid_questions):
            
            q_id = q['id']
            q_num = q_index + 1 
            question_key = f"q_{q_id}"
            
            concept_display = q.get('primary_concept', 'General Concept')
            detailed_explanation = q.get('detailed_explanation', 'No detailed explanation provided.')
            
            with st.container():
                st.markdown(f"**Question {q_num} ({concept_display}):** {q['question_text']}")
                
                options = q['options'] 
                user_choice = None

                if q['type'] == 'T/F':
                    options_display = ["True", "False"] 
                    default_index = options_display.index(user_answers.get(q_id)) if user_answers.get(q_id) in options_display else None
                    
                    user_choice = st.radio("Your Answer:", options_display, key=question_key, index=default_index, disabled=st.session_state.quiz_submitted)
                
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

                    user_choice_text = st.radio("Your Answer:", options_display, key=question_key, index=default_index, disabled=st.session_state.quiz_submitted)
                    
                    if user_choice_text:
                        try:
                            index = options_display.index(user_choice_text)
                            user_choice = ['A', 'B', 'C', 'D'][index]
                        except ValueError:
                            user_choice = None
                    else:
                        user_choice = None

            user_answers[q_id] = user_choice
            
            if st.session_state.quiz_submitted:
                correct_answer = q['correct_answer']
                
                if q['type'] == 'T/F':
                    correct_display = correct_answer
                elif q['type'] == 'MCQ':
                    correct_full_option = next((opt for opt in q['options'] if opt.startswith(correct_answer + ':')), 'N/A')
                    correct_display = f"**{correct_answer}:** {correct_full_option.split(': ')[-1]}"
                else:
                    correct_display = correct_answer

                is_correct = False
                if user_choice:
                    if q['type'] == 'MCQ':
                        is_correct = (user_choice == correct_answer)
                    elif q['type'] == 'T/F':
                        is_correct = (user_choice.strip() == correct_answer.strip())

                
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
            
            st.markdown("---")


        col_submit, col_reset = st.columns([1, 15])
        with col_submit:
            submit_button = st.form_submit_button(label='✅ Submit Quiz', type="primary", disabled=st.session_state.quiz_submitted)
        with col_reset:
            reset_button = st.form_submit_button(label='🔄 Reset Quiz', type="secondary")

    if submit_button:
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
        score = sum(1 for q in valid_questions if q.get('correct_answer') == st.session_state.user_answers.get(q['id']))
        total_valid = len(valid_questions)
        
        st.success(f"## Final Score: {score}/{total_valid} 🎉")
        if score == total_valid:
            st.balloons()
            if st.session_state.focus_quiz_active:
                 st.session_state.focus_quiz_active = False
                 st.session_state.weak_topics = []
                 st.info("🎯 **Mastery Achieved!** You scored 100% on the focus drill.")
        
    return

# --- SIDEBAR ---
with st.sidebar:
    st.title("📚 AI Study Companion")
    st.markdown("---")

    final_api_key = None
    
    if "GROQ_API_KEY" in st.secrets:
        final_api_key = st.secrets["GROQ_API_KEY"]
    
    if not final_api_key and st.session_state.groq_api_key:
        final_api_key = st.session_state.groq_api_key
        
    with st.expander("⚙️ Groq API Key Settings", expanded=not bool(final_api_key)):
        key_display_value = final_api_key if final_api_key else ""
        api_key_input = st.text_input(
            "Groq API Key", 
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
                st.session_state.weak_topics = [] 
                st.session_state.focus_quiz_active = False 
                st.rerun()
        st.markdown("---")
                
    if st.button("➕ Create New Project", type="primary", use_container_width=True):
        st.session_state.current_project = None
        st.rerun()

# --- MAIN APP LOGIC ---

if not api_key_configured:
    st.warning("🚨 Please configure your Groq API Key.")
    st.stop()
    
try:
    client = initialize_client(final_api_key)
except Exception as e:
    st.error(f"❌ Error initializing Groq client.")
    st.stop()


# VIEW 1: CREATE
if st.session_state.current_project is None:
    st.title("🚀 New Study Project")
    uploaded_file = st.file_uploader("Upload PDF Document", type="pdf")
    
    if uploaded_file:
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Project Name", value=uploaded_file.name.split('.')[0])
        with col2:
            level = st.select_slider("Detail Level", options=["Basic", "Intermediate", "Advanced"], value="Intermediate")
            
        if st.button("✨ Create & Generate Study Guide", type="primary"):
            with st.spinner("Extracting text..."):
                raw_text = extract_content_text_only(uploaded_file)
            
            if len(raw_text) > 50:
                with st.spinner("Synthesizing notes..."):
                    notes = generate_study_notes(raw_text, level, client)
                with st.spinner("Generating analogies..."):
                    default_analogies = generate_analogies(notes, client)

                analogy_data = json.dumps({"default": default_analogies})
                db.save_project(project_name, level, notes, raw_text, analogy_data=analogy_data, exam_analysis="{}")
                st.session_state.current_project = project_name
                st.rerun()


# VIEW 2: DASHBOARD
else:
    project_data = db.get_project_details(st.session_state.current_project)
    
    if project_data:
        practice_data = json.loads(project_data.get('practice_data') or "{}")
        analogy_data = json.loads(project_data.get('analogy_data') or "{}")
        exam_analysis_data = json.loads(project_data.get('exam_analysis') or "{}")

        col_header, col_btn = st.columns([3, 1])
        with col_header:
            st.title(f"📘 {project_data['name']}")
        with col_btn:
            st.download_button("💾 Export Notes (.md)", project_data['notes'], file_name=f"{project_data['name']}_notes.md", use_container_width=True)

        st.markdown("---")

        tab1, tab_analogy, tab_exam, tab2, tab3 = st.tabs(["📖 Study Notes", "💡 Analogies & Concepts", "📈 Exam Analysis", "🧠 Practices", "📊 Progress Tracker"])
        
        with tab1:
            st.markdown(project_data['notes'])
            
        with tab_analogy:
            st.subheader("Default Concepts")
            st.markdown(analogy_data.get('default', "No default analogies."))
            st.markdown("---")
            topic_request = st.text_input("Enter a specific concept:")
            if st.button("🎯 Explain with Analogy"):
                if topic_request:
                    new_analogy = generate_specific_analogy(topic_request, client)
                    db.update_analogy_data(project_data['name'], topic_request, new_analogy)
                    st.session_state.analogy_request = topic_request
                    st.session_state.analogy_content = new_analogy
                    st.rerun()
            if st.session_state.get('analogy_request'):
                st.markdown(st.session_state.analogy_content)

        with tab_exam:
            st.header("📈 Exam Analysis")
            uploaded_pdf = st.file_uploader("Upload Past Paper PDF", type="pdf")
            if uploaded_pdf:
                if st.button("🎯 Run Exam Analysis", type="primary"):
                    pdf_text = extract_content_text_only(uploaded_pdf)
                    analysis_result = analyze_past_papers(pdf_text, client)
                    st.session_state.exam_analysis_text = analysis_result
                    st.rerun() 
            if st.session_state.get('exam_analysis_text'):
                st.markdown(st.session_state.exam_analysis_text)

        with tab2:
            sub_tab1, sub_tab2 = st.tabs(["📝 Theory Q&A", "🎯 Interactive Quiz"])
            with sub_tab1:
                col_short, col_long, col_custom = st.columns(3)
                if col_short.button("Short (5 Qs)", use_container_width=True):
                    qna = generate_qna(project_data['notes'], "short", 0, client)
                    st.session_state.qna_content = qna
                    st.rerun()
                if col_long.button("Long (3 Qs)", use_container_width=True):
                    qna = generate_qna(project_data['notes'], "long", 0, client)
                    st.session_state.qna_content = qna
                    st.rerun()
                st.markdown(st.session_state.get('qna_content', "Generate Q&A!"))

            with sub_tab2:
                weak_topics = determine_weak_topics(project_data, WEAK_TOPIC_ACCURACY_THRESHOLD)
                if st.button("Generate General Quiz", type="primary", use_container_width=True):
                    quiz_content = generate_interactive_drills(project_data['notes'], client)
                    st.session_state.quiz_data = quiz_content
                    st.session_state.quiz_submitted = False
                    st.rerun()
                if st.session_state.quiz_data:
                    display_and_grade_quiz(project_data['name'], st.session_state.quiz_data)

        with tab3:
            st.header("📊 Progress Tracker")
            progress_tracker = json.loads(practice_data.get('progress_tracker') or "{}")
            if not progress_tracker:
                st.info("Take a quiz!")
            else:
                st.table(progress_tracker)
