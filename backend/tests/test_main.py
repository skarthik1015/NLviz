from __future__ import annotations

import pytest

from app import main as app_main


def test_runtime_dependency_check_raises_when_multipart_missing(monkeypatch):
    def _raise(_name: str):
        raise ModuleNotFoundError("missing multipart")

    monkeypatch.setattr(app_main.importlib, "import_module", _raise)

    with pytest.raises(RuntimeError, match="python-multipart"):
        app_main._ensure_runtime_dependencies()
