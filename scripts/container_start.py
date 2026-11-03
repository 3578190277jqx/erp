import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for command in [['migrate', '--noinput'], ['collectstatic', '--noinput']]:
    subprocess.run([sys.executable, 'manage.py', *command], check=True)
from scripts.serve import main
main()
