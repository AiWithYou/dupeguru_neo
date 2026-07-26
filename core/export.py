# Created By: Virgil Dupras
# Created On: 2006/09/16
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import csv
import html
import os
import os.path as op
import tempfile
from pathlib import Path
from tempfile import mkdtemp

# Yes, this is a very low-tech solution, but at least it doesn't have all these annoying dependency
# and resource problems.

MAIN_TEMPLATE = """
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Strict//EN' 'http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd'>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta content="text/html; charset=utf-8" http-equiv="Content-Type"/>
        <title>dupeGuru Results</title>
        <style type="text/css">
BODY
{
    background-color:white;
}

BODY,A,P,UL,TABLE,TR,TD
{
    font-family:Tahoma,Arial,sans-serif;
    font-size:10pt;
    color: #4477AA;
}

TABLE
{
    background-color: #225588;
    margin-left: auto;
    margin-right: auto;
    width: 90%;
}

TR
{
    background-color: white;
}

TH
{
    font-weight: bold;
    color: black;
    background-color: #C8D6E5;
}

TH TD
{
    color:black;
}

TD
{
    padding-left: 2pt;
}

TD.rightelem
{
    text-align:right;
    /*padding-left:0pt;*/
    padding-right: 2pt;
    width: 17%;
}

TD.indented
{
    padding-left: 12pt;
}

H1
{
    font-family:&quot;Courier New&quot;,monospace;
    color:#6699CC;
    font-size:18pt;
    color:#6da500;
    border-color: #70A0CF;
    border-width: 1pt;
    border-style: solid;
    margin-top:   16pt;
    margin-left:  5%;
    margin-right: 5%;
    padding-top:  2pt;
    padding-bottom:2pt;
    text-align:   center;
}
</style>
</head>
<body>
<h1>dupeGuru Results</h1>
<table>
<tr>$colheaders</tr>
$rows
</table>
</body>
</html>
"""

COLHEADERS_TEMPLATE = "<th>{name}</th>"

ROW_TEMPLATE = """
<tr>
    <td class="{indented}">{filename}</td>{cells}
</tr>
"""

CELL_TEMPLATE = """<td>{value}</td>"""

_SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SPREADSHEET_LEADING_WHITESPACE = " \t\r\n"


def _unicode_scalar_text(value):
    """Return text which is valid in UTF-8 and XML 1.0."""

    text = str(value)
    return "".join(
        (
            character
            if (
                character in "\t\n\r"
                or "\x20" <= character <= "\ud7ff"
                or "\ue000" <= character <= "\ufffd"
                or "\U00010000" <= character <= "\U0010ffff"
            )
            else "\ufffd"
        )
        for character in text
    )


def _xhtml_text(value):
    return html.escape(_unicode_scalar_text(value), quote=True)


def _spreadsheet_safe_cell(value):
    """Neutralize text which spreadsheet applications may evaluate as a formula."""

    if not isinstance(value, str):
        return value
    text = _unicode_scalar_text(value)
    probe = text.lstrip(_SPREADSHEET_LEADING_WHITESPACE)
    if text.startswith(("\t", "\r", "\n")) or probe.startswith(_SPREADSHEET_FORMULA_PREFIXES):
        return "'" + text
    return text


def _fsync_directory(path):
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_to_xhtml(colnames, rows):
    # a row is a list of values with the first value being a flag indicating if the row should be indented
    colnames = tuple(colnames)
    before_headers, remaining_template = MAIN_TEMPLATE.split("$colheaders", 1)
    between_headers_and_rows, after_rows = remaining_template.split("$rows", 1)
    previous_group_id = None
    folder = mkdtemp()
    destpath = op.join(folder, "export.htm")
    try:
        with open(destpath, "wt", encoding="utf-8", newline="") as stream:
            stream.write(before_headers)
            for name in colnames:
                stream.write(COLHEADERS_TEMPLATE.format(name=_xhtml_text(name)))
            stream.write(between_headers_and_rows)
            for row_number, row in enumerate(rows, start=1):
                if len(row) != len(colnames) + 1:
                    raise ValueError(
                        "XHTML export row {} has {} values; expected {}".format(
                            row_number,
                            len(row),
                            len(colnames) + 1,
                        )
                    )
                # [2:] removes the group identifier and filename.
                if row[0] != previous_group_id:
                    # A changed group means this row is the reference and is not indented.
                    indented = ""
                else:
                    indented = "indented"
                filename = _xhtml_text(row[1])
                cells = "".join(CELL_TEMPLATE.format(value=_xhtml_text(value)) for value in row[2:])
                stream.write(ROW_TEMPLATE.format(indented=indented, filename=filename, cells=cells))
                previous_group_id = row[0]
            stream.write(after_rows)
    except BaseException:
        try:
            os.unlink(destpath)
        except OSError:
            pass
        try:
            os.rmdir(folder)
        except OSError:
            pass
        raise
    return destpath


def export_to_csv(dest, colnames, rows):
    """Atomically replace *dest* with a spreadsheet-safe UTF-8 CSV export."""

    destination = Path(dest).absolute()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(destination.name),
        suffix=".tmp",
        dir=os.fspath(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            colnames = tuple(colnames)
            writer.writerow([_spreadsheet_safe_cell(value) for value in ("Group ID", *colnames)])
            for row_number, row in enumerate(rows, start=1):
                if len(row) != len(colnames) + 1:
                    raise ValueError(
                        "CSV export row {} has {} values; expected {}".format(
                            row_number,
                            len(row),
                            len(colnames) + 1,
                        )
                    )
                writer.writerow([_spreadsheet_safe_cell(value) for value in row])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(os.fspath(temporary), os.fspath(destination))
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
