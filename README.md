# PDF Modernization Tool

Converts scanned PDF books into EPUB, HTML, and plain TXT with real selectable text. An AI model reads each page image, detects text blocks and illustrations, transcribes the text, and assembles a clean digital book. Everything is controlled through a web interface in your browser. 

---

## Getting started

> **Recommended:** Apple MacBook with M1 chip or newer.

### Step 1 — Install tools

Open the **Terminal** app and run:

```bash
git --version
```

If the tools aren't installed yet, macOS will show a popup — click **Install** and wait for it to finish. This installs both Git and Python 3 in one step.

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

The installer will set everything up and ask for your API key. At the end it creates a `start.sh` file.

### Step 5 — Start the app

```bash
./start.sh
```

Then open your browser and go to **http://localhost:5000**

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

        ── Manual review ──────────────────────────────────────────
        Open the Area Editor in the browser.
        Drag handles to correct area boundaries, inspect OCR text,
        and mark any pages to skip.
        ───────────────────────────────────────────────────────────

Step 4  Extract elements    Illustration regions are cropped into separate images.
Step 6  Clean text         An AI pass cleans up OCR errors and normalises formatting.
Step 7  Assemble HTML       All text and images are combined into a single HTML file.
Step 8  Export EPUB         The HTML is packaged as a standard EPUB 3 e-book.
Step 9  Export TXT          Plain text is extracted in reading order.
```

All steps are triggered through the web interface — no command line needed after setup.

All output files are saved in the same folder as the original PDF.

---

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
