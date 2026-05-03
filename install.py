#!/usr/bin/env python3
"""
Setup script for PDF Modernization Tool.
Run this once after downloading the project to get everything ready.

Usage:
    python install.py
    python3 install.py
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


# ── helpers ────────────────────────────────────────────────────────────────────

def say(text=""):
    print(text, flush=True)

def step(text):
    print(f"\n{'─' * 50}\n  {text}\n{'─' * 50}", flush=True)

def success(text):
    print(f"  ✓  {text}", flush=True)

def error(text):
    print(f"\n  ERROR: {text}\n", file=sys.stderr, flush=True)

def ask(prompt, *, hidden=False) -> str:
    if hidden:
        import getpass
        return getpass.getpass(f"  → {prompt}: ").strip()
    return input(f"  → {prompt}: ").strip()


# ── checks ─────────────────────────────────────────────────────────────────────

def check_python_version():
    if sys.version_info < (3, 9):
        error(
            f"Python 3.9 or newer is required. "
            f"You have {sys.version_info.major}.{sys.version_info.minor}.\n"
            "  Download the latest Python from https://www.python.org/downloads/"
        )
        sys.exit(1)
    success(f"Python {sys.version_info.major}.{sys.version_info.minor} detected")


# ── virtual environment ────────────────────────────────────────────────────────

def get_venv_python() -> Path:
    venv_dir = HERE / ".venv"
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_venv():
    venv_dir = HERE / ".venv"
    if venv_dir.exists():
        success("Virtual environment already exists — skipping creation")
        return

    say("  Creating virtual environment (.venv) ...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error(f"Could not create virtual environment:\n{result.stderr}")
        sys.exit(1)
    success("Virtual environment created")


def install_dependencies():
    req_file = HERE / "requirements.txt"
    if not req_file.exists():
        error("requirements.txt not found. Make sure you downloaded the full project.")
        sys.exit(1)

    say("  Installing dependencies (this may take a minute) ...")
    venv_python = get_venv_python()
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        capture_output=True, text=True,
    )
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "-r", str(req_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error(f"Dependency installation failed:\n{result.stderr}")
        sys.exit(1)
    success("All dependencies installed")


# ── .env setup ─────────────────────────────────────────────────────────────────

def setup_env():
    env_path = HERE / ".env"

    if env_path.exists():
        say()
        say("  An .env file already exists.")
        answer = ask("  Overwrite it with new credentials? (yes/no)", hidden=False)
        if answer.lower() not in ("yes", "y"):
            success("Keeping existing .env file")
            return

    say()
    say("  You will need an OpenRouter API key.")
    say("  Get one for free at: https://openrouter.ai/settings/keys")
    say()

    while True:
        api_key = ask("OpenRouter API key")
        if api_key:
            break
        say("  API key cannot be empty. Please try again.")

    env_path.write_text(f"OPEN_ROUTER_APIKEY={api_key}\n", encoding="utf-8")
    success(f".env file created at {env_path}")


# ── start script ───────────────────────────────────────────────────────────────

def create_start_script():
    if sys.platform == "win32":
        script_path = HERE / "start.bat"
        script_path.write_text(
            "@echo off\n"
            "call .venv\\Scripts\\activate\n"
            "echo.\n"
            "echo   Starting PDF Modernization Tool...\n"
            "echo   Open your browser at: http://localhost:5000\n"
            "echo.\n"
            "python app.py\n",
            encoding="utf-8",
        )
        success(f"start.bat created — double-click it to launch the app")
        return script_path

    script_path = HERE / "start.sh"
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")"\n'
        "source .venv/bin/activate\n"
        'echo ""\n'
        'echo "  Starting PDF Modernization Tool..."\n'
        'echo "  Open your browser at: http://localhost:5000"\n'
        'echo ""\n'
        "python app.py\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    success(f"start.sh created")
    return script_path


# ── launch instructions ────────────────────────────────────────────────────────

def print_launch_instructions():
    run_hint = "double-click start.bat" if sys.platform == "win32" else "run ./start.sh in your terminal"

    say()
    say("╔══════════════════════════════════════════════════════╗")
    say("║            Setup complete!  Ready to run.            ║")
    say("╚══════════════════════════════════════════════════════╝")
    say()
    say(f"  To start the app: {run_hint}")
    say()
    say("  Then open your browser at:  http://localhost:5000")
    say()


# ── already-installed check ────────────────────────────────────────────────────

def is_already_installed() -> bool:
    start_script = HERE / ("start.bat" if sys.platform == "win32" else "start.sh")
    return (
        (HERE / ".venv").exists()
        and (HERE / ".env").exists()
        and start_script.exists()
    )


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    say()
    say("  PDF Modernization Tool — Setup")
    say("  ================================")

    if is_already_installed():
        say()
        say("  Everything is already set up.")
        print_launch_instructions()
        return

    step("1 / 5  Checking Python version")
    check_python_version()

    step("2 / 5  Setting up virtual environment")
    create_venv()

    step("3 / 5  Installing dependencies")
    install_dependencies()

    step("4 / 5  Configuring API credentials")
    setup_env()

    step("5 / 5  Creating start script")
    create_start_script()

    print_launch_instructions()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say()
        say("  Setup cancelled.")
        sys.exit(0)
