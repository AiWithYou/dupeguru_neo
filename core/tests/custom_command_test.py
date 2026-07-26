from types import SimpleNamespace

import pytest

from core import app as app_module
from core.app import parse_custom_command
from core.tests.base import GetTestGroups, TestApp


def test_custom_command_substitutes_paths_as_single_arguments():
    argv = parse_custom_command(
        'viewer --dupe "%d" --reference "%r"',
        "/library/file with spaces.jpg",
        "/reference/ref image.jpg",
        windows=False,
    )
    assert argv == [
        "viewer",
        "--dupe",
        "/library/file with spaces.jpg",
        "--reference",
        "/reference/ref image.jpg",
    ]


def test_custom_command_filename_metacharacters_are_not_shell_syntax():
    argv = parse_custom_command(
        'viewer "%d"',
        "photo.jpg; touch owned.txt",
        "reference.jpg",
        windows=False,
    )
    assert argv == ["viewer", "photo.jpg; touch owned.txt"]


def test_custom_command_windows_quoted_executable():
    argv = parse_custom_command(
        r'"C:\Program Files\Viewer\viewer.exe" --input "%d"',
        r"C:\Library\my image.jpg",
        r"C:\Library\reference.jpg",
        windows=True,
    )
    assert argv == [
        r"C:\Program Files\Viewer\viewer.exe",
        "--input",
        r"C:\Library\my image.jpg",
    ]


def test_custom_command_rejects_unbalanced_quotes():
    with pytest.raises(ValueError):
        parse_custom_command('viewer "%d', "image.jpg", "reference.jpg", windows=False)


def test_custom_command_rejects_nul_after_substitution():
    with pytest.raises(ValueError):
        parse_custom_command("viewer %d", "bad\0name.jpg", "reference.jpg", windows=False)


def _app_with_selected_duplicate(monkeypatch):
    test_app = TestApp().app
    _objects, _matches, groups = GetTestGroups()
    test_app.results.groups = groups
    selected = groups[0].dupes[0]
    test_app.selected_dupes = [selected]
    monkeypatch.setattr(test_app.view, "get_default", lambda _name: "viewer %d %r")
    return test_app


def test_custom_command_requires_an_explicit_external_safety_confirmation(monkeypatch):
    test_app = _app_with_selected_duplicate(monkeypatch)
    prompts = []
    monkeypatch.setattr(
        test_app.view,
        "ask_yes_no",
        lambda prompt: prompts.append(prompt) or False,
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("external process must not start after a declined warning")

    monkeypatch.setattr(app_module.subprocess, "run", must_not_run)

    test_app.invoke_custom_command()

    assert len(prompts) == 1
    assert "outside dupeGuru's safety model" in prompts[0]
    assert "permanently delete" in prompts[0]


def test_custom_command_discards_unbounded_child_output(monkeypatch):
    test_app = _app_with_selected_duplicate(monkeypatch)
    calls = []

    def record_run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(app_module.subprocess, "run", record_run)

    test_app.invoke_custom_command()

    assert len(calls) == 1
    _argv, options = calls[0]
    assert options["shell"] is False
    assert options["stdout"] is app_module.subprocess.DEVNULL
    assert options["stderr"] is app_module.subprocess.DEVNULL
