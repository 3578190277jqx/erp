"""Waitress WSGI entry point; shares proxy handling with the container startup."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')


def main():
    from waitress import serve
    from config.wsgi import application
    proxy = os.getenv('DJANGO_TRUSTED_PROXY', '').strip()
    options = {'clear_untrusted_proxy_headers': True}
    if proxy:
        from ipaddress import ip_address
        ip_address(proxy)  # Explicit peer IP only: do not accept a wildcard trust boundary.
        options.update(trusted_proxy=proxy, trusted_proxy_count=1,
                       trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'})
    serve(application, host=os.getenv('BIND_HOST', '0.0.0.0'), port=int(os.getenv('PORT', '8000')), threads=4, **options)


if __name__ == '__main__':
    main()
