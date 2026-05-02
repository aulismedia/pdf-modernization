# Pipeline Knowledge — Pre-Reform Russian Books

Accumulated fixes and patterns from processing 18th–19th century digitized Russian books (OCR via Gemini, polish via OpenRouter). Apply to every new book.

---

## Core Rule

Pre-reform Russian spellings are LEGITIMATE — do NOT correct: ъ, ѣ, і, ѳ, ѵ at word endings/roots, archaic forms, dialectal spellings. Only fix genuine OCR character confusions.

---

## Pipeline Code Fixes (already in repo)

### `_INLINE_HYPHEN` regex — `steps/step6_polish_text.py`

```python
_INLINE_HYPHEN = re.compile(r"(\w)-(?=[ \t\n]|„|,,|-,)[ \t\n]*(?:„|,,|-,)?[ \t]*(\w)")
```

18th–19th century block quotes prefix every continuation line with „, ,,, or -, . Without the lookahead `(?=...)`, the old regex matched real compound hyphens like `Ново-Архангельскъ`. The lookahead requires whitespace or a marker after the hyphen.

### Trailing-hyphen fallback — `steps/step7_seamless_html.py`

In `merge_sentence` branch, strips trailing hyphen before joining pages:

```python
elif prev_page_join == "merge_sentence" and pending:
    stripped = pending.rstrip()
    if stripped.endswith("-"):
        pending = stripped[:-1] + para
    else:
        pending = stripped + " " + para
```

---

## Polisher Prompt Rules (already in `prompts/polisher.py`)

- **„/,, and -, line-start markers:** Treat `word-\n„continuation`, `word-\n,,continuation`, `word- -,continuation` as normal `ocr_hyphen`. `part2` must NOT include the leading marker.
- **Cross-page hyphen exception:** If `is_last_main` text ends with a bare hyphen and `next_page_prefix` completes the word → emit `page_join: merge_hyphen`, do NOT emit `ocr_hyphen`.

---

## Marginalia Contamination

OCR area detection injects `marginalia` content into `main_text` areas, producing artifacts:

```
Когда Якуты увидятъ на дорогѣ1802 годъ медвѣдя...
параСентябрь. лелли 40 градусовъ   ← split a word
```

**Fix rules when scripting cleanup:**
- Remove NOMINATIVE year markers: `\d{4} годъ` — only `годъ`, NOT `года` (genitive `года` appears in real dates like `Августа 29 дня 1805 года`)
- Remove NOMINATIVE month names followed by `.` or `,` — but NOT when followed by a digit (diary headers like `Августъ 1. Вѣтръ` must survive)
- After removal, manually reconstruct any word fragments split by the injection

---

## Common OCR Character Confusions

| OCR error | Correct | Confirmed examples |
|-----------|---------|-------------------|
| `ш` → `т` | т | `версшы`→`версты`, `Якушы`→`Якуты`, `поднимаешся`→`поднимается`, `Совѣшникь`→`Совѣтникъ`, `есшьли`→`естьли`, `разобьещся`→`разобьется` |
| `п` → `т` | т | `возврапилась`→`возвратилась` |
| `с` → `е` | е | `такос`→`такое` |
| `ь` → `ъ` | ъ | `Кронштать`→`Кронштатъ`, `начальствомь`→`начальствомъ`, `промышленныхь`→`промышленныхъ`, `лѣсь`→`лѣсъ` |
| `ъ` → `ѣ` or `ь` → `ѣ` | ѣ | `мъсто`→`мѣсто`, `хльбомъ`→`хлѣбомъ` |
| `щ` → `т` | т | `разобьещся`→`разобьется` |
| `мм` → `м` | м | `камменныхъ`→`каменныхъ` |
| double letter | single | `Анникушанѣѣ`→`Анникушанѣ` |
| Latin `e` in Cyrillic word | е | `сіe`→`сіе`, `отверстіe`→`отверстіе` |
| Latin `o` in Cyrillic word | о | mixed-script words — but compass bearings like `NOTO` should stay all-Latin |
| missing space | space | `доПономаревскаго`→`до Пономаревскаго` |

**3rd-person reflexive verbs:** OCR produces 2nd-person `-ешся`/`-ишся` when subject is 3rd-person (`олень`, `мясо`, etc.) → fix to `-ется`/`-ится`. But 2nd-person address to reader (`остаешся мнѣ`, `останавливаешся`) is LEGITIMATE.

**Якуты:** tribal name frequently OCR'd as `Якушы` / `Якушъ`. Always fix. But `Якушъ` as a personal name (father's given name context) may be legitimate — verify against scan.

---

## Post-Polish Manual Patching Checklist

After `step6_polish_text.py`, scan JSON and patch directly for:

1. Remaining `word- „word` / `word- ,,word` / `word- -,word` (LLM may miss dense block-quote sections)
2. `версшы`, `Якушы`, 3rd-person `-ешся`/`-ишся` verb errors
3. ъ↔ь swaps at word endings
4. Latin letter intrusions into Cyrillic words
5. Marginalia fragments not caught by automated removal

After all JSON patches → re-run `step7_seamless_html.py` to regenerate HTML.

---

## Area Type Rules

- Never change area type from `illustration` to `main_text` or any other type — illustration areas drive figure/image rendering in HTML/EPUB output.
- `chapter_title` areas appear as centered bold section headers in output; `subtitle` as smaller bold headers; `title_page` as centered lines.
