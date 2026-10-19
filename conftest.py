import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

import pytest


@pytest.fixture
def source_subprocess_imports(monkeypatch):
    """Keep test child processes on this checkout, not a stale editable install."""
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
    inherited = os.environ.get('PYTHONPATH')
    monkeypatch.setenv('PYTHONPATH', source + (os.pathsep + inherited if inherited else ''))
