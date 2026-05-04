#!/usr/bin/env python3
"""Unified Flask app for the PDF modernisation pipeline."""

import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, stream_with_context, url_for)

PROJECT_ROOT = Path(__file__).parent
PROJECTS_FILE = PROJECT_ROOT / "projects.json"
app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SMART_QUOTE_CHARS = "‘’“”′″ʼ"

def _clean_path(value: str) -> str:
    """Strip macOS smart-quote decorations that Terminal pastes around paths."""
    return value.strip(_SMART_QUOTE_CHARS).strip()


# ── Atomic I/O ────────────────────────────────────────────────────────────────

def _write_atomic(path: Path, text: str) -> None:
    """Write text to a temp file then rename — prevents truncation on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── Projects DB ───────────────────────────────────────────────────────────────

def _load_projects() -> dict:
    if PROJECTS_FILE.exists():
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        # Migrate legacy pdf_path → source_path
        for p in data["projects"]:
            if "pdf_path" in p and "source_path" not in p:
                p["source_path"] = p.pop("pdf_path")
        return data
    return {"projects": []}


def _save_projects(data: dict) -> None:
    _write_atomic(PROJECTS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def _get_project(pid: str) -> dict | None:
    return next(
        (p for p in _load_projects()["projects"] if p["id"] == pid), None
    )


# ── Path helpers ──────────────────────────────────────────────────────────────

def _source_path(project: dict) -> Path:
    return Path(project.get("source_path", ""))


def _source_type(project: dict) -> str:
    """Returns 'pdf', 'djvu', or 'folder'."""
    p = _source_path(project)
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower()
    if ext == ".djvu":
        return "djvu"
    return "pdf"


def _book_dir(project: dict) -> Path:
    """Working directory: the folder itself for image-folder projects, parent dir otherwise."""
    p = _source_path(project)
    return p if p.is_dir() else p.parent


def _json_stem(project: dict) -> str:
    """Canonical stem for the areas JSON: source file stem, or 'author - title' for folders."""
    if _source_type(project) == "folder":
        author = (project.get("author") or "").strip()
        title  = (project.get("title")  or "").strip()
        return f"{author} - {title}" if author else title
    return _source_path(project).stem


def _elements_dir(project: dict) -> Path:
    """Return the elements directory, preferring <stem> - elements, falling back to elements/."""
    folder = _book_dir(project)
    stem   = _json_stem(project)
    canonical = folder / f"{stem} - elements"
    if canonical.exists():
        return canonical
    legacy = folder / "elements"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step4 will make it here


def _pages_dir(project: dict) -> Path:
    """Return the pages directory for a project.

    Layout: ``<book_dir>/<stem> - pages/``
    Legacy fallback: ``<book_dir>/pages/`` (existing data before this convention).
    """
    folder    = _book_dir(project)
    stem      = _json_stem(project)
    canonical = folder / f"{stem} - pages"
    if canonical.exists():
        return canonical
    legacy = folder / "pages"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step1 will make it here


def _try_recover_from_sidecars(project: dict, json_path: Path) -> None:
    """Rebuild main JSON from per-page sidecar files if they exist."""
    pages_dir = _pages_dir(project)
    if not pages_dir.exists():
        return
    pages: list[dict] = []
    for sc in sorted(pages_dir.glob("*-areas.json")):
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
            if data.get("source_image"):
                pages.append(data)
        except Exception:
            pass
    if not pages:
        return
    output = {
        "book":        project.get("title", _json_stem(project)),
        "total_pages": len(pages),
        "pages":       sorted(pages, key=lambda p: p["source_image"]),
    }
    _write_atomic(json_path, json.dumps(output, ensure_ascii=False, indent=2))
    print(f"[recovery] Rebuilt {json_path.name} from {len(pages)} sidecar files.")


def _book_json(project: dict) -> Path | None:
    p = _book_dir(project) / f"{_json_stem(project)}.json"
    if p.exists():
        if p.stat().st_size > 2:
            return p
        # File exists but is empty/near-empty — try sidecar recovery
        _try_recover_from_sidecars(project, p)
        return p if p.exists() else None
    # File missing — try sidecar recovery
    _try_recover_from_sidecars(project, p)
    return p if p.exists() else None


def _write_meta_to_json(project: dict) -> None:
    """Sync project metadata into the areas JSON top-level meta field."""
    book_dir = _book_dir(project)
    if not book_dir.exists():
        return
    json_path = book_dir / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.setdefault("book", project.get("title", ""))
    data["meta"] = {
        "title":      project.get("title", ""),
        "author":     project.get("author", ""),
        "year":       project.get("year", ""),
        "cover_path": project.get("cover_path", ""),
    }
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))


# ── Pipeline state ────────────────────────────────────────────────────────────

def pipeline_state(project: dict) -> dict:
    src_type     = _source_type(project)
    pages_dir    = _pages_dir(project)
    elements_dir = _elements_dir(project)

    pages    = sorted(
        p for p in (pages_dir.glob("page*.png") if pages_dir.exists() else [])
        if not p.name.endswith("-areas.png")
        and not p.name.endswith("-content.png")
        and not p.name.startswith(".")
    )
    overlays = list(pages_dir.glob("*-areas.png")) if pages_dir.exists() else []

    bj    = _book_json(project)

    area_pages = total_areas = ignored_pages = 0
    processed_pages = 0
    pending_area_pages = 0
    process_later_pending = 0
    table_pages = table_areas_total = table_areas_done = 0
    step2_done = False
    ps = []
    if bj:
        try:
            bd = json.loads(bj.read_text(encoding="utf-8"))
            ps = bd.get("pages", [])
            area_pages      = sum(1 for p in ps if p.get("areas"))
            total_areas     = sum(len(p.get("areas", [])) for p in ps)
            ignored_pages   = sum(1 for p in ps if p.get("ignored"))
            processed_pages = sum(1 for p in ps if p.get("page_dimensions"))
            step2_done      = processed_pages > 0
            # pending = non-ignored pages in JSON that haven't been processed yet
            pending_area_pages = sum(
                1 for p in ps
                if not p.get("ignored") and not p.get("page_dimensions")
            )
            process_later_pending = sum(
                1 for p in ps
                if not p.get("ignored")
                and p.get("process_later")
                and not (p.get("detected_by") or "").startswith("claudecode")
            )
            table_pages = sum(
                1 for p in ps
                if not p.get("ignored")
                and any(a.get("type") == "table" for a in p.get("areas", []))
            )
            table_areas_total = sum(
                1 for p in ps if not p.get("ignored")
                for a in p.get("areas", []) if a.get("type") == "table"
            )
            table_areas_done = sum(
                1 for p in ps if not p.get("ignored")
                for a in p.get("areas", []) if a.get("type") == "table" and a.get("table_html")
            )
        except Exception:
            pass

    has_elements = elements_dir.exists() and any(elements_dir.glob("*.png"))

    step6_done = False
    polished_pages = 0
    polished_count = 0
    json_active_pages = 0
    if ps:
        try:
            polished_pages    = sum(1 for p in ps if "page_join" in p)
            polished_count    = sum(1 for p in ps if p.get("polished"))
            json_active_pages = sum(1 for p in ps if not p.get("ignored"))
            polished_count    = max(polished_count, polished_pages)
            step6_done        = polished_count > 0
        except Exception:
            pass

    merged_html = _book_dir(project) / f"{_json_stem(project)}-merged.html"
    step7_done = merged_html.exists()

    return {
        "step1_done":    len(pages) > 0,
        "step2_done":    step2_done,
        "step3_done":    len(overlays) > 0,
        "step4_done":    has_elements,
        "step6_done":    step6_done,
        "step7_done":    step7_done,
        "page_count":    len(pages),
        "overlay_count": len(overlays),
        "area_pages":    area_pages,
        "total_areas":   total_areas,
        "ignored_pages":      ignored_pages,
        "pending_area_pages":    pending_area_pages,
        "process_later_pending": process_later_pending,
        "table_pages":           table_pages,
        "table_areas_total":     table_areas_total,
        "table_areas_done":      table_areas_done,
        "seamless_html_file": merged_html.name if step7_done else None,
        "polished_pages":    polished_pages,
        "polished_count":    polished_count,
        "json_active_pages": json_active_pages,
        "source_type":       src_type,
    }


# ── Background jobs ───────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _job_append(job: dict, line: str) -> None:
    job["lines"].append(line)


def _run_sequence(pid: str, commands: list[tuple[list[str], str]]) -> None:
    job = _jobs[pid]
    for cmd, label in commands:
        if job.get("cancelled"):
            _job_append(job, "⛔ Stopped by user")
            job["status"] = "error"
            return
        _job_append(job, f"=== {label} ===")
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(PROJECT_ROOT), env=env,
            )
            job["proc"] = proc
            for raw in proc.stdout:
                _job_append(job, raw.rstrip())
            proc.wait()
            job["proc"] = None
            if job.get("cancelled"):
                _job_append(job, "⛔ Stopped by user")
                job["status"] = "error"
                return
            if proc.returncode != 0:
                job["status"]     = "error"
                job["returncode"] = proc.returncode
                return
        except Exception as exc:
            _job_append(job, f"ERROR: {exc}")
            job["status"] = "error"
            return
    job["status"]     = "done"
    job["returncode"] = 0


def _run_step1(pid: str, src: Path, book_name: str | None = None, detect_content: bool = False) -> None:
    cmd = [sys.executable, "steps/step1_extract_pages.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    if detect_content:
        cmd += ["--detect-content"]
    _run_sequence(pid, [(cmd, "Step 1: Extract Pages")])


def _run_step2_with_step3(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    """Run step 2; visualization is done inline per page inside step2."""
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, "Step 2: Detect Areas & Visualise")])


def _run_redetect_opus(pid: str, src: Path, page_name: str, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--page", page_name, "--model", "anthropic/claude-opus-4-7", "--force"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, f"Re-detect {page_name} with Opus")])


def _run_redetect_sonnet(pid: str, src: Path, page_name: str, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--page", page_name, "--model", "anthropic/claude-sonnet-4-6", "--force"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, f"Re-detect {page_name} with Sonnet")])


def _run_process_later_opus(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--process-later-only"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, "Process Later pages with Opus")])


def _run_process_tables(pid: str, src: Path, book_name: str | None = None) -> None:
    cmd = [sys.executable, "-u", "steps/step5_process_tables.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    _run_sequence(pid, [(cmd, "Step 5: Process Tables")])



def _run_step6(pid: str, src: Path, book_name: str | None = None) -> None:
    cmd = [sys.executable, "-u", "steps/step6_polish_text.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    _run_sequence(pid, [(cmd, "Step 6: Cleanup Text")])


def _run_step6_resume(pid: str, src: Path, book_name: str | None = None) -> None:
    cmd = [sys.executable, "-u", "steps/step6_polish_text.py", str(src), "--resume"]
    if book_name:
        cmd += ["--book-name", book_name]
    _run_sequence(pid, [(cmd, "Step 6: Cleanup Text (resume)")])


def _merged_html(project: dict) -> Path:
    return _book_dir(project) / f"{_json_stem(project)}-merged.html"


def _run_step7(pid: str, project: dict) -> None:
    src = _source_path(project)
    merged_html = _merged_html(project)
    book_name = _json_stem(project)
    step4_cmd = [sys.executable, "steps/step4_extract_elements.py", str(src)]
    step7_cmd = [sys.executable, "-u", "steps/step7_seamless_html.py", str(src)]
    if book_name:
        step4_cmd += ["--book-name", book_name]
        step7_cmd += ["--book-name", book_name]
    _run_sequence(pid, [
        (step4_cmd, "Step 4: Extract Elements"),
        (step7_cmd, "Step 7: Build Seamless HTML"),
        ([sys.executable, "-u", "steps/postprocess_footnote_links.py", str(merged_html)],
         "Post-process: Link Footnotes"),
    ])


def _run_step8(pid: str, project: dict) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step8_export_epub.py", str(_merged_html(project))],
         "Step 8: Export to EPUB"),
    ])


def _run_step9(pid: str, project: dict) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step9_export_txt.py", str(_merged_html(project))],
         "Step 9: Export to TXT"),
    ])


def _start_job(pid: str, step: str, target, *args) -> None:
    _jobs[pid] = {"status": "running", "step": step, "lines": [], "returncode": None,
                  "cancelled": False, "proc": None}
    threading.Thread(target=target, args=(pid, *args), daemon=True).start()


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    data = _load_projects()
    projects_with_state = []
    for p in data["projects"]:
        try:
            state = pipeline_state(p)
        except Exception:
            state = {}
        projects_with_state.append({**p, "state": state})
    return render_template("dashboard.html", projects=projects_with_state)


@app.route("/projects/new", methods=["POST"])
def create_project():
    title       = request.form.get("title",       "").strip()
    author      = request.form.get("author",      "").strip()
    year        = request.form.get("year",        "").strip()
    source_path = _clean_path(request.form.get("source_path", ""))
    cover_path  = _clean_path(request.form.get("cover_path",  ""))
    if not title or not source_path:
        return redirect(url_for("dashboard"))
    src = Path(source_path)
    if src.is_dir():
        _image_exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
        has_images = any(
            f.is_file() and f.suffix.lower() in _image_exts
            for f in src.iterdir()
            if not f.name.startswith(".")
        )
        if not has_images:
            return redirect(url_for("dashboard"))
    detect_content = request.form.get("detect_content") == "1"
    styles        = request.form.get("styles") == "1"
    pid = str(uuid.uuid4())[:8]
    data = _load_projects()
    project = {
        "id":             pid,
        "title":          title,
        "author":         author,
        "year":           year,
        "source_path":    source_path,
        "cover_path":     cover_path,
        "detect_content": detect_content,
        "styles":         styles,
        "created_at":     datetime.utcnow().isoformat(),
    }
    data["projects"].append(project)
    _save_projects(data)
    _write_meta_to_json(project)
    return redirect(url_for("project_view", pid=pid))


@app.route("/projects/<pid>/edit", methods=["POST"])
def edit_project(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    data = _load_projects()
    for p in data["projects"]:
        if p["id"] == pid:
            p["title"]          = request.form.get("title",       "").strip() or p["title"]
            p["author"]         = request.form.get("author",      "").strip()
            p["year"]           = request.form.get("year",        "").strip()
            p["source_path"]    = _clean_path(request.form.get("source_path", "")) or p.get("source_path", "")
            p["cover_path"]     = _clean_path(request.form.get("cover_path",  ""))
            p["detect_content"] = request.form.get("detect_content") == "1"
            p["styles"]         = request.form.get("styles") == "1"
            break
    _save_projects(data)
    _write_meta_to_json(_get_project(pid))
    redirect_to = request.form.get("redirect", "dashboard")
    if redirect_to == "project":
        return redirect(url_for("project_view", pid=pid))
    return redirect(url_for("dashboard"))


@app.route("/projects/<pid>")
def project_view(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    state = pipeline_state(project)
    job   = _jobs.get(pid, {})
    return render_template("project.html", project=project, state=state, job=job)


@app.route("/projects/<pid>/delete", methods=["POST"])
def delete_project(pid: str):
    data = _load_projects()
    data["projects"] = [p for p in data["projects"] if p["id"] != pid]
    _save_projects(data)
    return redirect(url_for("dashboard"))


# ── Step runners ──────────────────────────────────────────────────────────────

@app.route("/projects/<pid>/run/step1", methods=["POST"])
def run_step1(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    detect_content = bool(project.get("detect_content", False))
    _start_job(pid, "step1", _run_step1, src, book_name, detect_content)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step2", methods=["POST"])
def run_step2(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "step2+3", _run_step2_with_step3, src, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-opus/<page_name>", methods=["POST"])
def run_redetect_opus(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "redetect-opus", _run_redetect_opus, src, page_name, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-sonnet/<page_name>", methods=["POST"])
def run_redetect_sonnet(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "redetect-sonnet", _run_redetect_sonnet, src, page_name, book_name, styles)
    return jsonify({"ok": True})



@app.route("/projects/<pid>/run/process-later-opus", methods=["POST"])
def run_process_later_opus(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "process-later-opus", _run_process_later_opus, src, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/process-tables", methods=["POST"])
def run_process_tables(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "process-tables", _run_process_tables, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step6", methods=["POST"])
def run_step6(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "step6", _run_step6, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step6-resume", methods=["POST"])
def run_step6_resume(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "step6", _run_step6_resume, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step7", methods=["POST"])
def run_step7(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step7", _run_step7, project)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step8", methods=["POST"])
def run_step8(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step8", _run_step8, project)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step9", methods=["POST"])
def run_step9(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step9", _run_step9, project)
    return jsonify({"ok": True})


# ── SSE job stream ────────────────────────────────────────────────────────────

@app.route("/projects/<pid>/stream")
def stream_job(pid: str):
    job = _jobs.get(pid)
    if not job:
        return Response(
            "data: [DONE status=idle]\n\n",
            mimetype="text/event-stream",
        )

    def generate():
        sent = 0
        while True:
            lines = job["lines"]
            while sent < len(lines):
                yield f"data: {lines[sent]}\n\n"
                sent += 1
            status = job.get("status")
            if status in ("done", "error") and sent >= len(job["lines"]):
                yield f"data: [DONE status={status}]\n\n"
                break
            time.sleep(0.15)
            yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/projects/<pid>/state")
def project_state_api(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    return jsonify(pipeline_state(project))


@app.route("/projects/<pid>/status")
def job_status(pid: str):
    job = _jobs.get(pid, {})
    return jsonify({
        "status":     job.get("status", "idle"),
        "step":       job.get("step", ""),
        "line_count": len(job.get("lines", [])),
    })


@app.route("/projects/<pid>/stop", methods=["POST"])
def stop_job(pid: str):
    job = _jobs.get(pid)
    if not job or job.get("status") != "running":
        return jsonify({"ok": False, "error": "no running job"})
    job["cancelled"] = True
    proc = job.get("proc")
    if proc:
        proc.kill()
    return jsonify({"ok": True})


# ── Area editor API ───────────────────────────────────────────────────────────

@app.route("/projects/<pid>/review")
def review(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    state = pipeline_state(project)
    return render_template("review.html", project=project, state=state)


@app.route("/projects/<pid>/api/book")
def api_book(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    folder = _book_dir(project)
    bj     = _book_json(project)

    bj_data: dict | None = None
    if bj:
        bj_data = json.loads(bj.read_text(encoding="utf-8"))
        ps = bj_data.get("pages", [])
        if ps:
            return jsonify({
                "book":      bj_data.get("book", project["title"]),
                "has_areas": any(p.get("page_dimensions") for p in ps),
                "pages": [
                    {
                        "name":          p["source_image"],
                        "prohibited":    p.get("prohibited", False),
                        "ignored":       p.get("ignored", False),
                        "process_later": p.get("process_later", False),
                        "detected_by":   p.get("detected_by", None),
                    }
                    for p in ps
                ],
            })

    # No areas yet — list raw page images; ignored flags come from JSON stubs
    ignored_set: set[str] = set()
    if bj_data:
        ignored_set = {p["source_image"] for p in bj_data.get("pages", []) if p.get("ignored")}

    pages_dir = _pages_dir(project)
    src_type  = _source_type(project)
    if not pages_dir.exists():
        if src_type == "folder":
            exts = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")
            pgs  = [f for ext in exts for f in sorted(folder.glob(ext))]
            if pgs:
                return jsonify({
                    "book":      project["title"],
                    "has_areas": False,
                    "pages": [
                        {"name": f.name, "prohibited": False, "ignored": f.name in ignored_set}
                        for f in pgs
                    ],
                })
        return jsonify({"error": "no pages"}), 404

    pgs = sorted(pages_dir.glob("page*.png"))
    return jsonify({
        "book":      project["title"],
        "has_areas": False,
        "pages": [
            {"name": f.name, "prohibited": False, "ignored": f.name in ignored_set}
            for f in pgs
        ],
    })


@app.route("/projects/<pid>/api/page/<page_name>")
def api_get_page(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    folder  = _book_dir(project)
    bj      = _book_json(project)
    bj_data = None

    if bj:
        bj_data = json.loads(bj.read_text(encoding="utf-8"))
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name and p.get("page_dimensions"):
                return jsonify(p)

    # Fallback: derive dimensions from the PNG itself
    img = _pages_dir(project) / page_name
    if not img.exists():
        img = folder / page_name  # image-folder projects
    if not img.exists():
        return jsonify({"error": "image not found"}), 404

    from PIL import Image as PilImage
    with PilImage.open(img) as im:
        w, h = im.size

    # Read ignored flag and content_bbox from any existing stub
    ignored = False
    process_later = False
    content_bbox = None
    if bj_data:
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name:
                ignored       = p.get("ignored", False)
                process_later = p.get("process_later", False)
                content_bbox  = p.get("content_bbox")
                break

    page_resp = {
        "source_image":    page_name,
        "ignored":         ignored,
        "process_later":   process_later,
        "page_dimensions": {"width": w, "height": h},
        "areas":           [],
    }
    if content_bbox:
        page_resp["content_bbox"] = content_bbox
    return jsonify(page_resp)


@app.route("/projects/<pid>/api/page/<page_name>", methods=["POST"])
def api_save_page(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no areas JSON — run detection first"}), 400
    data    = json.loads(bj.read_text(encoding="utf-8"))
    payload = request.get_json()
    for p in data["pages"]:
        if p["source_image"] == page_name:
            p["areas"] = payload["areas"]
            break
    _write_atomic(bj, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/content-bbox", methods=["POST"])
def api_save_content_bbox(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload = request.get_json()
    cb = payload.get("content_bbox")
    if not cb or not all(k in cb for k in ("left", "top", "right", "bottom")):
        return jsonify({"error": "invalid content_bbox"}), 400
    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    pages_map[page_name]["content_bbox"] = cb
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/process-later", methods=["POST"])
def api_toggle_process_later(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    entry = pages_map[page_name]
    entry["process_later"] = not entry.get("process_later", False)
    new_state = entry["process_later"]
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "process_later": new_state})


@app.route("/projects/<pid>/api/page/<page_name>/rotation", methods=["POST"])
def api_save_rotation(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload = request.get_json() or {}
    rotation = int(payload.get("rotation", 0)) % 360
    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    if rotation:
        pages_map[page_name]["rotation"] = rotation
    else:
        pages_map[page_name].pop("rotation", None)
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/ignore", methods=["POST"])
def api_toggle_ignore(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    entry = pages_map[page_name]
    entry["ignored"] = not entry.get("ignored", False)
    new_state = entry["ignored"]
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "ignored": new_state})


@app.route("/projects/<pid>/cover")
def serve_cover(pid: str):
    project = _get_project(pid)
    if not project or not project.get("cover_path"):
        return "not found", 404
    p = Path(project["cover_path"])
    if not p.exists():
        return "not found", 404
    return send_file(p)


@app.route("/projects/<pid>/images/<path:filename>")
def serve_image(pid: str, filename: str):
    project = _get_project(pid)
    if not project:
        return "not found", 404
    p = _pages_dir(project) / filename
    if not p.exists():
        p = _book_dir(project) / filename  # image-folder projects
    if not p.exists():
        return "not found", 404
    return send_file(p)


@app.route("/projects/<pid>/elements/<path:filename>")
def serve_element(pid: str, filename: str):
    project = _get_project(pid)
    if not project:
        return "not found", 404
    p = _elements_dir(project) / filename
    if not p.exists():
        return "not found", 404
    return send_file(p)


def _serve_html_with_rewritten_elements(html_path: Path, pid: str, elements_name: str) -> Response:
    content = html_path.read_text(encoding="utf-8")
    nfc_name = unicodedata.normalize("NFC", elements_name)
    content = content.replace(
        f'src="{nfc_name}/', f'src="/projects/{pid}/elements/'
    )
    return Response(content, mimetype="text/html")



@app.route("/projects/<pid>/preview-seamless")
def preview_seamless_html(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    html_path = _merged_html(project)
    if not html_path.exists():
        return "No seamless HTML generated yet", 404
    return _serve_html_with_rewritten_elements(
        html_path, pid, _elements_dir(project).name
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)
