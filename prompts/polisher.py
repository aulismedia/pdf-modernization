I have a batch of text blocks from an OCR-processed document. Complex text may include:
Poorly OCR'ed blocks of text
Maybe Pre-reform Russian (ѣ, і, ѳ, ѵ, ъ).
Maybe  Aleut Cyrillic (г̑, к̑, н̑, х̑).
Maybe  Old Norwegian punctuation and specific characters.
French and English fragments.

You will receive a JSON array. Each item has:
- "id": integer index
- "type": "main_text" or "footnote"
- "text": the text content to analyze
- "is_last_main": true — present only on the last main_text block of the page; used for page_join detection
- "next_page_prefix": first 10 words of the following page — present only on the item with "is_last_main": true

Your Goal:
Analyze each text block and identify only the issue types listed in Rules below. Return the result ONLY as a JSON array. Do not change the original text directly; provide find/replace pairs.

CRITICAL — Verbatim find strings:
The "find" value MUST be copied CHARACTER-FOR-CHARACTER from the input "text" field.
- Do NOT change any character, spacing, or punctuation in "find"
- Do NOT pre-apply corrections to "find" (e.g. do not replace a digit with a superscript digit)
- "replace" is the only field where corrections go
- If you cannot find the exact substring in the text, skip that fix entirely

Rules:

OCR Fixes: Find words split by a hyphen followed by whitespace (e.g., "пози- тивный" → "позитивный", "пара- дигма" → "парадигма").
Report them as: {"type": "ocr_hyphen", "part1": "пози", "part2": "тивный"}
"part1" is the fragment before the hyphen. "part2" is the continuation after the hyphen and any whitespace. Do NOT include the hyphen or any whitespace in part1 or part2.
IMPORTANT — „/,, and -, line-start markers: In 18th–19th century Russian typography, some texts prefix every continuation line with „, ,,, or -, as a typographic convention. When a hyphenated split occurs at such a line boundary (e.g., "лю-\n„бящаго", "уча-\n„ствовать", "подлин-\n,,номъ", "че- -,тыре"), treat it as a normal ocr_hyphen. part2 must be the continuation word WITHOUT the leading marker (e.g., part1="лю", part2="бящаго"; part1="че", part2="тыре").
EXCEPTION — cross-page hyphen: Applies only to items with "is_last_main": true. If the very last characters of the text are a word fragment ending in a hyphen (e.g., the text ends with "...With uncom-" or "...mother-to-be is heav-"), and the first word of "next_page_prefix" is the continuation that completes it into a real word — then:
  • Do NOT emit an ocr_hyphen fix for this trailing fragment.
  • DO include "page_join": "merge_hyphen" in this item's result.
  The key test: the hyphen is the final non-whitespace character of the text. If so, it is a cross-page split, not an in-page OCR artifact.

OCR Garble: Find words where individual letters or short clusters were misrecognised as visually similar characters, producing a clearly corrupted word. This happens frequently in pre-reform Russian OCR.
Report as: {"type": "ocr_garble", "find": "...", "replace": "..."}
The "find" value must be copied CHARACTER-FOR-CHARACTER from the input text.

Common OCR confusions to watch for:
- "ш" recognised as "п" or as "лп" or as "ы п" (the two legs of ш split apart): болыпемъ → большемъ, полще → толще
- "т" recognised as "ш" (especially after с): памяшь → память, осшавилъ → оставилъ
- "в" recognised as "нн" or "нь": Алексѣенны → Алексѣевны
- "е" recognised as "с" (mirrored shape): мосго → моего, мосмъ → моемъ
- "і" recognised as "а" or vice versa: предпріятаю → предпріятію
- Latin, Greek, or other-alphabet characters mixed into Cyrillic words (e.g., "ο", "ι", "π", "f" inside a Russian word): οιπвозившемъ → отвозившемъ, фють → флоть
- ъ recognised as ь (or vice versa) at word endings in pre-reform Russian: Кронштать→Кронштатъ, начальствомь→начальствомъ, промышленныхь→промышленныхъ. Fix only when the correct hard/soft sign is unambiguous from context.
- Digit recognised as letter: 0 (zero) in a Cyrillic word where о is intended, 6 where б is intended (e.g., 6ольшой→большой, г0родъ→городъ). Fix only when clearly in an alphabetic context.
- 3rd-person reflexive verb endings: OCR produces -ешся/-ишся (2nd-person form) when the grammatical subject is 3rd-person (a noun or он/она/оно) — fix to -ется/-ится. Do NOT fix when the verb is genuinely 2nd-person address to the reader (e.g., "ты остаешся").
- A single corrupted word may have multiple such substitutions; fix the whole word in one find/replace pair.

STRICT LIMITS — skip the fix if ANY of these apply:
- The correct reading is ambiguous or requires guessing beyond what context makes unambiguous.
- The word looks unusual but could be a genuine archaic or dialectal spelling.
- You would need to change ъ, ѣ, і, ѳ, ѵ at word-endings or roots — those are real pre-reform orthography, not OCR errors.
- The word appears only once and context does not make the intended reading clear.
When in doubt, skip. Do not invent corrections.

VOLUME LIMIT: Emit at most 10 ocr_garble fixes per text block. If you find more than 10, include only the 10 most unambiguous ones. "find" and "replace" must each be a single word or at most a two-word phrase — never a full sentence or clause.

Footnote Indexes: Find inline footnote reference markers in main_text areas that are missing <sup> wrapping. A number qualifies only if BOTH conditions hold:
  (a) It matches a leading number of one of the footnote areas on this page (e.g. a footnote area whose text starts with "2. …" means marker 2 is expected).
  (b) That same number does NOT already appear as <sup>N</sup> anywhere in the main_text areas on this page — i.e. it is not yet linked.

Three forms to detect:
- Trailing number after closing punctuation or quote with no space before it (e.g. text ending `."1` or mid-text `word."2 Next`) → wrap only the number: `."<sup>1</sup>` or `word."<sup>2</sup> Next`.
- Standalone number between words that breaks grammatical flow (e.g. "text 12 more text" → "text <sup>12</sup> more text"). The "find" must be the plain digit as it appears in the original text.
- Symbolic markers: *, †, ‡, § appearing inline after a word (e.g. "word* more text" → "word<sup>*</sup> more text"). Symbolic markers do not need the footnote-area cross-check.

CRITICAL — the candidate number must be a complete standalone token, isolated by whitespace or punctuation on both sides. NEVER extract a digit from within a longer number. Examples of what must NOT be wrapped: "1898" (year), "1,800" (price), "10th" (ordinal), "1st", "No. 1", "p. 2", "56th Congress", "Article 3", "chapter 1", "January 1". If the digit is adjacent to another digit (e.g. "1" in "1898" or "18" in "1898"), skip it entirely.

Page Join: Only for the item where "is_last_main" is true and "next_page_prefix" is provided, determine how the end of the page connects to the beginning of the next page. Choose one of:
- "merge_hyphen": The last word on the current page is hyphenated and continues on the next page (e.g., "прими-" + "рение" → "примирение"). Strip the hyphen and join the two parts into one word.
- "merge_sentence": The current page ends mid-sentence and the next page continues it without a paragraph break. Keep words as-is, join with a space.
- "new_paragraph": The next page starts a new paragraph or section.
Include "page_join" only in the result object for that item.

Strict Preservation: Do NOT "modernize" pre-reform Russian. Do NOT touch Aleut diacritics. If you are unsure if a number is a footnote or a date/page number, leave it alone. Do NOT alter Unicode superscript digit characters (⁰¹²³⁴⁵⁶⁷⁸⁹) — leave them exactly as-is.

Output Format:
Return ONLY a JSON array. Include an item in the response ONLY if at least one of these is true:
- it has one or more fixes
- it is the "is_last_main" item (always include it so page_join is reported)

Omit items that have no fixes and are not "is_last_main" — do not emit {"id": N, "fixes": []} for clean areas.

Example:
[
  {"id": 1, "fixes": [
    {"type": "ocr_hyphen", "part1": "Babylo", "part2": "nian"},
    {"type": "footnote", "find": "text 12 more text", "replace": "text <sup>12</sup> more text"}
  ]},
  {"id": 3, "fixes": [], "page_join": "merge_sentence"}
]
"page_join" is only present on the "is_last_main" item and only when "next_page_prefix" was provided.
