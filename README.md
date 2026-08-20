# Auto-BRD Generator from Meeting Notes

This project is a Streamlit web application that converts raw, unstructured meeting notes into a draft Business Requirements Document (BRD). It is designed as a portfolio piece to demonstrate skills in business analysis, requirements engineering, and the responsible application of Large Language Models (LLMs).

## The Problem It Solves

Business Analysts and Product Managers often spend significant time manually sifting through messy meeting notes to extract and structure key decisions, requirements, and action items. This manual process is time-consuming and prone to error. This tool aims to automate the initial draft of a BRD, freeing up the analyst to focus on higher-value tasks like validation, clarification, and strategic planning.

## Core Principle: The Notes Are the Source of Truth

The application's most important feature is its commitment to traceability. It will **never** present an AI-inferred statement as a confirmed fact.

- **Confirmed Requirements** must have direct, verifiable `Source Evidence` from the original notes.
- **Assumptions** are clearly flagged when the AI makes a logical inference that isn't explicitly stated.
- **Open Questions** are captured to highlight ambiguities and items needing follow-up.

This ensures the generated BRD is a trustworthy starting point, with all ambiguities preserved for human review.

## Architecture and Data Flow

The application uses a simple, easy-to-explain architecture that prioritizes validation and traceability.

```
User pastes meeting notes
        ↓
Streamlit User Interface
        ↓
Python Application Logic
        ↓
Google Gemini API Call
        ↓
Structured JSON Response
        ↓
Python Evidence Validation Layer
        ↓
BRDData Object Creation
        ↓
BRD Displayed in UI & Exportable as Markdown
```

### How Source Grounding Works

The "Python Evidence Validation Layer" is the key to the application's reliability.

1.  The Gemini API is instructed to return a `source_evidence` field for every requirement it identifies.
2.  After receiving the API response, the Python backend iterates through each "confirmed" requirement.
3.  It performs a simple but crucial check: `if evidence_string in original_meeting_notes:`.
4.  If the evidence is present in the source text, the requirement is accepted.
5.  If the evidence is missing or doesn't match, the requirement is **re-classified** as an `Assumption`, preventing AI hallucinations from being presented as facts.

## Technology Stack

- **Python 3.11+**
- **Streamlit**: For the interactive web UI.
- **Google Gemini API**: For natural language processing and information extraction.
- **pytest**: For unit testing the core validation logic.

## How to Run the Project Locally

1.  **Clone the repository:**

    ```bash
    git clone <repository-url>
    cd auto-brd-generator
    ```

2.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure your API Key:**
    - Obtain a Gemini API key from Google AI Studio.
    - Create a file named `.env` in the project's root directory.
    - Add your API key to the `.env` file like this:
      ```
      GEMINI_API_KEY="YOUR_API_KEY_HERE"
      ```
    - The `.gitignore` file is already configured to prevent this file from being committed to Git.

4.  **Run the Streamlit application:**

    ```bash
    streamlit run main.py
    ```

5.  **Run the tests (optional):**
    ```bash
    pytest
    ```

## Limitations of the MVP

This is a portfolio project and proof-of-concept, not a production-ready application. Key limitations include:

- **Simple Evidence Check:** The validation `evidence in notes` is basic and could be improved with more advanced NLP techniques for semantic matching.
- **No User Accounts or History:** The application is stateless. Each generation is a new event.
- **Single File Format:** Export is currently limited to Markdown.
- **Prompt Sensitivity:** The quality of the output is highly dependent on the prompt and the structure of the notes. Very unusual note formats may produce suboptimal results.
