import io
import os
from typing import Optional, Union, BinaryIO
from brd_models import NormalizedTranscript


class TranscriptProcessingError(Exception):
    """Raised when transcript processing fails."""
    pass


def normalize_manual_notes(
    text: str,
    title: Optional[str] = None,
    meeting_date: Optional[str] = None,
    participants: Optional[list[str]] = None,
) -> NormalizedTranscript:
    """
    Normalizes manually pasted meeting notes.
    Validates that meaningful transcript text exists.
    """
    if not text or not text.strip():
        raise TranscriptProcessingError("Meeting notes cannot be empty.")

    cleaned_text = text.strip()
    return NormalizedTranscript(
        raw_text=cleaned_text,
        source="manual",
        meeting_title=title.strip() if title and title.strip() else None,
        meeting_date=meeting_date.strip() if meeting_date and meeting_date.strip() else None,
        participants=participants or [],
        metadata={"char_count": len(cleaned_text), "line_count": len(cleaned_text.splitlines())},
    )


def _extract_text_from_pdf(file_bytes: bytes, filename: str) -> str:
    """Extracts text from a PDF file using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise TranscriptProcessingError(
            f"Could not process '{filename}': PDF support requires the 'pypdf' package. "
            "Please install it with 'pip install pypdf'."
        )

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text_parts = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text_parts.append(extracted)

        full_text = "\n".join(text_parts).strip()
        if not full_text:
            raise TranscriptProcessingError(f"The PDF file '{filename}' contained no extractable text.")
        return full_text
    except Exception as e:
        raise TranscriptProcessingError(f"Failed to extract text from PDF '{filename}': {e}")


def _extract_text_from_docx(file_bytes: bytes, filename: str) -> str:
    """Extracts text from a DOCX file using python-docx."""
    try:
        from docx import Document
    except ImportError:
        raise TranscriptProcessingError(
            f"Could not process '{filename}': DOCX support requires the 'python-docx' package. "
            "Please install it with 'pip install python-docx'."
        )

    try:
        doc = Document(io.BytesIO(file_bytes))
        text_parts = [p.text for p in doc.paragraphs if p.text.strip()]

        full_text = "\n".join(text_parts).strip()
        if not full_text:
            raise TranscriptProcessingError(f"The DOCX file '{filename}' contained no extractable text.")
        return full_text
    except Exception as e:
        raise TranscriptProcessingError(f"Failed to extract text from DOCX '{filename}': {e}")


def extract_text_from_file_bytes(file_bytes: bytes, filename: str = "uploaded_file.txt") -> str:
    """
    Safely decodes raw file bytes into a clean string.
    Attempts UTF-8 first, then falls back to Latin-1.
    """
    if not file_bytes:
        raise TranscriptProcessingError(f"The file '{filename}' is empty.")

    # Try UTF-8 first (standard for modern text files)
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback to Latin-1 which can decode any byte sequence
        try:
            text = file_bytes.decode("latin-1")
        except Exception as e:
            raise TranscriptProcessingError(f"Could not decode file '{filename}': {e}")

    cleaned = text.strip()
    if not cleaned:
        raise TranscriptProcessingError(f"The file '{filename}' contains only whitespace.")

    return cleaned


def normalize_uploaded_file(
    file_obj: Union[BinaryIO, bytes, io.BytesIO, any],
    filename: Optional[str] = None,
) -> NormalizedTranscript:
    """
    Safely reads, decodes, and normalizes an uploaded text file.
    Supports Streamlit UploadedFile objects, file-like objects, or raw bytes.
    """
    if file_obj is None:
        raise TranscriptProcessingError("No file was provided.")

    # Determine filename
    actual_filename = filename
    if not actual_filename and hasattr(file_obj, "name"):
        actual_filename = file_obj.name
    if not actual_filename:
        actual_filename = "uploaded_transcript.txt"

    # Validate file extension
    _, ext = os.path.splitext(actual_filename)
    ext = ext.lower()
    if ext not in [".txt", ".pdf", ".docx"]:
        raise TranscriptProcessingError(
            f"Unsupported file format '{ext}'. Supported formats: .txt, .pdf, .docx."
        )

    # Read bytes - robust handling of previously-consumed streams
    if isinstance(file_obj, bytes):
        raw_bytes = file_obj
    elif hasattr(file_obj, "read"):
        # For objects with a read() method (like Streamlit UploadedFile)
        # Store the current position if seek is available
        current_pos = None
        if hasattr(file_obj, "tell"):
            try:
                current_pos = file_obj.tell()
            except (OSError, io.UnsupportedOperation):
                pass

        # Read the bytes
        raw_bytes = file_obj.read()

        # If seek is available, reset to the beginning to handle previously-consumed streams
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        elif current_pos is not None and hasattr(file_obj, "seek"):
            # Fallback: if seek isn't available but we know current position,
            # we can't safely reset, but this is OK - the read() should work
            pass
    elif hasattr(file_obj, "getvalue"):
        # For objects with getvalue() method (like BytesIO)
        raw_bytes = file_obj.getvalue()
        # getvalue() doesn't consume the stream, so no need to reset
    else:
        raise TranscriptProcessingError(f"Unsupported file object type: {type(file_obj)}")

    if ext == ".txt":
        decoded_text = extract_text_from_file_bytes(raw_bytes, actual_filename)
    elif ext == ".pdf":
        decoded_text = _extract_text_from_pdf(raw_bytes, actual_filename)
    elif ext == ".docx":
        decoded_text = _extract_text_from_docx(raw_bytes, actual_filename)
    else:
        # Should not happen due to validation
        raise TranscriptProcessingError(f"Unsupported file format '{ext}'.")

    # Generate a default readable title from the filename
    base_name = os.path.splitext(actual_filename)[0]
    default_title = base_name.replace("_", " ").replace("-", " ").title()

    return NormalizedTranscript(
        raw_text=decoded_text,
        source="upload",
        meeting_title=default_title,
        metadata={
            "filename": actual_filename,
            "file_size_bytes": len(raw_bytes),
            "char_count": len(decoded_text),
            "line_count": len(decoded_text.splitlines()),
            "extension": ext,
        },
    )