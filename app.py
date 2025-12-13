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
    
    st.divider()
    # TOGGLE FOR VISION MODE
    use_vision = st.checkbox("👁️ Enable Vision Mode", help="Use this if your PDF has screenshots or scanned text. (Slower but sees everything)")
    
    st.info("Using Llama-3.3 (Text) or Llama-3.2-Vision (Images)")

# --- LOGIC ---

def encode_image(pix):
    """Converts a PyMuPDF Pixmap into a base64 string for the API"""
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content(uploaded_file, use_vision_mode, client):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    
    status_text = st.empty()
    progress_bar = st.progress(0)
    
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        progress = (i + 1) / total_pages
        progress_bar.progress(progress)
        
        if use_vision_mode:
            status_text.text(f"👁️ Scanning Page {i+1}/{total_pages} (Vision Mode)...")
            
            # 1. Convert PDF Page to Image
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)) # Zoom in for better quality
            img_str = encode_image(pix)
            
            # 2. Send Image to Llama-3.2-Vision
            # We ask the AI to transcribe everything it sees
            try:
                chat_completion = client.chat.completions.create(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Transcribe ALL text, code, and diagrams visible on this page into Markdown."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{img_str}",
                                    },
                                },
                            ],
                        }
                    ],
                    model="llama-3.2-11b-vision-preview",
                )
                page_text = chat_completion.choices[0].message.content
                full_content += f"\n--- Page {i+1} ---\n{page_text}\n"
            except Exception as e:
                st.error(f"Error scanning page {i+1}: {e}")
                
        else:
            # FAST TEXT MODE (Original)
            status_text.text(f"📖 Reading Page {i+1}/{total_pages} (Text Mode)...")
            text = page.get_text().replace("\x00", "") # Clean null bytes
            full_content += text
            
    status_text.empty()
    progress_bar.empty()
    return full_content

def generate_study_notes(raw_text, client):
    if not raw_text: return "⚠️ Error: No content extracted."

    prompt = f"""
    Act as an expert Professor. Create a structured study guide.
    
    CONTENT: {raw_text[:25000]} 
    
    INSTRUCTIONS:
    1. **Format:** Use clear Markdown headers (## Topic Name).
    2. **Structure:** Adapt to the content (Definitions, Process Steps, Pros/Cons).
    3. **Exam Tips:** Include "Exam Strategy" boxes.
    4. **Images:** If the text describes a diagram, add a placeholder [Image of...].
    
    Output strictly Markdown.
    """
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"❌ AI Error: {e}"

# --- MAIN UI ---

if not api_key:
    st.warning("⚠️ Enter Groq API Key to start.")
    st.stop()

client = Groq(api_key=api_key)

uploaded_file = st.file_uploader("📂 Upload PDF", type="pdf")

if uploaded_file:
    if st.button("🚀 Generate Notes"):
        # 1. Extract (Text or Vision)
        content = extract_content(uploaded_file, use_vision, client)
        
        if len(content) < 50:
            st.error("⚠️ No text found! Try checking 'Enable Vision Mode' in the sidebar.")
        else:
            st.success(f"✅ Extracted {len(content)} characters.")
            
            # 2. Generate Notes
            with st.spinner("🧠 Analyzing content..."):
                notes = generate_study_notes(content, client)
                st.markdown(notes)
                st.download_button("Download Notes", notes, file_name="Vision_Notes.md")
