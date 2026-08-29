import sys
import os
import subprocess
from pathlib import Path

# Relaunch inside the virtual environment if run with global python
venv_python = Path(__file__).parent / ".venv" / "Scripts" / "python.exe"
if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
    result = subprocess.run([str(venv_python)] + sys.argv)
    sys.exit(result.returncode)

from engine.health import run_health_checks

# 1. Run validation checks before importing Flask/app to prevent startup tracebacks
all_ok, report, remediations = run_health_checks()
print(report)

if not all_ok:
    print("\n[CRITICAL ERROR] Startup validation failed. Please resolve the following:")
    for rem in remediations:
        print(f"  - {rem}")
    print("\nAborting startup.\n")
    sys.exit(1)

# 2. Import app and start only if health check passed
from dashboard.app import app

if __name__ == "__main__":
    os.environ["FLASK_ENV"] = "development"

    # Tell Flask's watchdog reloader to ONLY watch source directories.
    # This prevents hot-reloads from killing the Playwright browser mid-render
    # when test scripts, generated files, or venv internals change.
    project_root = os.path.dirname(os.path.abspath(__file__))
    watch_dirs = [
        os.path.join(project_root, "dashboard"),
        os.path.join(project_root, "engine"),
        os.path.join(project_root, "services"),
        os.path.join(project_root, "agents"),
        os.path.join(project_root, "groups"),
        os.path.join(project_root, "design_templates"),
    ]
    # Only watch dirs that actually exist
    extra_files = [d for d in watch_dirs if os.path.isdir(d)]

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        extra_files=extra_files,
        use_reloader=True,
        reloader_type="watchdog",
        exclude_patterns=[
            "*/.venv/*",
            "*/generated/*",
            "*/test_*.py",
            "*/patch_*.py",
            "*/__pycache__/*",
            "*.log",
        ]
    )

