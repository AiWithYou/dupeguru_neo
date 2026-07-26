# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded, non-destructive image comparison rendering.

Every operation returns a new in-memory ``QImage``. Source files are only read
through ``QImageReader`` and are never rewritten.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from enum import Enum
from math import sqrt
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QPoint, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QImageReader, QPainter

from core.fs import _open_readonly_no_follow
from core.safe_walk import is_reparse_point

DEFAULT_MAX_DISPLAY_SIZE = QSize(1600, 1600)
DEFAULT_MAX_DISPLAY_PIXELS = 2_000_000
DEFAULT_MAX_ENCODED_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_SOURCE_SIZE = QSize(32_768, 32_768)
DEFAULT_MAX_SOURCE_PIXELS = 64_000_000
DEFAULT_ALLOCATION_LIMIT_MB = 64
_QIMAGE_READER_ALLOCATION_LOCK = threading.RLock()


class ComparisonMode(str, Enum):
    SIDE_BY_SIDE = "side_by_side"
    ALPHA_OVERLAY = "alpha_overlay"
    BLINK = "blink"
    DIFFERENCE_HEATMAP = "difference_heatmap"


class ComparisonError(RuntimeError):
    """Raised when a source cannot be rendered safely for comparison."""


@dataclass(frozen=True)
class BoundedImage:
    image: QImage
    source_size: QSize
    bounded: bool


@dataclass(frozen=True)
class NormalizedImagePair:
    selected: QImage
    reference: QImage
    display_size: QSize
    selected_source_size: QSize
    reference_source_size: QSize
    bounded: bool


def _validate_positive_integer(name: str, value: int):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_limits(
    max_size: QSize,
    max_pixels: int,
    *,
    max_encoded_bytes: int = DEFAULT_MAX_ENCODED_BYTES,
    max_source_size: QSize = DEFAULT_MAX_SOURCE_SIZE,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
    allocation_limit_mb: int = DEFAULT_ALLOCATION_LIMIT_MB,
):
    if not max_size.isValid() or max_size.isEmpty():
        raise ValueError("max_size must be positive")
    _validate_positive_integer("max_pixels", max_pixels)
    _validate_positive_integer("max_encoded_bytes", max_encoded_bytes)
    if not max_source_size.isValid() or max_source_size.isEmpty():
        raise ValueError("max_source_size must be positive")
    _validate_positive_integer("max_source_pixels", max_source_pixels)
    _validate_positive_integer("allocation_limit_mb", allocation_limit_mb)


def _bounded_size(size: QSize, max_size: QSize, max_pixels: int) -> QSize:
    _validate_limits(max_size, max_pixels)
    if not size.isValid() or size.isEmpty():
        raise ComparisonError("The image has no valid display dimensions.")
    target = size.scaled(max_size, Qt.AspectRatioMode.KeepAspectRatio)
    area = target.width() * target.height()
    if area > max_pixels:
        factor = sqrt(max_pixels / area)
        target = QSize(
            max(1, int(target.width() * factor)),
            max(1, int(target.height() * factor)),
        )
    return target


def _read_source_payload(path, max_encoded_bytes: int) -> tuple[str, bytes]:
    source_path = Path(os.path.abspath(os.fspath(path)))
    try:
        with _open_readonly_no_follow(source_path) as stream:
            source_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(source_stat.st_mode) or is_reparse_point(source_stat):
                raise OSError("comparison source is not a plain regular file")
            if int(source_stat.st_size) > max_encoded_bytes:
                raise ComparisonError(
                    f"Could not decode {source_path}: encoded source exceeds "
                    f"the {max_encoded_bytes}-byte safety limit."
                )
            payload = stream.read(max_encoded_bytes + 1)
            if len(payload) > max_encoded_bytes:
                raise ComparisonError(
                    f"Could not decode {source_path}: encoded source grew beyond "
                    f"the {max_encoded_bytes}-byte safety limit."
                )
    except ComparisonError:
        raise
    except (OSError, ValueError) as error:
        raise ComparisonError(f"Could not read {source_path} without following aliases: {error}") from error
    return str(source_path), payload


def _source_size_within_limits(
    source_size: QSize,
    *,
    max_source_size: QSize,
    max_source_pixels: int,
) -> bool:
    width = int(source_size.width())
    height = int(source_size.height())
    return (
        width > 0
        and height > 0
        and width <= max_source_size.width()
        and height <= max_source_size.height()
        and width * height <= max_source_pixels
    )


def load_bounded_image(
    path,
    *,
    max_size: QSize = DEFAULT_MAX_DISPLAY_SIZE,
    max_pixels: int = DEFAULT_MAX_DISPLAY_PIXELS,
    max_encoded_bytes: int = DEFAULT_MAX_ENCODED_BYTES,
    max_source_size: QSize = DEFAULT_MAX_SOURCE_SIZE,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
    allocation_limit_mb: int = DEFAULT_ALLOCATION_LIMIT_MB,
) -> BoundedImage:
    """Decode one image no larger than the comparison display budget."""

    _validate_limits(
        max_size,
        max_pixels,
        max_encoded_bytes=max_encoded_bytes,
        max_source_size=max_source_size,
        max_source_pixels=max_source_pixels,
        allocation_limit_mb=allocation_limit_mb,
    )
    path_text, payload = _read_source_payload(path, max_encoded_bytes)
    buffer = QBuffer()
    buffer.setData(payload)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ComparisonError(f"Could not open a bounded decoder buffer for {path_text}.")
    reader = None
    try:
        with _QIMAGE_READER_ALLOCATION_LOCK:
            previous_allocation_limit = QImageReader.allocationLimit()
            try:
                effective_allocation_limit = allocation_limit_mb
                if previous_allocation_limit > 0:
                    effective_allocation_limit = min(
                        effective_allocation_limit,
                        previous_allocation_limit,
                    )
                QImageReader.setAllocationLimit(effective_allocation_limit)
                reader = QImageReader(buffer)
                reader.setDecideFormatFromContent(True)
                reader.setAutoTransform(True)
                source_size = reader.size()
                if not source_size.isValid() or source_size.isEmpty():
                    reason = reader.errorString() or "unknown image format"
                    raise ComparisonError(f"Could not read image dimensions for {path_text}: {reason}")
                if not _source_size_within_limits(
                    source_size,
                    max_source_size=max_source_size,
                    max_source_pixels=max_source_pixels,
                ):
                    raise ComparisonError(
                        f"Could not decode {path_text}: source dimensions "
                        f"{source_size.width()}x{source_size.height()} exceed the safety limit."
                    )
                decode_size = _bounded_size(source_size, max_size, max_pixels)
                if decode_size != source_size:
                    reader.setScaledSize(decode_size)
                image = reader.read()
                if image.isNull():
                    reason = reader.errorString() or "image decoder returned no pixels"
                    raise ComparisonError(f"Could not decode {path_text}: {reason}")
                decoded_width = int(image.width())
                decoded_height = int(image.height())
                if (
                    decoded_width > max_size.width()
                    or decoded_height > max_size.height()
                    or decoded_width * decoded_height > max_pixels
                ):
                    raise ComparisonError(
                        f"Could not decode {path_text}: the decoder ignored the " "bounded scaled-decode request."
                    )
            finally:
                reader = None
                QImageReader.setAllocationLimit(previous_allocation_limit)
    finally:
        buffer.close()

    bounded = decode_size != source_size
    return BoundedImage(
        image=image.convertToFormat(QImage.Format.Format_RGBA8888),
        source_size=QSize(source_size),
        bounded=bounded,
    )


def _fit_to_canvas(image: QImage, canvas_size: QSize) -> QImage:
    scaled = image.scaled(
        canvas_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QImage(canvas_size, QImage.Format.Format_RGBA8888)
    canvas.fill(QColor(0, 0, 0, 255))
    origin = QPoint(
        (canvas_size.width() - scaled.width()) // 2,
        (canvas_size.height() - scaled.height()) // 2,
    )
    painter = QPainter(canvas)
    painter.drawImage(origin, scaled)
    painter.end()
    return canvas


def normalize_images(
    selected: BoundedImage,
    reference: BoundedImage,
    *,
    max_size: QSize = DEFAULT_MAX_DISPLAY_SIZE,
    max_pixels: int = DEFAULT_MAX_DISPLAY_PIXELS,
) -> NormalizedImagePair:
    """Scale both images into a shared, aspect-preserving display canvas."""

    _validate_limits(max_size, max_pixels)
    raw_canvas = QSize(
        max(selected.image.width(), reference.image.width()),
        max(selected.image.height(), reference.image.height()),
    )
    canvas_size = _bounded_size(raw_canvas, max_size, max_pixels)
    return NormalizedImagePair(
        selected=_fit_to_canvas(selected.image, canvas_size),
        reference=_fit_to_canvas(reference.image, canvas_size),
        display_size=canvas_size,
        selected_source_size=QSize(selected.source_size),
        reference_source_size=QSize(reference.source_size),
        bounded=selected.bounded or reference.bounded or canvas_size != raw_canvas,
    )


def load_normalized_pair(
    selected_path,
    reference_path,
    *,
    max_size: QSize = DEFAULT_MAX_DISPLAY_SIZE,
    max_pixels: int = DEFAULT_MAX_DISPLAY_PIXELS,
    max_encoded_bytes: int = DEFAULT_MAX_ENCODED_BYTES,
    max_source_size: QSize = DEFAULT_MAX_SOURCE_SIZE,
    max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
    allocation_limit_mb: int = DEFAULT_ALLOCATION_LIMIT_MB,
) -> NormalizedImagePair:
    selected = load_bounded_image(
        selected_path,
        max_size=max_size,
        max_pixels=max_pixels,
        max_encoded_bytes=max_encoded_bytes,
        max_source_size=max_source_size,
        max_source_pixels=max_source_pixels,
        allocation_limit_mb=allocation_limit_mb,
    )
    reference = load_bounded_image(
        reference_path,
        max_size=max_size,
        max_pixels=max_pixels,
        max_encoded_bytes=max_encoded_bytes,
        max_source_size=max_source_size,
        max_source_pixels=max_source_pixels,
        allocation_limit_mb=allocation_limit_mb,
    )
    return normalize_images(
        selected,
        reference,
        max_size=max_size,
        max_pixels=max_pixels,
    )


def alpha_overlay(pair: NormalizedImagePair, opacity: float = 0.5) -> QImage:
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("opacity must be between zero and one")
    result = pair.reference.copy()
    painter = QPainter(result)
    painter.setOpacity(opacity)
    painter.drawImage(0, 0, pair.selected)
    painter.end()
    return result


def _heatmap_color_table() -> list[int]:
    table = []
    for value in range(256):
        if value < 64:
            red, green, blue = 0, 0, value * 4
        elif value < 128:
            red, green, blue = 0, (value - 64) * 4, 255
        elif value < 192:
            red, green, blue = (value - 128) * 4, 255, 255 - (value - 128) * 4
        else:
            red, green, blue = 255, 255 - (value - 192) * 4, 0
        table.append(QColor(red, green, blue).rgba())
    return table


HEATMAP_COLOR_TABLE = _heatmap_color_table()


def absolute_difference_heatmap(pair: NormalizedImagePair) -> QImage:
    """Render absolute per-channel difference with a visible heat palette."""

    difference = pair.selected.copy()
    painter = QPainter(difference)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    painter.drawImage(0, 0, pair.reference)
    painter.end()
    grayscale = difference.convertToFormat(QImage.Format.Format_Grayscale8)
    indexed = grayscale.convertToFormat(
        QImage.Format.Format_Indexed8,
        HEATMAP_COLOR_TABLE,
    )
    return indexed.convertToFormat(QImage.Format.Format_RGB32)
