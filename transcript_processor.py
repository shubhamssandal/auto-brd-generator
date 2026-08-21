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
    if ext.lower() not in [".txt"]:
        raise TranscriptProcessingError(
            f"Unsupported file format '{ext}'. Initially, only .txt files are supported."
        )

    # Read bytes
    if isinstance(file_obj, bytes):
        raw_bytes = file_obj
    elif hasattr(file_obj, "read"):
        raw_bytes = file_obj.read()
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
    elif hasattr(file_obj, "getvalue"):
        raw_bytes = file_obj.getvalue()
    else:
        raise TranscriptProcessingError(f"Unsupported file object type: {type(file_obj)}")

    decoded_text = extract_text_from_file_bytes(raw_bytes, actual_filename)

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
        },
    )
