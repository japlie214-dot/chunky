"""
procedure/utils/prompts.py
Self-contained copy of the prompts used by the procedure handlers.

Source: top-level prompts.py (verbatim where possible). Lives in the
procedure bundle so Snowflake IMPORTS don't have to reach back to the
Streamlit-side module.
"""
from __future__ import annotations
from typing import Optional


def get_silver_bullet_prompt(input_text: str,
                             context_instruction: Optional[str] = None) -> str:
    """Enhanced 'Silver Bullet' reconstruction prompt."""
    context_block = (
        f"<priority_instruction>\n{context_instruction}\n</priority_instruction>"
        if context_instruction and context_instruction.strip()
        else "<priority_instruction>\nStandard RAG Processing: Prioritize data completeness and layout fidelity.\n</priority_instruction>"
    )
    return f"""
You are a Document Reconstruction Specialist. Convert the page image into lossless, structured Markdown — a 'Single Source of Truth' faithful to the original.

{context_block}

## CORE RULES

1. **Reproduce, don't summarize.** Every word, number, symbol in the image appears in your output. Nothing invented, nothing omitted.
2. **Mark uncertainty honestly.** Illegible text → `[unclear: best guess]` or `[?]`. Never guess silently. Never fabricate.
3. **Preserve spatial relationships.** Layout conveys meaning.
4. **Image is ground truth.** Translate into Markdown. Do not interpret, correct, or improve.

## TABLES
- Merged cells (vertical): REPEAT value in every row it spans.
- Merged cells (horizontal): value in leftmost column; leave spanned columns empty or repeat.
- Multi-line cells: use `<br>`.
- Headers: every column header MUST have a value. Empty headers are forbidden.
- Empty cells: `| |` — not placeholder text.
- Numbers: exact reproduction. Do NOT round, reformat, or convert units.

## CHARTS & VISUAL ELEMENTS
- Charts: reconstruct into raw structured data. Extract every visible data point into Markdown tables. Add a brief narrated description after the table(s).
- All other visual elements (photos, illustrations, logos, diagrams): reconstruct as descriptive text. Use `[VISUAL: ...]` tags.

## NUMBERS & DATA
Copy exactly as shown. No rounding, no reformatting.

## LANGUAGE
Maintain original languages. Do NOT translate. Preserve diacritics and script mixing.

## OUTPUT
Produce the Markdown reconstruction that is truest to the image reference. No commentary. Output only the Markdown.

INPUT TEXT:
\"\"\"
{input_text}
\"\"\"
"""


def get_layout_repair_prompt(input_text: str,
                             context_instruction: Optional[str] = None) -> str:
    """Specialized prompt for repairing visual/layout defects."""
    context_block = (
        f"<priority_instruction>\n{context_instruction}\n</priority_instruction>"
        if context_instruction and context_instruction.strip()
        else "<priority_instruction>\nFocus on visual layout reconstruction.\n</priority_instruction>"
    )
    return f"""
You are a Document Reconstruction Specialist. Your goal is to reconcile OCR text with the Page Image to create a 'Single Source of Truth' Markdown document.

{context_block}

### 1. VISUAL ELEMENT RECONSTRUCTION (CRITICAL)
- Detect ALL standard Markdown image syntax (e.g., `![alt text](url)`) in the input text.
- Transform each occurrence into the format: `[VISUAL: <Descriptive Title>]` where the title is inferred from the alt text or surrounding context on the page image.
- If the alt text is empty or generic, derive a meaningful title from the image's position and the surrounding paragraph content visible in the page image.
- IMPORTANT: Do NOT add, invent, or reference any URLs in your output.

### 2. TABLE AND STRUCTURAL FIDELITY
- Preserve all Markdown table structures exactly as they appear in the source image.
- Repeat merged cell values to ensure data continuity across rows.
- Use `<br>` tags for multi-line cell content.

### 3. TEXT PRESERVATION
- Retain all body text, headings, and list items verbatim from the source image.
- Correct only obvious OCR artifacts (broken words, phantom spaces in numbers).
- Do NOT summarize, paraphrase, or omit any content.

INPUT TEXT:
\"\"\"
{input_text}
\"\"\"
"""


def get_vision_extraction_prompt() -> str:
    """Transcription prompt for Vision-only mode."""
    return (
        "Analyze the image and transcribe ALL text into a high-fidelity Markdown document. "
        "Pay special attention to table structures, repeating merged values to ensure data integrity. "
        "Every word, number, symbol in the image must appear in the output. "
        "Mark illegible text with [unclear: best guess] or [?]. "
        "Preserve spatial relationships — layout conveys meaning. "
        "Image is ground truth: translate into Markdown, do not interpret or improve.\n\n"
        + get_silver_bullet_prompt("", "Vision Extraction Mode: Primary focus on visual layout.")
    )


def get_chat_system_prompt() -> str:
    """Standard persona for the RAG Playground."""
    return (
        "You are an expert Document Research Assistant. "
        "Answer faithfully based on facts from the RAG context. "
        "If the answer is not in the facts, state you do not know."
    )
