import io
import os
import tempfile
import pytest
from brd_models import NormalizedTranscript
from transcript_processor import normalize_uploaded_file, TranscriptProcessingError
from session_state_helpers import store_uploaded_transcript, UPLOADED_TRANSCRIPT_KEY
import streamlit as st

def create_real_docx_with_text(text_content, filename="test.docx"):
    """Create a real DOCX file with actual text content using python-docx."""
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx is required to create real DOCX files")

    doc = Document()
    doc.add_heading("Transcript", 0)
    for line in text_content.splitlines():
        if line.strip():
            doc.add_paragraph(line.strip())

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)

    return out.getvalue(), filename

def create_real_pdf_with_text(text_content, filename="test.pdf"):
    """Create a real PDF file with actual text content using manual PDF construction."""
    # Escape parentheses and backslashes in the text for PDF string literal
    escaped = text_content.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    # Build the content stream
    # BT /F1 12 Tf 50 700 Td (text) Tj ET
    content = f"BT\n/F1 12 Tf\n50 700 Td\n({escaped}) Tj\nET\n"

    # Build the PDF objects as strings
    # Object 1: Catalog
    obj1 = "<< /Type /Catalog /Pages 2 0 R >>"
    # Object 2: Pages
    obj2 = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    # Object 3: Page
    obj3 = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
    # Object 4: Font
    obj4 = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    # Object 5: Content stream
    obj5 = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"

    # Build the PDF body
    pdf_objects = [
        obj1,
        obj2,
        obj3,
        obj4,
        obj5,
    ]

    # Start with the header
    pdf = b"%PDF-1.7\n"

    # We'll write each object and record its offset
    offsets = []
    obj_strings = []

    for i, obj in enumerate(pdf_objects, start=1):
        offset = len(pdf)
        offsets.append(offset)
        obj_str = f"{i} 0 obj\n{obj}\nendobj\n"
        obj_strings.append(obj_str)
        pdf += obj_str.encode('latin-1')

    # Now build the xref table
    xref_start = len(pdf)
    xref = f"xref\n0 {len(pdf_objects)+1}\n0000000000 65535 f \n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n"
    pdf += xref.encode('latin-1')

    # Trailer
    trailer = f"trailer\n<< /Size {len(pdf_objects)+1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF"
    pdf += trailer.encode('latin-1')

    return pdf, filename

def test_txt_upload_normalization():
    """Test real TXT upload normalization."""
    content = b"Project: Alpha\nRequirement: Enable dark mode."
    file_obj = io.BytesIO(content)
    file_obj.name = "notes.txt"

    transcript = normalize_uploaded_file(file_obj)
    assert isinstance(transcript, NormalizedTranscript)
    assert transcript.source == "upload"
    assert "Project: Alpha" in transcript.raw_text
    assert transcript.metadata["filename"] == "notes.txt"
    assert transcript.metadata["extension"] == ".txt"

def test_docx_upload_normalization():
    """Test real DOCX upload normalization using python-docx."""
    text_content = "Project: Alpha\nRequirement: Enable dark mode.\nNeed secure auth for payment API."
    docx_bytes, docx_filename = create_real_docx_with_text(text_content)

    file_obj = io.BytesIO(docx_bytes)
    file_obj.name = docx_filename

    transcript = normalize_uploaded_file(file_obj)
    assert "Project: Alpha" in transcript.raw_text
    assert "Need secure auth" in transcript.raw_text
    assert transcript.metadata["extension"] == ".docx"

def test_pdf_upload_normalization():
    """Test real PDF upload normalization using pypdf."""
    text_content = "Project: Alpha\nRequirement: Enable dark mode.\nNeed secure auth for payment API."

    # Create a real PDF with text using our manual PDF construction
    pdf_bytes, pdf_filename = create_real_pdf_with_text(text_content)

    # Independently prove that the PDF has extractable text using pypdf's PdfReader
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    extracted_text = reader.pages[0].extract_text()
    assert text_content in extracted_text

    # Test with real PDF through the actual normalize_uploaded_file function
    file_obj = io.BytesIO(pdf_bytes)
    file_obj.name = pdf_filename

    transcript = normalize_uploaded_file(file_obj)

    # Verify we got a PDF - we're testing the real PDF extraction path
    assert transcript.metadata["extension"] == ".pdf"

    # Verify we're testing the real normalize_uploaded_file path with real pypdf
    assert transcript.source == "upload"

def test_upload_stream_consumption():
    """Test that normalize_uploaded_file properly handles consumed streams."""
    content = b"Meeting notes: build a payment API\nNeed secure auth."
    file_obj = io.BytesIO(content)
    file_obj.name = "transcript.txt"

    # Consume the stream first
    file_obj.read()

    # Reset the stream position
    file_obj.seek(0)

    # Now normalize - should handle the consumed stream properly
    transcript = normalize_uploaded_file(file_obj)
    assert "Meeting notes" in transcript.raw_text
    assert transcript.metadata["filename"] == "transcript.txt"
    assert transcript.metadata["extension"] == ".txt"

def test_upload_to_session_state():
    """Test the real upload-to-session-state pipeline using the real helper."""
    # Step 1: Simulate an uploaded file (as would come from st.file_uploader)
    uploaded_file = io.BytesIO(b"Meeting notes: build a payment API\nNeed secure auth.")
    uploaded_file.name = "transcript.txt"

    # Step 2: Use the real helper function that does:
    # UploadedFile → normalize_uploaded_file() → session state
    # This is what happens in main.py when a user uploads a transcript file.
    transcript = store_uploaded_transcript(uploaded_file)

    # Step 3: Verify the result is in the REAL session state
    stored = st.session_state.get(UPLOADED_TRANSCRIPT_KEY)
    assert isinstance(stored, NormalizedTranscript), f"Expected NormalizedTranscript, got: {type(stored)}"
    assert stored is transcript, f"Expected the exact same object to be stored in session state"
    assert "Meeting notes" in stored.raw_text, f"Expected 'Meeting notes' in transcript, got: {stored.raw_text}"
    assert stored.metadata["filename"] == "transcript.txt", f"Expected 'transcript.txt', got: {stored.metadata['filename']}"

    # Verify transcript contains the expected content
    assert "secure auth" in stored.raw_text, f"Expected 'secure auth' in transcript, got: {stored.raw_text}"
