"""Current main frontend acceptance entry point; all platforms are simulated.

The historical v1.1 four-column/address script is retained as
legacy_smoke_frontend_v110.py. Main v1.3 removed address submission.
"""
import subprocess
import sys
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    for name in ('smoke_core_frontend.py', 'smoke_v130.py', 'smoke_v130_fill.py'):
        subprocess.run([sys.executable, str(root / 'tests' / name)], cwd=root, check=True)
    print('PASS: current main frontend acceptance (large grids, two clients, native fill gestures).')
