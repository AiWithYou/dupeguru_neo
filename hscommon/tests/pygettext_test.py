from hscommon import pygettext


def test_main_preserves_unicode_message_ids(tmp_path):
    source_path = tmp_path / "unicode_messages.py"
    output_path = tmp_path / "messages.pot"
    source_path.write_text('tr("Find Similar Image… · 640×480 🙂")\n', encoding="utf-8")

    pygettext.main([str(source_path)], outpath=output_path, keywords=["tr"])

    output = output_path.read_text(encoding="utf-8")
    assert 'msgid "Find Similar Image… · 640×480 🙂"' in output


def test_make_escapes_is_repeatable_and_escapes_po_syntax():
    pygettext.make_escapes(False)
    first = pygettext.escape('quote: " slash: \\ newline:\n café')
    pygettext.make_escapes(False)
    second = pygettext.escape('quote: " slash: \\ newline:\n café')

    assert first == second
    assert first == 'quote: \\" slash: \\\\ newline:\\n café'
