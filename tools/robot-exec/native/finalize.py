"""Use the original diagnostic implementation after native execution."""
import importlib.util
import json
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('original', Path(__file__).with_name('run-original.py'))
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
directory = Path(sys.argv[1])
inline_bytes = int(sys.argv[2])
metadata_path = Path(sys.argv[4]) if len(sys.argv) > 4 else directory / 'result.json'
metadata = json.loads(metadata_path.read_text())


class PinnedStreams:
    def __truediv__(self, name):
        return Path(sys.argv[5] if name == 'stdout.log' else sys.argv[6])


if len(sys.argv) > 6:
    directory = PinnedStreams()
result = {key: metadata[key] for key in ('status', 'exit_code', 'duration_seconds', 'bytes')}
code = metadata['exit_code']
if metadata['signal']:
    result['signal'] = metadata['signal']
result['evidence'] = sys.argv[3] if len(sys.argv) > 3 else str(directory)
if sum(metadata['bytes'].values()) <= inline_bytes:
    for stream in ('stdout', 'stderr'):
        text = original.ANSI.sub('', (directory / (stream + '.log')).read_bytes().decode('utf-8', 'replace'))
        if text:
            result[stream] = text
    result['diagnostics'] = []
if len(original.encoded(result)) > 4000 or 'diagnostics' not in result:
    result.pop('stdout', None)
    result.pop('stderr', None)
    result['diagnostics'] = original.excerpts(directory) if code else []
while len(original.encoded(result)) > 4000 and result['diagnostics']:
    result['diagnostics'].pop()
result['diagnostics'].sort(key=lambda item: (item['stream'], item['line']))
print(original.encoded(result).decode())
sys.exit(code)
