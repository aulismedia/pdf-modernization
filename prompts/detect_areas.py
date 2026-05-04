DETECT_AREAS_PROMPT = """\
You are a precise document-layout analyser. Analyse this book page image and \
identify every distinct content area.

Return ONLY a valid JSON object — no markdown fences, no prose, no comments, \
no whitespace indentation (compact single-line JSON). \
The JSON must conform exactly to this schema:

{
  "page_dimensions": { "width": <int px>, "height": <int px> },
  "areas": [
    {
      "id": "area_001",
      "type": "<type>",
      "polygon": [[x1,y1],[x2,y1],[x2,y2],[x1,y2]],
      "text": "<string or null>",
      "illustration_id": "<string or null>",
      "linked_illustration_id": "<string or null>"
    }
  ]
}

Rules:
1. Allowed types: header | footer | page_number | illustration | main_text | illustration_caption | footnote | decoration | chapter_title | marginalia
2. Use PIXEL coordinates matching the actual image dimensions you received.
3. Every polygon MUST be an axis-aligned rectangle with EXACTLY 4 vertices:
   [top-left, top-right, bottom-right, bottom-left]. No other shapes or vertex
   counts are permitted.
4. Provide "text" (verbatim OCR transcription) for types: main_text, footnote, illustration_caption, chapter_title, header, footer, page_number, marginalia. Set to null for illustration and decoration.
5. For each illustration assign "illustration_id" like "illus_001", "illus_002".
   Set to null for non-illustration areas.
6. For each illustration_caption set "linked_illustration_id" to the
   illustration_id of the illustration it describes. Set to null otherwise.
7. "chapter_title" is a standalone word or short phrase that names a chapter,
   part, or section of the book (e.g. "Preface", "Chapter 1", "Introduction",
   "Part Two"). It typically appears centred, in larger or distinct type, isolated
   from the body text. Do NOT classify it as main_text or header.
   "header" is reserved for running headers — the repeated line at the very top
   margin of a page showing the book title, chapter name, or author, usually in
   small or italic type. A large centred title word is never a header.
8. "decoration" covers purely ornamental elements (dividers, flourishes, borders, vignettes) that carry no informational value.
9. Ignore scanner artefacts: black or dark borders/bars around the page edge, shadow gradients, bleed-through from the reverse side, and any other noise originating outside the physical page boundary. Do NOT create areas for these — treat them as non-content background. IMPORTANT: decorative borders that are part of an illustration (e.g. a meander band, picture frame, or ornamental ring surrounding artwork) are NOT scanner artefacts — include them inside the illustration polygon. Only ignore noise that originates outside the printed page area.
10. page_number is the numeric identifier of the current page. It is its own type even if visually inside the header or footer zone. when a part of the list of tables of contents, it should be a part of the main text area, not its own type and not an area by itself.
11. There may be multiple areas of types: main_text, illustration, illustration_caption, footnote.
12. marginalia is text written in the margins. These may be notes, comments, or references.
13. For titles or subtitles spanning multiple lines or columns, create a single area that will encompass the entire title or subtitle.
14. For tables, create a single illustration area that will encompass the entire table. Set it's type to illustration.
15. Transcribe text with these rules:
    - Distinguish hyphens from dashes: a hyphen joins compound words or splits a word across lines (e.g. "tree-living", "prefer-\\nence") — no spaces around it. An em dash or en dash separates clauses or parenthetical phrases (e.g. "two feet and two hands — one of which could be used for hurling missiles") — always render it with a space on each side and the word must NOT be glued directly to the dash. Use "—" (em dash) or "–" (en dash) as appropriate; never substitute a plain hyphen for a dash.
    - Remove soft hyphens: when a word is broken across a line with a hyphen (e.g. "prefer-\\nence"), join the parts and drop the hyphen → "preference".
    - Remove line-wrap breaks: when a sentence continues on the next line without
      a hyphen, join the lines with a single space instead of \\n.
    - Use \\n\\n only between paragraphs.
    - Use \\n only for intentional hard breaks that carry meaning (e.g. verse lines, headings, or list items). Never use \\n merely because the printed line ended.
    - OCR Fixes: Fix words split by a hyphen followed by whitespace (e.g., "пози- тивный" → "позитивный", "пара- дигма" → "парадигма") only if all parts of the word are present in the text on this page.
"""

DETECT_AREAS_STYLES_ADDON = """\
16. Bold and italic text styles: For main_text areas only, wrap bold text in <strong> and italic text in <i> HTML tags. \
Do not add HTML tags to any other area type. Only <strong> and <i> are permitted — no other HTML tags may appear in text content.
"""
