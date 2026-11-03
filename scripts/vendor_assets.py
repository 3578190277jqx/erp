"""Copy only Tabler's core CSS from its fixed npm distribution; no CDN dependency."""
import argparse
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument('archive', type=Path, help='tabler-core-1.6.1.tgz produced by npm pack @tabler/core@1.6.1')
args = parser.parse_args()
with tarfile.open(args.archive) as archive:
    css = archive.extractfile('package/dist/css/tabler.min.css').read().decode('utf-8')
css = re.sub(r'/\*# sourceMappingURL=.*?\*/', '', css)
css = re.sub(r'@import\s+url\([^;]+;', '', css)
destination = ROOT / 'erp/static/vendor'
destination.mkdir(parents=True, exist_ok=True)
(destination / 'tabler.min.css').write_text(css, encoding='utf-8')
print(f'Vendored Tabler 1.6.1 CSS: {len(css):,} characters; third-party license in TABLER-LICENSE.txt')
