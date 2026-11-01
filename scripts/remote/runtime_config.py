"""Read the same bundled, non-secret stack settings on a configured server."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
STACK = json.loads((BASE / 'config/stack.json').read_text(encoding='utf-8'))
APP = Path(STACK['paths']['application'])
SHARED = Path(STACK['paths']['shared'])
