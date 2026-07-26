import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QSize  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402

import qt.pe.comparison as comparison_module  # noqa: E402
from qt.pe.comparison import (  # noqa: E402
    BoundedImage,
    ComparisonError,
    absolute_difference_heatmap,
    alpha_overlay,
    load_bounded_image,
    load_normalized_pair,
    normalize_images,
)


def solid_image(size, color):
    image = QImage(size, QImage.Format.Format_RGBA8888)
    image.fill(QColor(color))
    return image


def test_pair_normalization_is_bounded_and_never_changes_sources(tmp_path):
    selected_path = tmp_path / "wide.png"
    reference_path = tmp_path / "tall.png"
    assert solid_image(QSize(600, 300), "#CC2222").save(str(selected_path))
    assert solid_image(QSize(300, 600), "#2244CC").save(str(reference_path))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (selected_path, reference_path)}

    pair = load_normalized_pair(
        selected_path,
        reference_path,
        max_size=QSize(120, 100),
        max_pixels=8_000,
    )
    overlay = alpha_overlay(pair)
    heatmap = absolute_difference_heatmap(pair)

    assert pair.selected.size() == pair.reference.size() == pair.display_size
    assert pair.display_size.width() <= 120
    assert pair.display_size.height() <= 100
    assert pair.display_size.width() * pair.display_size.height() <= 8_000
    assert pair.bounded
    assert overlay.size() == pair.display_size
    assert heatmap.size() == pair.display_size
    for path in (selected_path, reference_path):
        assert path.read_bytes() == before[path][0]
        assert path.stat().st_mtime_ns == before[path][1]


def test_alpha_overlay_blends_normalized_pixels():
    red = BoundedImage(solid_image(QSize(2, 2), "#FF0000"), QSize(2, 2), False)
    blue = BoundedImage(solid_image(QSize(2, 2), "#0000FF"), QSize(2, 2), False)
    pair = normalize_images(red, blue)

    pixel = alpha_overlay(pair, 0.5).pixelColor(0, 0)

    assert 126 <= pixel.red() <= 129
    assert pixel.green() == 0
    assert 126 <= pixel.blue() <= 129


def test_absolute_difference_heatmap_is_black_for_equal_pixels_and_colored_for_changes():
    black = BoundedImage(solid_image(QSize(2, 2), "#000000"), QSize(2, 2), False)
    red = BoundedImage(solid_image(QSize(2, 2), "#FF0000"), QSize(2, 2), False)
    equal_pair = normalize_images(black, black)
    changed_pair = normalize_images(red, black)

    equal_pixel = absolute_difference_heatmap(equal_pair).pixelColor(0, 0)
    changed_pixel = absolute_difference_heatmap(changed_pair).pixelColor(0, 0)

    assert equal_pixel == QColor("#000000")
    assert changed_pixel != QColor("#000000")


def test_invalid_image_has_an_explicit_failure(tmp_path):
    invalid = tmp_path / "not-an-image.bin"
    invalid.write_bytes(b"not an image")

    with pytest.raises(ComparisonError, match="Could not read image dimensions"):
        load_bounded_image(invalid)


@pytest.mark.parametrize(
    ("max_size", "max_pixels"),
    [
        (QSize(), 100),
        (QSize(10, 10), 0),
    ],
)
def test_invalid_display_bounds_fail_clearly(tmp_path, max_size, max_pixels):
    image_path = tmp_path / "source.png"
    assert solid_image(QSize(2, 2), "#FFFFFF").save(str(image_path))

    with pytest.raises(ValueError):
        load_bounded_image(
            image_path,
            max_size=max_size,
            max_pixels=max_pixels,
        )


def test_encoded_byte_limit_is_checked_before_decoder_creation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "oversized.bin"
    source.write_bytes(b"12345")

    class ForbiddenReader:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("decoder must not see over-budget encoded bytes")

    monkeypatch.setattr(comparison_module, "QImageReader", ForbiddenReader)

    with pytest.raises(ComparisonError, match="encoded source exceeds"):
        load_bounded_image(source, max_encoded_bytes=4)


def test_source_symlink_is_rejected_before_decode(tmp_path):
    source = tmp_path / "source.png"
    alias = tmp_path / "alias.png"
    assert solid_image(QSize(2, 2), "#FFFFFF").save(str(source))
    try:
        os.symlink(source, alias)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ComparisonError, match="without following aliases"):
        load_bounded_image(alias)


class _ScaledReader64MP:
    allocation_limit = 117
    allocation_history = []
    instances = []

    def __init__(self, _buffer):
        self.scaled_size = QSize()
        self.__class__.instances.append(self)

    @classmethod
    def allocationLimit(cls):
        return cls.allocation_limit

    @classmethod
    def setAllocationLimit(cls, value):
        cls.allocation_limit = value
        cls.allocation_history.append(value)

    def setDecideFormatFromContent(self, _enabled):
        pass

    def setAutoTransform(self, _enabled):
        pass

    def size(self):
        return QSize(8000, 8000)

    def setScaledSize(self, size):
        self.scaled_size = QSize(size)

    def read(self):
        assert self.__class__.allocation_limit == 64
        assert self.scaled_size.isValid()
        return solid_image(self.scaled_size, "#557799")

    def errorString(self):
        return ""


def test_two_64mp_sources_are_scaled_during_decode_with_restored_allocation_limit(
    monkeypatch,
):
    _ScaledReader64MP.allocation_limit = 117
    _ScaledReader64MP.allocation_history = []
    _ScaledReader64MP.instances = []
    monkeypatch.setattr(comparison_module, "QImageReader", _ScaledReader64MP)
    monkeypatch.setattr(
        comparison_module,
        "_read_source_payload",
        lambda path, _limit: (str(path), b"synthetic dimensions"),
    )

    pair = load_normalized_pair(
        "selected.synthetic",
        "reference.synthetic",
        max_size=QSize(100, 100),
        max_pixels=10_000,
    )

    assert pair.selected_source_size == QSize(8000, 8000)
    assert pair.reference_source_size == QSize(8000, 8000)
    assert pair.display_size == QSize(100, 100)
    assert len(_ScaledReader64MP.instances) == 2
    assert all(reader.scaled_size == QSize(100, 100) for reader in _ScaledReader64MP.instances)
    assert _ScaledReader64MP.allocation_history == [64, 117, 64, 117]
    assert _ScaledReader64MP.allocation_limit == 117


def test_source_pixel_limit_is_checked_before_read(monkeypatch):
    class PixelLimitReader(_ScaledReader64MP):
        allocation_limit = 117

        def size(self):
            return QSize(8001, 8000)

        def read(self):
            raise AssertionError("over-budget source dimensions must not be decoded")

    monkeypatch.setattr(comparison_module, "QImageReader", PixelLimitReader)
    monkeypatch.setattr(
        comparison_module,
        "_read_source_payload",
        lambda path, _limit: (str(path), b"synthetic dimensions"),
    )

    with pytest.raises(ComparisonError, match="source dimensions"):
        load_bounded_image("over-limit.synthetic")


def test_allocation_limit_blocks_decoder_that_would_ignore_scaled_decode(
    monkeypatch,
):
    class AllocationGuardReader(_ScaledReader64MP):
        allocation_limit = 117
        allocation_history = []

        def read(self):
            assert self.__class__.allocation_limit == 64
            return QImage()

        def errorString(self):
            return "allocation limit rejected a simulated 256000000-byte image"

    monkeypatch.setattr(comparison_module, "QImageReader", AllocationGuardReader)
    monkeypatch.setattr(
        comparison_module,
        "_read_source_payload",
        lambda path, _limit: (str(path), b"synthetic dimensions"),
    )

    with pytest.raises(ComparisonError, match="allocation limit rejected"):
        load_bounded_image("ignored-scale.synthetic")

    assert AllocationGuardReader.allocation_history == [64, 117]
    assert AllocationGuardReader.allocation_limit == 117


def test_decoder_ignored_scaled_size_is_never_post_scaled(monkeypatch):
    class OversizedDecodedImage:
        def isNull(self):
            return False

        def width(self):
            return 8000

        def height(self):
            return 8000

        def convertToFormat(self, _format):
            raise AssertionError("an oversized decoder result must never reach conversion")

    class IgnoredScaleReader(_ScaledReader64MP):
        allocation_limit = 117

        def read(self):
            assert self.__class__.allocation_limit == 64
            return OversizedDecodedImage()

    monkeypatch.setattr(comparison_module, "QImageReader", IgnoredScaleReader)
    monkeypatch.setattr(
        comparison_module,
        "_read_source_payload",
        lambda path, _limit: (str(path), b"synthetic dimensions"),
    )

    with pytest.raises(ComparisonError, match="ignored the bounded scaled-decode"):
        load_bounded_image("ignored-scale.synthetic")
