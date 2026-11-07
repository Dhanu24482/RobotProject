"""Runs the browser-side picture renderer's checks under Node (skipped where Node is not installed)."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is not installed')
def test_board_js_checks_pass():
    done = subprocess.run(['node', os.path.join(HERE, 'board_check.js')], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'checks passed' in done.stdout
