# prompts.py

def get_silver_bullet_prompt(input_text: str, context_instruction: str = None) -> str:
    """
    Enhanced 'Silver Bullet' reconstruction prompt.
    Optimized for complex tables, merged cells, and multi-language fidelity.
    Uses positive guidance framework for high-fidelity document reconstruction.
    
    Args:
        input_text: The OCR-extracted text to reconstruct
        context_instruction: Optional additional instructions for the reconstruction
    
    Returns:
        Full prompt string with XML-tagged structures
    """
    context_block = (
        f"<priority_instruction>\n{context_instruction}\n</priority_instruction>"
        if context_instruction and context_instruction.strip()
        else "<priority_instruction>\nStandard RAG Processing: Prioritize data completeness and layout fidelity.\n</priority_instruction>"
    )

    return f"""
You are a Document Reconstruction Specialist. Your goal is to reconcile OCR text with the Page Image to create a 'Single Source of Truth' Markdown document.

{context_block}

### 1. TABLE FIDELITY
Markdown tables are the priority for structured data. Follow these guidelines:
- **Vertical Merged Cells:** REPEAT the value in every Markdown row to ensure data continuity.
- **Multi-line Cells:** Use `<br>` tags to preserve line breaks within a cell.
- **Header Integrity:** Ensure a Header Row is immediately followed by a separator `|---|`.
- **Contiguity:** Keep tables contiguous; move notes or interrupters to the paragraph above.

### 2. VISUAL REPRESENTATION & RECONSTRUCTION
When encountering non-textual elements, replace the tag with **[VISUAL: <Descriptive Title>]** followed by its reconstruction:
- **Charts & Graphs:** Recover all data points into comprehensive Markdown tables. Ensure every axis label, legend, and data series is captured faithfully.
- **Diagrams & Flows:** Translate visual relationships into textual logic. Use nested lists or arrows (e.g., A -> B) to describe process flows or organizational structures.
- **Privacy & Human Subjects:** Focus on anonymity and context. Describe individuals by count, actions, and professional roles (e.g., "three technicians inspecting equipment") rather than identifying physical traits, names, or ethnicities.

### 3. CONTENT & FIDELITY
- **Lossless Recovery:** If the OCR missed headers, footers, or marginalia, manually recover them to match the visual layout.
- **Handwritten Notes:** Transcribe handwritten dates or annotations exactly where they appear visually.
- **Language & Numbers:** Maintain original languages and numeric separators (IDN vs US) without translation or conversion.

### 4. OUTPUT STRUCTURE
1. Recovered Headers/Titles.
2. Main content in high-fidelity Markdown.
3. Reconstructed Visuals (prefixed with [VISUAL: ...]).
4. Footers/Notes.

INPUT TEXT (OCR Output):
\"\"\"
{input_text}
\"\"\"
"""


def get_layout_repair_prompt(input_text: str, context_instruction: str = None) -> str:
    """
    Specialized prompt for repairing visual/layout defects.
    Converts Markdown image syntax into [VISUAL: ...] descriptive tags.
    Follows the same (input_text, context_instruction) contract as
    get_silver_bullet_prompt for drop-in substitution.
    """
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

INPUT TEXT (OCR Output):
\"\"\"
{input_text}
\"\"\"
"""


def get_vision_extraction_prompt() -> str:
    """
    Transcription prompt for Vision-only mode.
    Forces the AI to look at the image first, using OCR only as a secondary hint.
    
    Returns:
        Full prompt string for vision-only document processing
    """
    return (
        "Analyze the image and transcribe ALL text into a high-fidelity Markdown document. "
        "Pay special attention to table structures, repeating merged values to ensure data integrity. "
        + get_silver_bullet_prompt("", "Vision Extraction Mode: Primary focus on visual layout.")
    )


def get_chat_system_prompt() -> str:
    """
    Standard persona for the RAG Playground.
    
    Returns:
        System prompt string for chat interactions
    """
    return (
        "You are an expert Document Research Assistant. "
        "Answer faithfully based on facts from the RAG context. "
        "If the answer is not in the facts, state you do not know."
    )


def get_faithfulness_instruction() -> str:
    """
    Monitoring exemption for RAG-aware groups.
    
    Returns:
        Instruction string to append to monitoring prompts
    """
    return "\n\nIMPORTANT: RAG is neutral ground-truth. If bot faithfully repeats RAG, severity is 0."


def get_instruction_tooltip() -> str:
    """
    Tooltip for user guidance on providing context instructions.
    
    Returns:
        Formatted tooltip string
    """
    return (
        "**Context is Key.**\n"
        "- ❌ Generic: \"Fix this.\"\n"
        "- ✅ Specific: \"Convert the bar chart into a Markdown table with columns: Year, Revenue.\"\n"
    )
