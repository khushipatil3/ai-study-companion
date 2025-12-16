import streamlit as st
import fitz # PyMuPDF
from groq import Groq
import sqlite3
import json
import base64

# --- CONFIGURATION ---
GROQ_MODEL = "llama-3.1-8b-instant"
WEAK_TOPIC_ACCURACY_THRESHOLD = 0.80
WEAK_TOPIC_MIN_ATTEMPTS = 3

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
                progress INTEGER DEFAULT 0,
                practice_data TEXT,
                analogy_data TEXT,
                exam_analysis TEXT
            )
        ''')
        for col_name in ['practice_data', 'analogy_data', 'exam_analysis']:
            try:
                c.execute(f"SELECT {col_name} FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col_name} TEXT DEFAULT '{{}}'")
        conn.commit()
        conn.close()

    def save_project(self, name, level, notes, raw_text, practice_data="{}", analogy_data="{}", exam_analysis="{}"):
        conn = self.connect()
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO projects (name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, level, notes, raw_text, 0, practice_data, analogy_data, exam_analysis))
        conn.commit()
        conn.close()

    def get_project_details(self, name):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name, level, notes, raw_text, progress, practice_data, analogy_data, exam_analysis FROM projects WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"name": row[0], "level": row[1], "notes": row[2], "raw_text": row[3], "progress": row[4], "practice_data": row[5], "analogy_data": row[6], "exam_analysis": row[7]}
        return None

    def update_project_field(self, name, field, key, content):
        data = self.get_project_details(name)
        if not data: return
        field_dict = json.loads(data.get(field) or "{}")
        field_dict[key] = content
        conn = self.connect()
        c = conn.cursor()
        c.execute(f"UPDATE projects SET {field} = ? WHERE name = ?", (json.dumps(field_dict), name))
        conn.commit()
        conn.close()

    def update_progress_tracker(self, project_name, concept_scores):
        data = self.get_project_details(project_name)
        practice_dict = json.loads(data.get('practice_data') or "{}")
        tracker = json.loads(practice_dict.get('progress_tracker') or "{}")
        for concept, (correct, total) in concept_scores.items():
            if concept not in tracker: tracker[concept] = {"correct": 0, "total": 0}
            tracker[concept]["correct"] += correct
            tracker[concept]["total"] += total
        practice_dict['progress_tracker'] = json.dumps(tracker)
        conn = self.connect()
        c = conn.cursor()
        c.execute("UPDATE projects SET practice_data = ? WHERE name = ?", (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

    def reset_progress_tracker(self, project_name):
        data = self.get_project_details(project_name)
        practice_dict = json.loads(data.get('practice_data') or "{}")
        practice_dict['progress_tracker'] = json.dumps({})
        conn = self.connect()
        c = conn.cursor()
        c.execute("UPDATE projects SET practice_data = ? WHERE name = ?", (json.dumps(practice_dict), project_name))
        conn.commit()
        conn.close()

    def load_all_projects(self):
        conn = self.connect()
        c = conn.cursor()
        c.execute("SELECT name FROM projects")
        names = [row[0] for row in c.fetchall()]
        conn.close()
        return names

db = StudyDB()

# --- HARDENED JSON PARSER ---
def safe_json_parse(json_str):
    if not json_str: 
        return None
    try:
        clean_str = json_str.replace('```json', '').replace('```', '').strip()
        start = clean_str.find('{')
        end = clean_str.rfind('}')
        if start != -1 and end != -1:
            clean_str = clean_str[start:end+1]
        parsed = json.loads(clean_str)
        if isinstance(parsed, dict) and "questions" in parsed:
            return parsed
        elif isinstance(parsed, list):
            return {"questions": parsed}
        return None
    except:
        return None

def extract_pdf_text(uploaded_file):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    return "\n--- PAGE_BREAK ---\n".join([page.get_text() for page in doc])

def determine_weak_topics(project_data):
    practice_data = json.loads(project_data.get('practice_data') or "{}")
    tracker = json.loads(practice_data.get('progress_tracker') or "{}")
    return [c for c, s in tracker.items() if (s['correct']/s['total'] if s['total'] > 0 else 1) < WEAK_TOPIC_ACCURACY_THRESHOLD and len(c) < 50]

# --- LLM LOGIC ---
def get_client(api_key):
    return Groq(api_key=api_key) if api_key else None

def generate_content(prompt, client, is_json=False):
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a strict academic assistant. You generate content based ONLY on the provided user context. You never hallucinate or use external general knowledge."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"} if is_json else None,
            temperature=0.3 # Lower temperature for higher factual accuracy
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"Generation Error: {e}")
        return None

# --- PAGE CONFIG & CSS ---
st.set_page_config(page_title="AI Study Guide", layout="wide", page_icon="🎓")
st.markdown("""
<style>
    .metric-card { background: white; padding: 20px; border-radius: 10px; border: 1px solid #ddd; text-align: center; }
    .stButton>button { width: 100%; border-radius: 5px; }
    .correct-box { background-color: #d4edda; color: #155724; padding: 15px; border-radius: 8px; border-left: 5px solid #28a745; margin: 10px 0; }
    .wrong-box { background-color: #f8d7da; color: #721c24; padding: 15px; border-radius: 8px; border-left: 5px solid #dc3545; margin: 10px 0; }
</style>
""", unsafe_allow_html=True)

# --- SESSION STATE ---
for key in ['current_project', 'groq_api_key', 'quiz_data', 'quiz_submitted', 'user_answers', 'quiz_type', 'focus_quiz_active']:
    if key not in st.session_state: st.session_state[key] = None

# --- SIDEBAR NAV ---
with st.sidebar:
    st.title("🎓 AI Study Companion")
    api_key = st.text_input("Groq API Key", type="password", value=st.session_state.groq_api_key or "")
    if api_key: st.session_state.groq_api_key = api_key
    
    st.divider()
    projects = db.load_all_projects()
    selected_proj = st.selectbox("📂 Switch Project", ["None"] + projects, index=0 if not st.session_state.current_project else projects.index(st.session_state.current_project)+1)
    if selected_proj != "None": st.session_state.current_project = selected_proj
    
    if st.button("➕ Create New Unit"): st.session_state.current_project = None; st.rerun()
    
    if st.session_state.current_project:
        st.divider()
        st.subheader("🎯 Journey Phase")
        app_mode = st.radio("Move to:", ["📈 Dashboard", "📖 Study Materials", "🎯 Practice Drills", "📊 Mastery Tracker"])
    else: app_mode = "Create"

client = get_client(st.session_state.groq_api_key)

# --- MAIN LOGIC ---
if app_mode == "Create":
    st.title("🚀 Start a New Learning Journey")
    uploaded_file = st.file_uploader("Upload Unit PDF", type="pdf")
    if uploaded_file and client:
        col1, col2 = st.columns(2)
        proj_name = col1.text_input("Unit Name", value=uploaded_file.name.split('.')[0])
        level = col2.select_slider("Depth", ["Basic", "Intermediate", "Advanced"], value="Intermediate")
        if st.button("✨ Initialize Unit"):
            with st.spinner("Processing..."):
                raw = extract_pdf_text(uploaded_file)
                notes = generate_content(f"Extract and summarize the key information from these notes in an organized markdown format. Detail Level: {level}. NOTES: {raw[:15000]}", client)
                db.save_project(proj_name, level, notes, raw)
                st.session_state.current_project = proj_name
                st.rerun()

elif st.session_state.current_project:
    data = db.get_project_details(st.session_state.current_project)
    
    if app_mode == "📈 Dashboard":
        st.title(f"🚀 Unit: {data['name']}")
        tracker = json.loads(json.loads(data['practice_data']).get('progress_tracker', '{}'))
        weak = determine_weak_topics(data)
        mastery = int((1 - (len(weak)/len(tracker) if tracker else 0)) * 100)
        
        m1, m2, m3 = st.columns(3)
        m1.metric("Topics Identified", len(tracker))
        m2.metric("Weak Spots 🚨", len(weak), delta_color="inverse")
        m3.metric("Overall Mastery", f"{mastery}%")
        
        st.divider()
        c1, c2 = st.columns(2)
        with c1: st.info("### 📖 Knowledge Base\nReview your synthesized notes and analogies.")
        with c2: 
            if weak: st.error(f"### 🎯 Targeted Focus\nYou have {len(weak)} weak topics. Start a Focus Quiz now.")
            else: st.success("### ✅ Ready for Practice\nAll concepts look strong. Try a general drill!")

    elif app_mode == "📖 Study Materials":
        st.title("📖 Knowledge Base")
        t1, t2 = st.tabs(["📝 Study Notes", "💡 Analogies"])
        with t1: st.markdown(data['notes'])
        with t2:
            if st.button("🔄 Generate Analogies"):
                ana = generate_content(f"Create 5 real-world analogies based STRICTLY on the content of these study notes: {data['notes'][:5000]}", client)
                db.update_project_field(data['name'], 'analogy_data', 'current', ana)
                st.rerun()
            st.markdown(json.loads(data['analogy_data']).get('current', 'No analogies yet.'))

    elif app_mode == "🎯 Practice Drills":
        st.title("🎯 Interactive Practice")
        weak = determine_weak_topics(data)
        
        col_gen, col_foc = st.columns(2)
        if col_gen.button("🎲 Generate General Quiz"):
            prompt = f"""
            Generate 10 MCQ/TF JSON questions based STRICTLY and ONLY on the following study notes. 
            Do not include general knowledge.
            NOTES: {data['notes'][:10000]}
            FORMAT: {{'questions': [{{'id':1,'question_text':'','options':['A','B','C','D'],'correct_answer':'A','primary_concept':'Topic Title','detailed_explanation':''}}]}}
            """
            st.session_state.quiz_data = generate_content(prompt, client, is_json=True)
            st.session_state.quiz_submitted = False; st.session_state.user_answers = {}; st.rerun()
            
        if weak and col_foc.button(f"🎯 Generate Focus Quiz ({len(weak)})"):
            prompt = f"""
            Generate 10 MCQ/TF JSON questions strictly focusing ONLY on these specific topics: {', '.join(weak)}.
            Use this context for question details: {data['notes'][:5000]}
            FORMAT: {{'questions': [...]}}
            """
            st.session_state.quiz_data = generate_content(prompt, client, is_json=True)
            st.session_state.quiz_submitted = False; st.session_state.user_answers = {}; st.rerun()

        if st.session_state.quiz_data:
            q_json = safe_json_parse(st.session_state.quiz_data)
            if q_json and not st.session_state.quiz_submitted:
                with st.form("quiz_form"):
                    ans_map = {}
                    for i, q in enumerate(q_json['questions']):
                        st.markdown(f"**Q{i+1}:** {q['question_text']}")
                        ans_map[q['id']] = st.radio("Choose:", q['options'], index=None, key=f"q_{q['id']}")
                    
                    if st.form_submit_button("Submit Quiz"):
                        st.session_state.user_answers = ans_map
                        st.session_state.quiz_submitted = True
                        scores = {q['primary_concept']: (1 if ans_map[q['id']] == q['correct_answer'] else 0, 1) for q in q_json['questions']}
                        db.update_progress_tracker(data['name'], scores)
                        st.rerun()
            
            elif st.session_state.quiz_submitted:
                st.header("🏁 Quiz Results")
                score = 0
                for i, q in enumerate(q_json['questions']):
                    user_ans = st.session_state.user_answers.get(q['id'])
                    is_correct = user_ans == q['correct_answer']
                    if is_correct: score += 1
                    
                    with st.container():
                        if is_correct:
                            st.markdown(f"""<div class="correct-box">
                                <b>Q{i+1}: Correct!</b><br>{q['question_text']}<br>
                                <i>Your Answer: {user_ans}</i>
                            </div>""", unsafe_allow_html=True)
                        else:
                            st.markdown(f"""<div class="wrong-box">
                                <b>Q{i+1}: Incorrect</b><br>{q['question_text']}<br>
                                <b>Correct Answer:</b> {q['correct_answer']}<br>
                                <b>Explanation:</b> {q['detailed_explanation']}
                            </div>""", unsafe_allow_html=True)
                st.success(f"### Final Score: {score}/{len(q_json['questions'])}")
                if st.button("🔄 Take New Quiz"):
                    st.session_state.quiz_data = None
                    st.session_state.quiz_submitted = False
                    st.rerun()

    elif app_mode == "📊 Mastery Tracker":
        st.title("📊 Concept Mastery")
        tracker = json.loads(json.loads(data['practice_data']).get('progress_tracker', '{}'))
        if tracker:
            rows = [{"Concept": c, "Accuracy": f"{(s['correct']/s['total'])*100:.1f}%", "Status": "🟢" if (s['correct']/s['total']) >= 0.8 else "🔴"} for c, s in tracker.items()]
            st.table(rows)
            if st.button("🗑️ Clear Data"): db.reset_progress_tracker(data['name']); st.rerun()

else: st.warning("Please configure your Groq API Key and select or create a project to begin.")
