# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import os

from hscommon import loc


def test_generate_pot_merge_closes_and_removes_its_temporary_file(tmp_path, monkeypatch):
    source_directory = tmp_path / "source"
    source_directory.mkdir()
    source_directory.joinpath("messages.py").write_text('tr("New message")\n', encoding="utf-8")
    output_path = tmp_path / "messages.pot"
    output_path.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=utf-8\\n"\n',
        encoding="utf-8",
    )
    temporary_path = tmp_path / "generated.pot"

    def create_temporary_file():
        return os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_RDWR), str(temporary_path)

    merge_calls = []

    def record_merge(source, destination):
        merge_calls.append((source, destination))

    monkeypatch.setattr(loc.tempfile, "mkstemp", create_temporary_file)
    monkeypatch.setattr(loc, "merge_po_and_preserve", record_merge)

    loc.generate_pot([str(source_directory)], str(output_path), ["tr"], merge=True)

    assert not temporary_path.exists()
    assert merge_calls == [(str(temporary_path), str(output_path))]
