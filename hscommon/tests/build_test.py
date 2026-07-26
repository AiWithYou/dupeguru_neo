# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import pytest

from hscommon import build


def test_print_and_do_passes_an_argument_vector_without_a_shell(monkeypatch):
    observed = {}

    class Process:
        def wait(self):
            return 7

    def fake_popen(arguments, *, shell):
        observed["arguments"] = arguments
        observed["shell"] = shell
        return Process()

    monkeypatch.setattr(build, "Popen", fake_popen)

    result = build.print_and_do(["tool", "argument with spaces", "$(must-not-run)"])

    assert result == 7
    assert observed == {
        "arguments": ["tool", "argument with spaces", "$(must-not-run)"],
        "shell": False,
    }


@pytest.mark.parametrize("command", ["tool --flag", b"tool --flag", []])
def test_print_and_do_rejects_shell_command_strings_and_empty_vectors(command):
    with pytest.raises(TypeError, match="argument sequence"):
        build.print_and_do(command)
