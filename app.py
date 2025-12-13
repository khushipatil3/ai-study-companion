import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

# --- CUSTOM CSS (Clean up the UI) ---
# This hides the default "Manage App" menu to make it look cleaner
st.markdown("""
<style>
    .reportview-container {
        margin-top: -2em;
    }
    #MainMenu {visibility: hidden;}
    .stDeployButton {display:none;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

# --- HEADER & SETTINGS ---
st.title("🎓 AI Study Companion")
st.markdown("### Upload your material, choose your depth, and start learning.")

# We hide the API key in an expander to keep the UI clean
with st.expander("⚙️ System Settings (API Key)", expanded=False):
    api_key = st.text_input("Groq API Key:", type="password", help="Enter your key from console.groq.com")
    st.info("ℹ️ Vision Mode is active by default. The AI will scan diagrams and charts.")

# --- LOGIC FUNCTIONS ---

def encode_image(pix):
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content_with_vision(uploaded_file, client):
    """
    Scans pages as images (Vision Mode) to capture all details.
    """
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    
    # Create a container for progress updates
    progress_container = st.empty()
    bar = st.progress(0)
    
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        # Update progress
        bar.progress((i + 1) / total_pages)
        progress_container.caption(f"👁️ Scanning Page {i+1} of {total_pages}...")

        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
            img_str = encode_image(pix)
            
            chat_completion = client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this page into Markdown. Describe any diagrams or flowcharts in detail."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_str}",
                                },
                            },
                        ],
                    }
                ],
                model="meta-llama/llama-4-scout-17b-16e-instruct",
            )
            
            page_text = chat_completion.choices[0].message.content
            full_content += f"\n--- PAGE_BREAK ---\n{page_text}\n"
            
        except Exception as e:
            full_content += f"\n--- PAGE_BREAK ---\n(Error reading page {i+1})\n"

    # Cleanup progress bars
    progress_container.empty()
    bar.empty()
    return full_content

def get_system_prompt(level):
    """
    Returns specific instructions based on the user's selected difficulty.
    """
    if level == "Basic (Quick Summary)":
        return """
        Act as a Tutor creating a 'Crash Course' guide.
        GOAL: The student just wants to pass the exam.
        1. **Keep it simple:** Focus only on high-level definitions and core concepts.
        2. **Brevity:** Use bullet points and short paragraphs.
        3. **Outlines:** Create clear outlines of the chapters.
        4. **Visuals:** Add 

[Image of X]
 tags only for the most critical diagrams.
        """
    elif level == "Intermediate (Detailed Notes)":
        return """
        Act as an expert Professor. Create a comprehensive study guide.
        GOAL: The student wants a solid B+ or A grade.
        1. **Format:** Use clear Markdown headers (## Topic).
        2. **Structure:** Explain concepts clearly with definitions and process steps.
        3. **Exam Tips:** Include specific "Exam Strategy" boxes.
        4. **Visuals:** Insert 

[Image of X]
 tags for every relevant concept or flowchart.
        """
    else:  # Advanced
        return """
        Act as a Subject Matter Expert and Researcher.
        GOAL: The student wants to master the subject (Top 1%).
        1. **Depth:** Dive deep into every nuance. Explain 'Why' and 'How', not just 'What'.
        2. **Context:** Add information *outside* the text if it helps understanding (e.g., real-world applications).
        3. **Connections:** Link concepts together to show the bigger picture.
        4. **Visuals:** Insert  tags frequently.
        """

def generate_study_notes(raw_text, level, client):
    if not raw_text: return "⚠️ Error: No content extracted."

    pages = raw_text.split("--- PAGE_BREAK ---")
    batch_size = 15 
    batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
    
    final_notes = f"# 📘 {level} Study Guide\n\n"
    
    status_text = st.empty()
    bar = st.progress(0)
    
    # Get the specific instructions for the chosen level
    system_instructions = get_system_prompt(level)
    
    for i, batch in enumerate(batches):
        bar.progress((i + 1) / len(batches))
        status_text.caption(f"🧠 Synthesizing Batch {i+1}/{len(batches)}...")
        
        batch_content = "\n".join(batch)
        
        prompt = f"""
        {system_instructions}
        
        CONTENT TO PROCESS:
        {batch_content}
        
        Output strictly Markdown.
        """
        
        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            batch_summary = completion.choices[0].message.content
            final_notes += batch_summary + "\n\n---\n\n"
        except Exception as e:
            final_notes += f"\n\n(Error processing batch {i+1}: {e})\n\n"
            
    status_text.empty()
    bar.empty()
    return final_notes

# --- MAIN UI FLOW ---

# 1. STOP if no API Key
if not api_key:
    st.warning("Please enter your API Key in the 'Settings' above to proceed.")
    st.stop()

client = Groq(api_key=api_key)

# 2. File Upload Area
st.divider()
uploaded_file = st.file_uploader("📂 Step 1: Upload your Document", type="pdf")

if uploaded_file:
    # 3. Detail Selection (Only shows after upload)
    st.write("### 🎚️ Step 2: Choose your Detail Level")
    
    level = st.radio(
        "How deep should we go?",
        ["Basic (Quick Summary)", "Intermediate (Detailed Notes)", "Advanced (Mastery & Context)"],
        index=1, # Default to Intermediate
        horizontal=True
    )
    
    # 4. Generate Button
    if st.button("🚀 Generate Notes"):
        
        # A. Extract
        content = extract_content_with_vision(uploaded_file, client)
        
        if len(content) < 50:
            st.error("⚠️ No content found.")
        else:
            # B. Generate based on Level
            notes = generate_study_notes(content, level, client)
            
            # C. Display
            st.success("✨ Generation Complete!")
            st.markdown(notes)
            st.download_button("Download Notes (.md)", notes, file_name=f"{level.split()[0]}_Notes.md")
            
            # Placeholder for future features (Sidebar will populate here later)
            with st.sidebar:
                st.header("Tools")
                st.info("Quiz & Flashcards coming in next update!")
