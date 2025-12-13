import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import os

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

st.title("🎓 AI Study Companion")
st.markdown("### Turn any lecture PDF into adaptive study notes (powered by Llama-3.3).")

# --- SIDEBAR: API KEY ---
with st.sidebar:
    st.header("⚙️ Settings")
    api_key = st.text_input("Enter Groq API Key:", type="password")
    st.info("Get a free key at console.groq.com")
    
    st.divider()
    st.markdown("**Features:**")
    st.markdown("- 🧠 **Model:** Llama-3.3 70B (Newer & Smarter)")
    st.markdown("- 📝 **Adaptive Notes**")
    st.markdown("- 🎯 **Quiz Generation**")

# --- LOGIC FUNCTIONS ---

def extract_text_from_pdf(uploaded_file):
    """
    Extracts text and cleans it to prevent API crashes.
    """
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    
    # 🧼 CLEANING STEP (Crucial for preventing crashes)
    # Remove null bytes and strange control characters that break APIs
    text = text.replace("\x00", "").strip()
    return text

def generate_study_notes(text_chunk, client):
    """
    Generates notes using Llama-3.3. Includes Error Handling.
    """
    if not text_chunk:
        return "⚠️ Error: No text found in PDF. It might be an image-only scan."

    prompt = f"""
    Act as an expert Professor. Create a structured study guide for the following text.
    
    TEXT: {text_chunk[:15000]} 
    
    INSTRUCTIONS:
    1. **Format:** Use clear Markdown headers (## Topic Name).
    2. **Structure:** Adapt to the content (Definitions, Process Steps, Comparisons).
    3. **Exam Tips:** Include specific "Exam Strategy" boxes for tricky concepts.
    4. **Tone:** Academic but accessible.
    
    Output strictly Markdown.
    """
    
    try:
        completion = client.chat.completions.create(
            # ✅ UPDATED MODEL ID HERE
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_completion_tokens=4000, # Updated parameter name for new API
        )
        return completion.choices[0].message.content
        
    except Exception as e:
        return f"❌ API Error: {str(e)}"

def generate_quiz(text_chunk, client):
    prompt = f"""
    Create 5 Multiple Choice Questions (MCQs) based on this text.
    Format the output so the answer is hidden or at the bottom.
    TEXT: {text_chunk[:5000]}
    """
    try:
        completion = client.chat.completions.create(
            # ✅ UPDATED MODEL ID HERE
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, 
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"❌ Quiz Error: {str(e)}"

# --- MAIN UI ---

if not api_key:
    st.warning("⚠️ Please enter your Groq API Key in the sidebar to start.")
    st.stop()

# Initialize Client
try:
    client = Groq(api_key=api_key)
except Exception as e:
    st.error(f"Invalid API Key format: {e}")
    st.stop()

uploaded_file = st.file_uploader("📂 Upload your Lecture PDF", type="pdf")

if uploaded_file:
    if st.button("🚀 Generate Study Guide"):
        with st.spinner("Analyzing document... (Llama-3.3 is fast!)"):
            # 1. Extract Text
            try:
                raw_text = extract_text_from_pdf(uploaded_file)
                st.success(f"✅ Read {len(raw_text)} characters.")
            except Exception as e:
                st.error(f"Error reading PDF: {e}")
                st.stop()
            
            # 2. Create Tabs for Output
            tab1, tab2 = st.tabs(["📘 Study Notes", "📝 Practice Quiz"])
            
            # 3. Generate Content
            notes = generate_study_notes(raw_text, client)
            quiz = generate_quiz(raw_text, client)
            
            with tab1:
                st.markdown(notes)
                # Helper to download
                st.download_button("Download Notes (.md)", notes, file_name="Study_Notes.md")
                
            with tab2:
                st.subheader("Test Your Knowledge")
                st.markdown(quiz)
