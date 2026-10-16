"""Cross-platform local launcher: python scripts/start.py --demo."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description='栖木 ERP 本地启动器')
    parser.add_argument('--demo', action='store_true', help='初始化虚构演示数据；已有数据时不覆盖')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('请使用 Python 3.12 或更新版本。')
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1—65535 之间。')
    os.chdir(ROOT)
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('创建项目独立 Python 环境…', flush=True)
        venv.create(ROOT / '.venv', with_pip=True)
    digest = hashlib.sha256((ROOT / 'requirements.txt').read_bytes()).hexdigest()
    marker = ROOT / '.venv/.requirements.sha256'
    if not marker.exists() or marker.read_text() != digest:
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt'], check=True)
        marker.write_text(digest)
    env = os.environ.copy()
    env.setdefault('DJANGO_DEBUG', 'true')
    env['PYTHONUTF8'] = '1'
    for command in [['migrate', '--noinput'], ['collectstatic', '--noinput', '--verbosity', '0']]:
        subprocess.run([str(python), 'manage.py', *command], check=True, env=env)
    if args.demo:
        subprocess.run([str(python), 'manage.py', 'seed_demo', '--credentials-file', '.demo-credentials.txt'], check=True, env=env)
        print('演示账号：demo_admin。首次生成的密码保存在 .demo-credentials.txt（请勿提交）。', flush=True)
    print(f'打开 http://127.0.0.1:{args.port} ；按 Ctrl+C 停止。', flush=True)
    subprocess.run([str(python), 'manage.py', 'runserver', f'127.0.0.1:{args.port}'], env=env, check=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
