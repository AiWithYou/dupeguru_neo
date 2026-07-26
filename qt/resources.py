# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Filesystem-backed Qt image resources.

PyQt6 no longer ships the PyQt5 ``pyrcc5`` command used by the historical
build. Keeping assets as normal package data also makes a missing image fail
clearly instead of silently creating an empty ``QPixmap``.
"""

from importlib.resources import files
from pathlib import Path

RESOURCE_FILES = {
    "logo_se": "dgse_logo_32.png",
    "logo_se_big": "dgse_logo_128.png",
    "plus": "plus_8.png",
    "minus": "minus_8.png",
    "search_clear_13": "search_clear_13.png",
    "exchange": "exchange_purple_upscaled.png",
    "zoom_in": "old_zoom_in.png",
    "zoom_out": "old_zoom_out.png",
    "zoom_original": "old_zoom_original.png",
    "zoom_best_fit": "old_zoom_best_fit.png",
    "error": "dialog-error.png",
}


class ResourceError(RuntimeError):
    """Raised when a required packaged UI asset is unavailable."""


def resource_path(alias: str) -> str:
    try:
        filename = RESOURCE_FILES[alias]
    except KeyError as error:
        raise ResourceError("Unknown Qt resource alias: {!r}".format(alias)) from error
    candidate = files("images").joinpath(filename)
    try:
        path = Path(candidate)
    except TypeError as error:
        raise ResourceError("Qt resources must be installed on a filesystem") from error
    if not path.is_file():
        raise ResourceError("Required Qt resource is missing: {}".format(path))
    return str(path)


__all__ = ["RESOURCE_FILES", "ResourceError", "resource_path"]
