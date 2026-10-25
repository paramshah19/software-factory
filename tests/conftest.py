import pytest


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path_factory, monkeypatch):
    """Tests never touch the owner's ~/.software-factory (config, key store): the engine is Claude Code unless a test says otherwise."""
    home = tmp_path_factory.mktemp("sfhome")
    monkeypatch.setenv("SF_HOME", str(home))
    monkeypatch.setenv("SF_CONFIG", str(home / "config.json"))


@pytest.fixture(autouse=True)
def _no_real_windows(monkeypatch):
    """No test may open a browser tab or an app window on the owner's screen."""
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)
