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
    st.caption("Scans every page as an image to capture text, diagrams, and charts.")

# --- LOGIC FUNCTIONS ---

def encode_image(pix):
    """Converts a PyMuPDF Pixmap into a base64 string for the API"""
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def extract_content_with_vision(uploaded_file, client):
    """
    Scans every page as an image using Llama 4 Scout.
    """
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    full_content = ""
    
    status_text = st.empty()
    progress_bar = st.progress(0)
    total_pages = len(doc)
    
    for i, page in enumerate(doc):
        progress = (i + 1) / total_pages
        progress_bar.progress(progress)
        status_text.text(f"👁️ Scanning Page {i+1}/{total_pages}...")

        try:
            # 2x Zoom for better clarity on diagrams
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
            img_str = encode_image(pix)
            
            # Send to Llama 4 Scout
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
            # Add a clear delimiter for splitting later
            full_content += f"\n--- PAGE_BREAK ---\n{page_text}\n"
            
        except Exception as e:
            st.error(f"❌ Error scanning page {i+1}: {e}")
            full_content += f"\n--- PAGE_BREAK ---\n(Error reading page {i+1})\n"

    status_text.success("✅ Scanning Complete!")
    progress_bar.empty()
    return full_content

def generate_study_notes(raw_text, client):
    """
    Split text into batches to avoid Token Limits, then combine results.
    """
    if not raw_text: return "⚠️ Error: No content extracted."

    # 1. SPLIT CONTENT BY PAGES
    # We use the delimiter we added during extraction
    pages = raw_text.split("--- PAGE_BREAK ---")
    
    # 2. CREATE BATCHES (e.g., 15 pages per batch)
    # This ensures the AI doesn't get overwhelmed
    batch_size = 15 
    batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
    
    final_notes = "# 📘 Comprehensive Study Guide\n\n"
    
    progress_text = st.empty()
    bar = st.progress(0)
    
    for i, batch in enumerate(batches):
        progress = (i + 1) / len(batches)
        bar.progress(progress)
        progress_text.text(f"🧠 Synthesizing Batch {i+1}/{len(batches)}...")
        
        batch_content = "\n".join(batch)
        
        prompt = f"""
        Act as an expert Professor. Create detailed study notes for this section of the course.
        
        CONTENT TO PROCESS:
        {batch_content}
        
        INSTRUCTIONS:
        1. **Format:** Use clear Markdown headers (## Topic).
        2. **Visuals:** If the text mentions a complex concept (like 'Mitosis', 'Architecture', 'Flowchart'), insert a tag like
 immediately after the concept. 
           - Example: "Data cleaning involves removing noise. 

[Image of Data Cleaning Process Flowchart]
"
        3. **Detail:** Do not skip sections. Explain every concept found in the content.
        4. **Tables:** If there is tabular data, format it as a Markdown table.
        
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
            
    progress_text.empty()
    bar.empty()
    return final_notes

def generate_quiz(raw_text, client):
    """
    Generates a quiz based on a random sample of the text to avoid limits.
    """
    # Take the first 15000 chars roughly (Introduction + Unit 1)
    # Sending 113 pages for a quiz is too much context
    sample_text = raw_text[:20000] 
    
    prompt = f"""
    Create 10 Multiple Choice Questions (MCQs) based on this text.
    Format the output so the answer is hidden or at the bottom.
    TEXT: {sample_text}
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
        
        # 1. Extract (Vision Mode)
        content = extract_content_with_vision(uploaded_file, client)
        
        if len(content) < 50:
            st.error("⚠️ No content found. The PDF might be blank.")
        else:
            # 2. Create Tabs for Output
            tab1, tab2, tab3 = st.tabs(["📘 Study Notes", "📝 Practice Quiz", "🔍 Raw Transcription"])
            
            # 3. Generate Final Output
            # Note: We run these sequentially now to handle the progress bars correctly
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
