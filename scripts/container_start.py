import os
import subprocess
import sys

for command in [['migrate', '--noinput'], ['collectstatic', '--noinput']]:
    subprocess.run([sys.executable, 'manage.py', *command], check=True)
from waitress import serve
from config.wsgi import application

serve(application, host='0.0.0.0', port=int(os.getenv('PORT', '8000')), threads=4)
