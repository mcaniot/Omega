"""Compatibility launcher used by legacy Python callers and behavioral tests."""
import os
from pathlib import Path
import sys

binary = str(Path(__file__).with_name('run'))
os.execv(binary, [binary, *sys.argv[1:]])
