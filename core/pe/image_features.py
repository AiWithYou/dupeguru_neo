# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Deterministic, color-managed image features used by the picture matcher.

The normalization contract is intentionally centralized here:

* only the first frame of an animated image is analyzed;
* EXIF orientation is applied before any feature is calculated;
* embedded ICC profiles are converted to sRGB (missing profiles are explicitly assumed sRGB);
* alpha is composited onto opaque sRGB white; and
* pHash, dHash, color, bounded tile, quality, and thumbnail identity share one
  versioned normalization policy.

Changing any of those rules requires a new :data:`FEATURE_VERSION` so cached features cannot be
silently mixed across incompatible decoder policies.
"""

from __future__ import annotations

import hashlib
import math
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Tuple

from core.pe.block import getblocks2
from core.pe.candidate_index import dct_phash

FEATURE_VERSION = "pillow_srgb_exif_firstframe_whitealpha_visual_v3"
PHASH_SAMPLE_SIZE = 32
PHASH_BIT_WIDTH = 64
DHASH_SAMPLE_SIZE = (9, 8)
DHASH_BIT_WIDTH = 64
COLOR_HISTOGRAM_BINS_PER_CHANNEL = 4
COLOR_HISTOGRAM_SAMPLE_SIZE = (32, 32)
COLOR_HISTOGRAM_LENGTH = COLOR_HISTOGRAM_BINS_PER_CHANNEL**3
MAX_TILE_FINGERPRINTS = 4
TILE_BOX_SCALE = 10_000
JPEG_BLOCKINESS_MAX_SAMPLES = 200_000
THUMBNAIL_MAX_SIZE = (256, 256)
DEFAULT_MAX_DECODE_PIXELS = 64_000_000
# The streaming feature pipeline keeps one normalized RGB base image plus at
# most one owned full-resolution orientation or crop.  A grayscale analysis
# plane can briefly coexist with those two RGB buffers.  The legacy block
# extractor can instead hold one RGB block crop; at its supported worst case
# (one block) that crop is full size:
# 3 (base RGB) + 3 (owned RGB) + 3 (block crop) = 9 bytes/pixel.
FEATURE_WORKING_BYTES_PER_PIXEL = 9
# One caller-owned normalized RGB base, one streamed RGB orientation/crop, and
# one temporary grayscale plane are the only full-resolution pixel images that
# may coexist during feature extraction.
MAX_LIVE_FULL_RESOLUTION_IMAGES = 3
DEFAULT_MAX_FEATURE_WORKING_BYTES = DEFAULT_MAX_DECODE_PIXELS * FEATURE_WORKING_BYTES_PER_PIXEL

Color = Tuple[int, int, int]
Blocks = Tuple[Color, ...]


class ImageFeatureError(Exception):
    """Base class for a failure that prevents trustworthy feature extraction."""

    code = "decoder_failure"


class DecoderUnavailableError(ImageFeatureError):
    code = "decoder_unavailable"


class ImageDecodeError(ImageFeatureError):
    code = "decoder_failure"


class ImageResourceLimitError(ImageFeatureError):
    code = "resource_limit"


@dataclass(frozen=True)
class TileFingerprint:
    kind: str
    phash: int
    dhash: int
    box: Tuple[int, int, int, int]

    def __post_init__(self):
        if self.kind not in {
            "center_90",
            "center_75",
            "center_50",
            "content",
        }:
            raise ValueError("unsupported image tile fingerprint kind")
        for value in (self.phash, self.dhash):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value >= 1 << 64:
                raise ValueError("tile fingerprint does not fit 64 bits")
        if (
            not isinstance(self.box, tuple)
            or len(self.box) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) for value in self.box)
        ):
            raise ValueError("tile fingerprint box must use four integer coordinates")
        left, top, right, bottom = self.box
        if not (0 <= left < right <= TILE_BOX_SCALE and 0 <= top < bottom <= TILE_BOX_SCALE):
            raise ValueError("tile fingerprint box exceeds normalized image bounds")


@dataclass(frozen=True)
class ImageQuality:
    bit_depth: int
    exif_count: int
    metadata_count: int
    jpeg_artifact_score: float

    def __post_init__(self):
        for value in (self.bit_depth, self.exif_count, self.metadata_count):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("image quality counts must be non-negative integers")
        if (
            not isinstance(self.jpeg_artifact_score, (int, float))
            or isinstance(self.jpeg_artifact_score, bool)
            or not math.isfinite(self.jpeg_artifact_score)
            or not 0 <= self.jpeg_artifact_score <= 1
        ):
            raise ValueError("JPEG artifact score must be between zero and one")


@dataclass(frozen=True)
class ImageFeatures:
    """Features derived from one normalized first frame.

    ``blocks`` and ``phashes`` contain either the identity orientation only or all eight D4
    orientations in the same order.  A partial orientation set is rejected because it would make
    the rotated candidate guarantee ambiguous.
    """

    dimensions: Tuple[int, int]
    frame_count: int
    blocks: Tuple[Blocks, ...]
    phashes: Tuple[int, ...]
    dhashes: Tuple[int, ...]
    color_histogram: Tuple[int, ...]
    tile_fingerprints: Tuple[TileFingerprint, ...]
    quality: ImageQuality
    thumbnail_size: Tuple[int, int]
    thumbnail_key: str
    feature_version: str = FEATURE_VERSION

    def __post_init__(self) -> None:
        width, height = self.dimensions
        if width <= 0 or height <= 0:
            raise ValueError("feature dimensions must be positive")
        if self.frame_count <= 0:
            raise ValueError("frame_count must be positive")
        if (
            len(self.blocks) not in {1, 8}
            or len(self.phashes) != len(self.blocks)
            or len(self.dhashes) != len(self.blocks)
        ):
            raise ValueError("features require one or eight aligned pHash/dHash orientations")
        if any(value < 0 or value >= 1 << PHASH_BIT_WIDTH for value in self.phashes):
            raise ValueError("pHash does not fit the configured width")
        if any(value < 0 or value >= 1 << DHASH_BIT_WIDTH for value in self.dhashes):
            raise ValueError("dHash does not fit the configured width")
        if (
            len(self.color_histogram) != COLOR_HISTOGRAM_LENGTH
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in self.color_histogram)
            or sum(self.color_histogram) != COLOR_HISTOGRAM_SAMPLE_SIZE[0] * COLOR_HISTOGRAM_SAMPLE_SIZE[1]
        ):
            raise ValueError("color histogram has an incompatible fixed-bin payload")
        if len(self.tile_fingerprints) > MAX_TILE_FINGERPRINTS or len(
            {item.kind for item in self.tile_fingerprints}
        ) != len(self.tile_fingerprints):
            raise ValueError("tile fingerprints must be unique and bounded")
        if not isinstance(self.quality, ImageQuality):
            raise ValueError("image features require measured quality metadata")
        if not self.thumbnail_key:
            raise ValueError("thumbnail key must not be empty")
        if any(value <= 0 for value in self.thumbnail_size):
            raise ValueError("thumbnail dimensions must be positive")
        if not self.feature_version:
            raise ValueError("feature_version must not be empty")

    @property
    def orientation_count(self) -> int:
        return len(self.phashes)


def _pillow_modules():
    try:
        from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
    except ImportError as error:
        raise DecoderUnavailableError("Pillow is required for image feature extraction") from error
    return Image, ImageCms, ImageOps, UnidentifiedImageError


def _close_owned_image(image, owner=None) -> None:
    """Close a Pillow image unless it is the caller-owned base image."""

    if image is not None and image is not owner:
        image.close()


def _required_feature_working_bytes(size) -> int:
    width, height = size
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive integers")
    return width * height * FEATURE_WORKING_BYTES_PER_PIXEL


def _require_feature_working_budget(size, maximum_bytes: int) -> int:
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("max_working_bytes must be a positive integer")
    required = _required_feature_working_bytes(size)
    if required > maximum_bytes:
        raise ImageResourceLimitError(
            "image feature working set requires at least {:,} bytes; "
            "configured limit is {:,}".format(required, maximum_bytes)
        )
    return required


def _has_alpha(image) -> bool:
    return image.mode in {"LA", "PA", "RGBA"} or "transparency" in image.info


def _convert_to_srgb(
    image,
    icc_profile,
    image_cms,
    *,
    consume=False,
):
    if not icc_profile:
        try:
            return image.convert("RGB")
        finally:
            if consume:
                image.close()
    source = None
    try:
        source_profile = image_cms.ImageCmsProfile(BytesIO(icc_profile))
        color_space = source_profile.profile.xcolor_space.strip().upper()
        source_mode = {
            "RGB": "RGB",
            "CMYK": "CMYK",
            "GRAY": "L",
            "LAB": "LAB",
        }.get(color_space)
        if source_mode is None:
            raise ImageDecodeError("unsupported ICC input color space: {!r}".format(color_space))
        source = image if image.mode == source_mode else image.convert(source_mode)
        if consume and source is not image:
            image.close()
        destination_profile = image_cms.createProfile("sRGB")
        return image_cms.profileToProfile(
            source,
            source_profile,
            destination_profile,
            renderingIntent=0,
            outputMode="RGB",
            inPlace=False,
        )
    except ImageDecodeError:
        raise
    except MemoryError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise ImageDecodeError("invalid or unsupported embedded ICC profile") from error
    except Exception as error:
        # Pillow exposes profile failures as ImageCms.PyCMSError, which is not guaranteed to be
        # available from every supported Pillow build.
        raise ImageDecodeError("could not convert embedded ICC profile to sRGB") from error
    finally:
        if source is not None and (source is not image or consume):
            source.close()
        elif consume:
            image.close()


def _image_bit_depth(image) -> int:
    explicit = getattr(image, "bits", None)
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    mode = str(getattr(image, "mode", ""))
    if mode == "1":
        return 1
    if mode.startswith("I;16"):
        return 16
    if mode in {"I", "F"}:
        return 32
    if mode:
        return 8
    return 0


def _source_metadata(source) -> Tuple[str, int, int, int]:
    source_format = str(getattr(source, "format", "") or "").upper()
    bit_depth = _image_bit_depth(source)
    try:
        exif_count = len(source.getexif())
    except (AttributeError, OSError, TypeError, ValueError):
        exif_count = 0
    try:
        info_count = len(source.info)
    except (AttributeError, TypeError):
        info_count = 0
    return source_format, bit_depth, exif_count, exif_count + info_count


def _normalize_first_frame(
    path: Path,
    max_pixels: int,
    max_working_bytes: int,
):
    Image, ImageCms, ImageOps, UnidentifiedImageError = _pillow_modules()
    if type(max_pixels) is not int or max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if type(max_working_bytes) is not int or max_working_bytes <= 0:
        raise ValueError("max_working_bytes must be a positive integer")
    oriented = None
    alpha = None
    normalized = None
    keep_normalized = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                source.seek(0)
                width, height = source.size
                if width <= 0 or height <= 0:
                    raise ImageDecodeError("image has invalid dimensions")
                if width * height > max_pixels:
                    raise ImageResourceLimitError(
                        "image has {:,} pixels; configured limit is {:,}".format(width * height, max_pixels)
                    )
                _require_feature_working_budget(
                    (width, height),
                    max_working_bytes,
                )
                frame_count = int(getattr(source, "n_frames", 1) or 1)
                icc_profile = source.info.get("icc_profile")
                source_metadata = _source_metadata(source)
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.load()
        if _has_alpha(oriented):
            if oriented.mode in {"LA", "RGBA"}:
                alpha = oriented.getchannel("A")
            else:
                rgba = oriented.convert("RGBA")
                try:
                    alpha = rgba.getchannel("A")
                finally:
                    _close_owned_image(rgba, oriented)
        normalized = _convert_to_srgb(
            oriented,
            icc_profile,
            ImageCms,
            consume=True,
        )
        oriented = None
        if alpha is not None:
            background = Image.new("RGB", normalized.size, (255, 255, 255))
            try:
                background.paste(normalized, mask=alpha)
            except Exception:
                background.close()
                raise
            normalized.close()
            normalized = background
        keep_normalized = True
        return normalized, frame_count, Image, source_metadata
    except ImageFeatureError:
        raise
    except MemoryError as error:
        raise ImageResourceLimitError("not enough memory to decode image") from error
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ImageResourceLimitError("image exceeds Pillow's decompression-bomb limit") from error
    except (FileNotFoundError, PermissionError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise ImageDecodeError("could not decode first image frame: {}".format(error)) from error
    except Exception as error:
        raise ImageDecodeError("unexpected first-frame decoder failure: {}".format(error)) from error
    finally:
        _close_owned_image(oriented, normalized)
        _close_owned_image(alpha, normalized)
        if not keep_normalized:
            _close_owned_image(normalized)


def _orientation_transforms(include_orientations: bool, image_module) -> Tuple:
    if not include_orientations:
        return (None,)
    transpose = image_module.Transpose
    return (
        None,
        transpose.FLIP_LEFT_RIGHT,
        transpose.ROTATE_180,
        transpose.FLIP_TOP_BOTTOM,
        transpose.TRANSPOSE,
        transpose.ROTATE_270,
        transpose.TRANSVERSE,
        transpose.ROTATE_90,
    )


def _extract_orientation_features(
    image,
    block_count_per_side: int,
    include_orientations: bool,
    image_module,
):
    """Extract D4 features while retaining at most one owned orientation."""

    blocks = []
    phashes = []
    dhashes = []
    with warnings.catch_warnings():
        # Pillow 12 deprecates getdata(), which the long-standing C block implementation calls
        # internally.  The pixels and block algorithm are unchanged.
        warnings.filterwarnings(
            "ignore",
            message=r"Image\.Image\.getdata is deprecated",
            category=DeprecationWarning,
        )
        for transform in _orientation_transforms(
            include_orientations,
            image_module,
        ):
            oriented = image if transform is None else image.transpose(transform)
            try:
                blocks.append(tuple(getblocks2(oriented, block_count_per_side)))
                phashes.append(_phash(oriented, image_module))
                dhashes.append(_dhash(oriented, image_module))
            finally:
                _close_owned_image(oriented, image)
    return tuple(blocks), tuple(phashes), tuple(dhashes)


def _phash(image, image_module) -> int:
    gray = image.convert("L")
    sample = None
    try:
        sample = gray.resize(
            (PHASH_SAMPLE_SIZE, PHASH_SAMPLE_SIZE),
            resample=image_module.Resampling.LANCZOS,
            reducing_gap=3.0,
        )
        get_pixels = getattr(
            sample,
            "get_flattened_data",
            sample.getdata,
        )
        pixels = tuple(get_pixels())
        rows = tuple(pixels[offset : offset + PHASH_SAMPLE_SIZE] for offset in range(0, len(pixels), PHASH_SAMPLE_SIZE))
        return dct_phash(rows).value
    finally:
        _close_owned_image(sample, gray)
        _close_owned_image(gray, image)


def _dhash(image, image_module) -> int:
    gray = image.convert("L")
    sample = None
    try:
        sample = gray.resize(
            DHASH_SAMPLE_SIZE,
            resample=image_module.Resampling.LANCZOS,
            reducing_gap=3.0,
        )
        get_pixels = getattr(
            sample,
            "get_flattened_data",
            sample.getdata,
        )
        pixels = tuple(get_pixels())
        fingerprint = 0
        width, height = DHASH_SAMPLE_SIZE
        bit = 0
        for y in range(height):
            offset = y * width
            for x in range(width - 1):
                if pixels[offset + x] > pixels[offset + x + 1]:
                    fingerprint |= 1 << bit
                bit += 1
        return fingerprint
    finally:
        _close_owned_image(sample, gray)
        _close_owned_image(gray, image)


def _color_histogram(image, image_module) -> Tuple[int, ...]:
    resized = image.resize(
        COLOR_HISTOGRAM_SAMPLE_SIZE,
        resample=image_module.Resampling.BOX,
    )
    sample = None
    try:
        sample = resized.convert("RGB")
        bins = [0] * COLOR_HISTOGRAM_LENGTH
        get_pixels = getattr(
            sample,
            "get_flattened_data",
            sample.getdata,
        )
        for red, green, blue in get_pixels():
            red_bin = min(
                red * COLOR_HISTOGRAM_BINS_PER_CHANNEL // 256,
                3,
            )
            green_bin = min(
                green * COLOR_HISTOGRAM_BINS_PER_CHANNEL // 256,
                3,
            )
            blue_bin = min(
                blue * COLOR_HISTOGRAM_BINS_PER_CHANNEL // 256,
                3,
            )
            index = (
                red_bin * COLOR_HISTOGRAM_BINS_PER_CHANNEL**2 + green_bin * COLOR_HISTOGRAM_BINS_PER_CHANNEL + blue_bin
            )
            bins[index] += 1
        return tuple(bins)
    finally:
        _close_owned_image(sample, resized)
        _close_owned_image(resized, image)


def _normalized_box(box, size):
    width, height = size
    left, top, right, bottom = box
    return (
        int(round(left * TILE_BOX_SCALE / width)),
        int(round(top * TILE_BOX_SCALE / height)),
        int(round(right * TILE_BOX_SCALE / width)),
        int(round(bottom * TILE_BOX_SCALE / height)),
    )


def _center_crop(image, ratio):
    width, height = image.size
    crop_width = max(2, int(round(width * ratio)))
    crop_height = max(2, int(round(height * ratio)))
    left = max(0, (width - crop_width) // 2)
    top = max(0, (height - crop_height) // 2)
    box = (left, top, left + crop_width, top + crop_height)
    return image.crop(box), _normalized_box(box, image.size)


def _content_crop(image, image_module):
    sample = image.copy()
    try:
        sample.thumbnail(
            (64, 64),
            resample=image_module.Resampling.BOX,
            reducing_gap=3.0,
        )
        width, height = sample.size
        if width < 8 or height < 8:
            return None
        pixels = sample.load()
        corners = (
            pixels[0, 0],
            pixels[width - 1, 0],
            pixels[0, height - 1],
            pixels[width - 1, height - 1],
        )
        background = tuple(sorted(color[channel] for color in corners)[len(corners) // 2] for channel in range(3))
        coordinates = []
        for y in range(height):
            for x in range(width):
                color = pixels[x, y]
                if max(abs(color[channel] - background[channel]) for channel in range(3)) > 16:
                    coordinates.append((x, y))
        if not coordinates:
            return None
        left = min(item[0] for item in coordinates)
        top = min(item[1] for item in coordinates)
        right = max(item[0] for item in coordinates) + 1
        bottom = max(item[1] for item in coordinates) + 1
        if left < 2 and top < 2 and right > width - 2 and bottom > height - 2:
            return None
        if (right - left) * (bottom - top) < width * height // 4:
            return None
        source_width, source_height = image.size
        scale_x = source_width / width
        scale_y = source_height / height
        box = (
            max(0, int(math.floor(left * scale_x))),
            max(0, int(math.floor(top * scale_y))),
            min(source_width, int(math.ceil(right * scale_x))),
            min(source_height, int(math.ceil(bottom * scale_y))),
        )
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            return None
        return image.crop(box), _normalized_box(box, image.size)
    finally:
        _close_owned_image(sample, image)


def _tile_fingerprints(image, image_module) -> Tuple[TileFingerprint, ...]:
    result = []
    seen = set()

    def append_candidate(kind, candidate):
        tile, box = candidate
        try:
            phash = _phash(tile, image_module)
            dhash = _dhash(tile, image_module)
            key = phash, dhash
            if key in seen:
                return
            seen.add(key)
            result.append(TileFingerprint(kind, phash, dhash, box))
        finally:
            _close_owned_image(tile, image)

    for kind, ratio in (
        ("center_90", 0.90),
        ("center_75", 0.75),
        ("center_50", 0.50),
    ):
        append_candidate(kind, _center_crop(image, ratio))
    content = _content_crop(image, image_module)
    if content is not None:
        append_candidate("content", content)
    return tuple(result)


def _jpeg_blockiness(image, source_format: str) -> float:
    if source_format not in {"JPEG", "JPG"}:
        return 0.0
    gray = image.convert("L")
    try:
        width, height = gray.size
        if width < 16 or height < 16:
            return 0.0
        pixels = gray.load()
        vertical_lines = max(0, (width - 1) // 8)
        horizontal_lines = max(0, (height - 1) // 8)
        estimated = vertical_lines * height + horizontal_lines * width
        step = max(
            1,
            math.ceil(estimated / JPEG_BLOCKINESS_MAX_SAMPLES),
        )
        boundary_total = 0.0
        nearby_total = 0.0
        samples = 0
        for x in range(8, width, 8):
            if x < 2 or x + 1 >= width:
                continue
            for y in range(0, height, step):
                boundary_total += abs(pixels[x, y] - pixels[x - 1, y])
                nearby_total += (abs(pixels[x - 1, y] - pixels[x - 2, y]) + abs(pixels[x + 1, y] - pixels[x, y])) / 2
                samples += 1
        for y in range(8, height, 8):
            if y < 2 or y + 1 >= height:
                continue
            for x in range(0, width, step):
                boundary_total += abs(pixels[x, y] - pixels[x, y - 1])
                nearby_total += (abs(pixels[x, y - 1] - pixels[x, y - 2]) + abs(pixels[x, y + 1] - pixels[x, y])) / 2
                samples += 1
        if samples < 16:
            return 0.0
        boundary_mean = boundary_total / samples
        nearby_mean = nearby_total / samples
        conservative_excess = boundary_mean - nearby_mean * 1.15 - 2.0
        if conservative_excess <= 0:
            return 0.0
        return round(
            min(1.0, conservative_excess / 24.0),
            6,
        )
    finally:
        _close_owned_image(gray, image)


def _thumbnail_identity(image, image_module) -> Tuple[Tuple[int, int], str]:
    """Return bounded display metadata without encoding or retaining image bytes."""

    width, height = image.size
    maximum_width, maximum_height = THUMBNAIL_MAX_SIZE
    if width <= maximum_width and height <= maximum_height:
        size = image.size
    elif width * maximum_height > height * maximum_width:
        size = maximum_width, max(1, round(height * maximum_width / width))
    else:
        size = max(1, round(width * maximum_height / height)), maximum_height
    thumbnail = (
        image
        if size == image.size
        else image.resize(
            size,
            resample=image_module.Resampling.LANCZOS,
            reducing_gap=3.0,
        )
    )
    try:
        digest = hashlib.sha256()
        digest.update(FEATURE_VERSION.encode("ascii"))
        digest.update(b"\0thumbnail-rgb\0")
        digest.update(size[0].to_bytes(4, "big"))
        digest.update(size[1].to_bytes(4, "big"))
        digest.update(thumbnail.tobytes())
        return size, digest.hexdigest()
    finally:
        _close_owned_image(thumbnail, image)


def decode_image_features(
    path,
    block_count_per_side: int,
    include_orientations: bool = False,
    max_pixels: int = DEFAULT_MAX_DECODE_PIXELS,
    max_working_bytes: int = DEFAULT_MAX_FEATURE_WORKING_BYTES,
) -> ImageFeatures:
    """Decode ``path`` and return deterministic normalized matching features."""

    if block_count_per_side <= 0:
        raise ValueError("block_count_per_side must be positive")
    normalized, frame_count, image_module, source_metadata = _normalize_first_frame(
        Path(path),
        max_pixels,
        max_working_bytes,
    )
    try:
        blocks, phashes, dhashes = _extract_orientation_features(
            normalized,
            block_count_per_side,
            include_orientations,
            image_module,
        )
        color_histogram = _color_histogram(normalized, image_module)
        tile_fingerprints = _tile_fingerprints(normalized, image_module)
        source_format, bit_depth, exif_count, metadata_count = source_metadata
        quality = ImageQuality(
            bit_depth=bit_depth,
            exif_count=exif_count,
            metadata_count=metadata_count,
            jpeg_artifact_score=_jpeg_blockiness(normalized, source_format),
        )
        thumbnail_size, thumbnail_key = _thumbnail_identity(normalized, image_module)
        return ImageFeatures(
            dimensions=normalized.size,
            frame_count=frame_count,
            blocks=blocks,
            phashes=phashes,
            dhashes=dhashes,
            color_histogram=color_histogram,
            tile_fingerprints=tile_fingerprints,
            quality=quality,
            thumbnail_size=thumbnail_size,
            thumbnail_key=thumbnail_key,
        )
    except MemoryError as error:
        raise ImageResourceLimitError("not enough memory to calculate image features") from error
    except ImageFeatureError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise ImageDecodeError("could not calculate normalized image features") from error
    except Exception as error:
        raise ImageDecodeError("unexpected normalized feature failure") from error
    finally:
        normalized.close()


def read_image_quality(
    path,
    max_pixels: int = DEFAULT_MAX_DECODE_PIXELS,
    max_working_bytes: int = DEFAULT_MAX_FEATURE_WORKING_BYTES,
) -> ImageQuality:
    """Read bounded, measured image quality metadata for keeper explanations."""

    normalized, _frame_count, _image_module, source_metadata = _normalize_first_frame(
        Path(path),
        max_pixels,
        max_working_bytes,
    )
    try:
        source_format, bit_depth, exif_count, metadata_count = source_metadata
        return ImageQuality(
            bit_depth=bit_depth,
            exif_count=exif_count,
            metadata_count=metadata_count,
            jpeg_artifact_score=_jpeg_blockiness(
                normalized,
                source_format,
            ),
        )
    finally:
        normalized.close()


__all__ = [
    "COLOR_HISTOGRAM_LENGTH",
    "DEFAULT_MAX_FEATURE_WORKING_BYTES",
    "DEFAULT_MAX_DECODE_PIXELS",
    "DHASH_BIT_WIDTH",
    "DecoderUnavailableError",
    "FEATURE_VERSION",
    "ImageDecodeError",
    "ImageFeatureError",
    "ImageFeatures",
    "ImageQuality",
    "ImageResourceLimitError",
    "MAX_LIVE_FULL_RESOLUTION_IMAGES",
    "MAX_TILE_FINGERPRINTS",
    "PHASH_BIT_WIDTH",
    "TileFingerprint",
    "TILE_BOX_SCALE",
    "decode_image_features",
    "read_image_quality",
]
