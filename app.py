import streamlit as st
import fitz  # PyMuPDF
from groq import Groq
import base64

# --- PAGE CONFIG ---
st.set_page_config(page_title="AI Study Companion", page_icon="🎓", layout="wide")

st.title("🎓 AI Study Companion")
st.markdown("### Turn any lecture PDF into adaptive study notes.")

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Settings")
    api_key = st.text_input("Enter Groq API Key:", type="password")
    st.info("Get a free key at console.groq.com")
    
    st.divider()
    st.success("👁️ Vision Mode is ACTIVE")
    st.caption("Every page is scanned as an image to capture diagrams, charts, and screenshots.")

# --- LOGIC FUNCTIONS ---

def encode_image(pix):
    """Converts a PyMuPDF Pixmap into a base64 string for the API"""
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content_with_vision(uploaded_file, client):
    """
    Scans every page as an image using Llama 4 Scout (Vision).
    This ensures no diagrams or screenshots are missed.
    """
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    
    status_text = st.empty()
    progress_bar = st.progress(0)
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        # Update UI Progress
        progress = (i + 1) / total_pages
        progress_bar.progress(progress)
        status_text.text(f"👁️ Scanning Page {i+1}/{total_pages}...")

        try:
            # 1. Convert PDF Page to High-Res Image
            # Matrix(2, 2) = 2x Zoom for better clarity on small text/charts
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
            img_str = encode_image(pix)
            
            # 2. Send Image to Llama 4 Scout (The new Vision Model)
            chat_completion = client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this page into Markdown. If there are diagrams, charts, or screenshots, describe them in detail. Capture all text exactly."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_str}",
                                },
                            },
                        ],
                    }
                ],
                # ✅ NEWEST MODEL ID (Replaces decommissioned 3.2)
                model="meta-llama/llama-4-scout-17b-16e-instruct",
            )
            
            page_text = chat_completion.choices[0].message.content
            full_content += f"\n\n--- Page {i+1} ---\n{page_text}\n"
            
        except Exception as e:
            st.error(f"❌ Error scanning page {i+1}: {e}")
            # Fallback to plain text if Vision fails for some reason
            full_content += f"\n\n--- Page {i+1} (Text Fallback) ---\n{page.get_text()}\n"

    status_text.success("✅ Scanning Complete!")
    progress_bar.empty()
    return full_content

def generate_study_notes(raw_text, client):
    """
    Summarizes the raw transcription into structured notes.
    """
    if not raw_text: return "⚠️ Error: No content extracted."

    prompt = f"""
    Act as an expert Professor. Create a structured study guide based on the transcribed lecture notes below.
    
    CONTENT: {raw_text[:30000]} 
    
    INSTRUCTIONS:
    1. **Format:** Use clear Markdown headers (## Topic Name).
    2. **Structure:** Adapt to the content (Definitions, Process Steps, Pros/Cons).
    3. **Visuals:** If the transcription mentions a diagram (e.g., "The image shows a flowchart..."), insert a placeholder tag like .
    4. **Exam Tips:** Include specific "Exam Strategy" boxes.
    
    Output strictly Markdown.
    """
    
    try:
        completion = client.chat.completions.create(
            # Using Llama 3.3 for the smart summarization part
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"❌ AI Error: {e}"

def generate_quiz(raw_text, client):
    """
    Generates a quiz based on the content.
    """
    prompt = f"""
    Create 5 Multiple Choice Questions (MCQs) based on this text.
    Format the output so the answer is hidden or at the bottom.
    TEXT: {raw_text[:10000]}
    """
    try:
        completion = client.chat.completions.create(
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
        
        # 1. Extract (Vision Mode Always On)
        content = extract_content_with_vision(uploaded_file, client)
        
        if len(content) < 50:
            st.error("⚠️ No content found. The PDF might be blank.")
        else:
            # 2. Create Tabs for Output
            tab1, tab2, tab3 = st.tabs(["📘 Study Notes", "📝 Practice Quiz", "🔍 Raw Transcription"])
            
            # 3. Generate Final Output
            with st.spinner("🧠 Synthesizing notes from vision data..."):
                notes = generate_study_notes(content, client)
                quiz = generate_quiz(content, client)
            
            with tab1:
                st.markdown(notes)
                st.download_button("Download Notes (.md)", notes, file_name="Vision_Notes.md")
                
            with tab2:
                st.subheader("Test Your Knowledge")
                st.markdown(quiz)
                
            with tab3:
                st.info("This is what the AI 'saw' on your pages:")
                st.text_area("Raw Vision Output", content, height=400)
