# PDF Modernization Tool

Converts scanned PDF books into EPUB, HTML, and plain TXT with real selectable text. An AI model reads each page image, detects text blocks and illustrations, transcribes the text, and assembles a clean digital book. Everything is controlled through a web interface in your browser. 

---

## Getting started

> **Recommended:** Apple MacBook with M1 chip or newer.

> **Note:** Steps 1–4 are one-time setup. Once complete, you only need Step 5 to launch the app.

### Step 1 — Install tools

Open the **Terminal** app and run:

```bash
git --version
```

If the tools aren't installed yet, macOS will show a popup — click **Install** and wait for it to finish.

### Step 2 — Download the project

In Terminal, run these commands one by one:

```bash
mkdir ~/Desktop/PDF\ Modernization
cd ~/Desktop/PDF\ Modernization
git clone https://github.com/aulismedia/pdf-modernization.git
cd pdf-modernization
```

The project will be inside a `PDF Modernization` folder on your Desktop.

### Step 3 — Get an OpenRouter API key

The app uses AI models through OpenRouter.

> [!CAUTION]
> Processing pages costs money. Each page is sent to an AI model and billed by OpenRouter based on usage. A typical book of a few hundred pages costs a few dollars. In my experience I normally run at $4-5 per book of 300 pages. Add credits to your OpenRouter account before processing.

1. Go to [openrouter.ai](https://openrouter.ai) and create a free account
2. Go to **Settings → Keys** and create a new key
3. Copy the key — the installer will ask for it in the next step

### Step 4 — Run the installer

```bash
python3 install.py
```

The installer will set everything up and ask for your API key from Step 3.

### Step 5 — Start the app

```bash
./start.sh
```

Then open your browser and go to **http://localhost:5000**

> **macOS permission prompt:** the first time you start the app, macOS may ask _"Allow Python to find devices on local networks?"_ — click **Don't Allow**. The app runs entirely on your own computer and does not need network discovery -- unless you want to access books you store on network drives. In that case, you may allow it.

Next time you want to open the app, open Terminal and run this command:

```bash
~/Desktop/PDF\ Modernization/pdf-modernization/start.sh
```

---

## Pipeline

The tool walks a PDF through these stages:

```
Step 1  Extract pages       PDF → PNG images (one per page)

Step 2  AI detection        Each page image is sent to an AI model via OpenRouter.
                            The model identifies text blocks, headings, footnotes,
                            illustrations, and captions, and transcribes all text.

Step 3  Visualize           Colour-coded overlays are drawn on each page image
                            so you can see exactly what was detected.

        ── Manual review ──────────────────────────────────────────────────
        Open the Area Editor in the browser.
        Drag handles to correct area boundaries, inspect OCR text, set a
        content crop on pages with wide margins or scanner artifacts, and
        mark difficult pages for a second Opus pass (see below).
        ───────────────────────────────────────────────────────────────────

Step 4  Extract elements    Illustration regions are cropped into separate images.
Step 6  Clean text          An AI pass cleans up OCR errors and normalises formatting.
Step 7  Assemble HTML       All text and images are combined into a flowing HTML file.
Step 8  Export EPUB         The HTML is packaged as a standard EPUB 3 e-book.
Step 9  Export TXT          Plain text is extracted in reading order.
```

All steps are triggered through the web interface — no command line needed after setup.

All output files are saved in the same folder as the original PDF.

---

## Area Editor — manual review tools

After Step 2 and Step 3, open the Area Editor in the browser before running the remaining steps. The editor gives you several tools to correct or improve the AI's output:

**Drag handles** — resize or reposition any detected area boundary directly on the page image.

**Inspect OCR text** — click any area to see the transcribed text and correct obvious errors.

**Mark pages to skip** — flag spine pages, blank pages, or any page that should not appear in the output.

**Content crop (content bbox)** — draw a rectangle on a page to tell the AI exactly where the printable content is. When a content crop is set, Step 2 sends only that cropped region to the model instead of the full scan, which improves accuracy on pages with wide scanner borders, dark edges, or margin annotations that confuse the detector.

**Flag for Opus retry** — mark individual pages as *process later*. These pages are skipped in the main Step 2 run and can be re-sent later to Claude Opus (a stronger, slower model) via the **Run Opus on flagged pages** button. Use this for pages where the default model produced garbled OCR or missed areas — Opus handles degraded or complex scans more reliably.

---

## Step 7 — HTML assembly

Step 7 reads the polished JSON produced by Step 6 and assembles a single flowing HTML document. Key things it handles automatically:

- **Cross-page paragraph stitching** — body text that continues across a page break is joined seamlessly. The `page_join` field set by Step 6 controls whether the join is a hyphen merge, a sentence continuation, or a new paragraph.
- **Running footnotes** — footnotes that start on one page and continue on the next are detected and merged. The detection rule: a footnote area whose leading marker does not appear in the current page's body text is treated as a continuation of the previous footnote.
- **Footnote linking** — numeric (`<sup>N</sup>`), Unicode superscript (`¹²³`), and symbolic (`* ** ***`) footnote markers in body text are linked to their footnote items so clicking the marker jumps to the note, and clicking the note jumps back.
- **Illustrations and captions** — illustration regions are rendered as `<figure>` elements with the cropped image and any linked caption.
- **Area ordering** — areas within each page are sorted top-to-bottom, left-to-right before rendering.
- **Title page, chapter titles, subtitles** — each area type is rendered with the appropriate CSS class for correct visual hierarchy.

After Step 7 runs, a `postprocess_footnote_links.py` pass is applied automatically to wire up all footnote anchors across the document.

---

## Post-processing and structural assembly — manual prompt

After the seamless HTML is built, a final review pass in VS Code (or Claude Code) is recommended to catch anything the automated pipeline missed. The prompt lives in `prompts/manual.txt` and covers:

- OCR garble, incorrect word breaks, character confusions (ь/ъ, ш/т, Latin letters in Cyrillic words)
- Footnote linking gaps — any unmatched marker/item pairs that postprocess could not resolve automatically
- Chapter title and subtitle structure — headings split across lines, headings run together, wrong area ordering
- Page header/footer content bleeding into body text
- Spine or half-title pages that should be ignored

**How to use it:** open the HTML output and the book's JSON side-by-side in VS Code. Copy the contents of `prompts/manual.txt` into a Claude Code (or similar AI assistant) session with both files in context. All corrections are made in the JSON (never directly in the HTML), then Step 7 and the postprocess script are re-run to regenerate the HTML from the fixed source.

---

> The sections below are for developers and technical users. If you just want to use the app, everything you need is above.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Python 3.9+ | |
| OpenRouter API key | Used for both area detection (Step 2) and text polishing (Step 6) |

The default model for area detection is `google/gemini-2.5-flash-preview` via OpenRouter. You can change the model priority in `utils/detect_models.cfg`.

---

## Output structure

Each book gets its own folder. All outputs are written inside it:

```
<book-dir>/
├── <stem> - pages/         PNG image of every page  (step 1)
├── <book-name>.json        Area detection + OCR data  (step 2)
├── <stem> - elements/      Cropped illustration images  (step 4)
├── <book-name>.html        Assembled HTML  (step 7)
├── <book-name>.epub        EPUB export  (step 8)
└── <book-name>.txt         Plain text export  (step 9)
```

---

## Detected area types

| Type | Description | Text extracted |
|------|-------------|:-:|
| `main_text` | Body text block | ✓ |
| `chapter_title` | Chapter or section heading | ✓ |
| `subtitle` | Sub-section heading | ✓ |
| `title_page` | Title page line | ✓ |
| `illustration` | Image or figure | — |
| `illustration_caption` | Caption linked to an illustration | ✓ |
| `footnote` | Footnote | ✓ |
| `header` | Running page header | ✓ |
| `footer` | Running page footer | ✓ |
| `page_number` | Page number | ✓ |
| `decoration` | Ornamental element with no text value | — |

---

## Configuration

**Model priority** (`utils/detect_models.cfg`) — controls which AI model is used for area detection and the fallback order. Comment out a line with `##` to disable it temporarily (e.g. after a quota error).

**API key** (`.env`) — created automatically by the installer. To update the key, run `python install.py` again.
