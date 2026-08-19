from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
PYTHON = VENV_DIR / "Scripts" / "python.exe"


def run(command: list[str], *, quiet: bool = False) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=False,
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)


def ensure_runtime() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("91Fetch requires Python 3.11 or newer.")
    if not PYTHON.exists():
        print("Creating the local 91Fetch environment...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        run([str(PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(PYTHON), "-m", "pip", "install", "-e", str(ROOT)])
    try:
        run([str(PYTHON), "-c", "import fastapi, uvicorn, viewkey_batch"], quiet=True)
    except subprocess.CalledProcessError:
        run([str(PYTHON), "-m", "pip", "install", "-e", str(ROOT)])
    site_config = ROOT / "site.json"
    if not site_config.exists():
        shutil.copyfile(ROOT / "site.example.json", site_config)


def main() -> int:
    try:
        ensure_runtime()
        if os.getenv("VIEWKEY_LAUNCHER_CHECK") == "1":
            print(f"91Fetch path check passed: {ROOT}")
            return 0
        print("Starting 91Fetch. The web interface will open automatically...")
        return subprocess.call([str(PYTHON), "-m", "viewkey_batch.web"], cwd=ROOT, env=os.environ.copy())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"91Fetch failed to start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
