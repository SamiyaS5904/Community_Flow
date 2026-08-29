import os
import sys
import subprocess
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from engine.config import config

def install_chromium():
    print(f"Forcing PLAYWRIGHT_BROWSERS_PATH to: {os.environ['PLAYWRIGHT_BROWSERS_PATH']}")
    print("Installing Chromium browser...")
    
    # Run the installation
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    result = subprocess.run(cmd, env=os.environ)
    if result.returncode == 0:
        print("Chromium installed successfully inside project directory.")
    else:
        print(f"Installation failed with exit code: {result.returncode}")
        sys.exit(result.returncode)

if __name__ == "__main__":
    install_chromium()
