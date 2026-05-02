#!/usr/bin/env python3
"""Unified Flask app for the PDF modernisation pipeline."""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, stream_with_context, url_for)

PROJECT_ROOT = Path(__file__).parent
PROJECTS_FILE = PROJECT_ROOT / "projects.json"
app = Flask(__name__)


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
    PROJECTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
    stem   = _source_path(project).stem
    canonical = folder / f"{stem} - elements"
    if canonical.exists():
        return canonical
    legacy = folder / "elements"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step4 will make it here


def _pages_dir(project: dict) -> Path:
    """Return the pages directory for a project.

    New layout: ``<book_dir>/<stem> - pages/``
    Legacy fallback: ``<book_dir>/pages/`` (existing data before this convention).
    For image-folder projects the folder itself contains the images.
    """
    if _source_type(project) == "folder":
        return _book_dir(project)
    folder   = _book_dir(project)
    stem     = _source_path(project).stem
    canonical = folder / f"{stem} - pages"
    if canonical.exists():
        return canonical
    legacy = folder / "pages"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step1 will make it here


def _book_json(project: dict) -> Path | None:
    p = _book_dir(project) / f"{_json_stem(project)}.json"
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
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Pipeline state ────────────────────────────────────────────────────────────

def pipeline_state(project: dict) -> dict:
    folder       = _book_dir(project)
    src_type     = _source_type(project)
    pages_dir    = _pages_dir(project)
    elements_dir = _elements_dir(project)

    pages    = sorted(
        p for p in (pages_dir.glob("page*.png") if pages_dir.exists() else [])
        if not p.name.endswith("-areas.png") and not p.name.startswith(".")
    )
    overlays = list(pages_dir.glob("*-areas.png")) if pages_dir.exists() else []

    # For image-folder projects, raw images in the folder count as pages if pages/ is empty
    if src_type == "folder" and not pages:
        exts = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")
        pages = [f for ext in exts for f in sorted(folder.glob(ext))]

    bj    = _book_json(project)

    area_pages = total_areas = ignored_pages = 0
    processed_pages = 0
    step2_done = False
    if bj:
        try:
            bd = json.loads(bj.read_text(encoding="utf-8"))
            ps = bd.get("pages", [])
            area_pages      = sum(1 for p in ps if p.get("areas"))
            total_areas     = sum(len(p.get("areas", [])) for p in ps)
            ignored_pages   = sum(1 for p in ps if p.get("ignored"))
            processed_pages = sum(1 for p in ps if p.get("page_dimensions"))
            step2_done      = processed_pages > 0
        except Exception:
            pass

    ignored_no_detect = 0
    if bj:
        try:
            ignored_no_detect = sum(
                1 for p in bd.get("pages", [])
                if p.get("ignored") and not p.get("page_dimensions")
            )
        except Exception:
            pass
    pending_area_pages = max(0, len(pages) - processed_pages - ignored_no_detect)

    has_elements = elements_dir.exists() and any(elements_dir.glob("*.png"))

    step6_done = False
    polished_pages = 0
    polished_count = 0
    json_active_pages = 0
    if bj:
        try:
            ps_all = bd.get("pages", [])
            polished_pages    = sum(1 for p in ps_all if "page_join" in p)
            polished_count    = sum(1 for p in ps_all if p.get("polished"))
            json_active_pages = sum(1 for p in ps_all if not p.get("ignored"))
            # effective: whichever indicator is higher (page_join covers pre-flag runs)
            polished_count    = max(polished_count, polished_pages)
            step6_done        = polished_count > 0
        except Exception:
            pass

    seamless_htmls = list(folder.glob("*-merged.html"))
    step7_done = len(seamless_htmls) > 0

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
        "pending_area_pages": pending_area_pages,
        "seamless_html_file": seamless_htmls[0].name if seamless_htmls else None,
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


def _run_step1(pid: str, src: Path) -> None:
    _run_sequence(pid, [
        ([sys.executable, "steps/step1_extract_pages.py", str(src)], "Step 1: Extract Pages"),
    ])


def _run_step2_with_step3(pid: str, src: Path) -> None:
    """Run step 2; visualization is done inline per page inside step2."""
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step2_detect_areas.py", str(src)],
         "Step 2: Detect Areas & Visualise"),
    ])


def _run_redetect_opus(pid: str, src: Path, page_name: str) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
          "--page", page_name, "--model", "anthropic/claude-opus-4-7", "--force"],
         f"Re-detect {page_name} with Opus"),
    ])



def _run_step6(pid: str, src: Path) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step6_polish_text.py", str(src)], "Step 6: Cleanup Text"),
    ])


def _run_step6_resume(pid: str, src: Path) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step6_polish_text.py", str(src), "--resume"],
         "Step 6: Cleanup Text (resume)"),
    ])


def _merged_html(project: dict) -> Path:
    return _book_dir(project) / f"{_json_stem(project)}-merged.html"


def _run_step7(pid: str, project: dict) -> None:
    src = _source_path(project)
    merged_html = _merged_html(project)
    _run_sequence(pid, [
        ([sys.executable, "steps/step4_extract_elements.py", str(src)], "Step 4: Extract Elements"),
        ([sys.executable, "-u", "steps/step7_seamless_html.py", str(src)],
         "Step 7: Build Seamless HTML"),
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
    source_path = request.form.get("source_path", "").strip()
    cover_path  = request.form.get("cover_path",  "").strip()
    if not title or not source_path:
        return redirect(url_for("dashboard"))
    pid = str(uuid.uuid4())[:8]
    data = _load_projects()
    project = {
        "id":          pid,
        "title":       title,
        "author":      author,
        "year":        year,
        "source_path": source_path,
        "cover_path":  cover_path,
        "created_at":  datetime.utcnow().isoformat(),
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
            p["title"]       = request.form.get("title",       "").strip() or p["title"]
            p["author"]      = request.form.get("author",      "").strip()
            p["year"]        = request.form.get("year",        "").strip()
            p["source_path"] = request.form.get("source_path", "").strip() or p.get("source_path", "")
            p["cover_path"]  = request.form.get("cover_path",  "").strip()
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
    _start_job(pid, "step1", _run_step1, _source_path(project))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step2", methods=["POST"])
def run_step2(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step2+3", _run_step2_with_step3, _source_path(project))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-opus/<page_name>", methods=["POST"])
def run_redetect_opus(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "redetect-opus", _run_redetect_opus, _source_path(project), page_name)
    return jsonify({"ok": True})



@app.route("/projects/<pid>/run/step6", methods=["POST"])
def run_step6(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step6", _run_step6, _source_path(project))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step6-resume", methods=["POST"])
def run_step6_resume(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step6", _run_step6_resume, _source_path(project))
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
        if any(p.get("page_dimensions") for p in ps):
            return jsonify({
                "book":      bj_data.get("book", project["title"]),
                "has_areas": True,
                "pages": [
                    {
                        "name":       p["source_image"],
                        "prohibited": p.get("prohibited", False),
                        "ignored":    p.get("ignored", False),
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

    # Read ignored flag from any existing stub
    ignored = False
    if bj_data:
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name:
                ignored = p.get("ignored", False)
                break

    return jsonify({
        "source_image":    page_name,
        "ignored":         ignored,
        "page_dimensions": {"width": w, "height": h},
        "areas":           [],
    })


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
    bj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    content = content.replace(
        f'src="{elements_name}/', f'src="/projects/{pid}/elements/'
    )
    return Response(content, mimetype="text/html")



@app.route("/projects/<pid>/preview-seamless")
def preview_seamless_html(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    htmls = list(_book_dir(project).glob("*-merged.html"))
    if not htmls:
        return "No seamless HTML generated yet", 404
    return _serve_html_with_rewritten_elements(
        htmls[0], pid, _elements_dir(project).name
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)
