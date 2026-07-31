import math
from types import SimpleNamespace

import pytest
from PIL import Image, ImageCms

import core.pe.image_features as image_features
from core.pe.image_features import (
    DEFAULT_MAX_FEATURE_WORKING_BYTES,
    FEATURE_VERSION,
    ImageDecodeError,
    ImageResourceLimitError,
    MAX_LIVE_FULL_RESOLUTION_IMAGES,
    decode_image_features,
    read_image_quality,
)


def _decode(
    path,
    *,
    rotated=False,
    max_pixels=64_000_000,
    max_working_bytes=DEFAULT_MAX_FEATURE_WORKING_BYTES,
):
    return decode_image_features(
        path,
        block_count_per_side=3,
        include_orientations=rotated,
        max_pixels=max_pixels,
        max_working_bytes=max_working_bytes,
    )


class _LiveImageTracker:
    def __init__(self):
        self.live_full_resolution = 0
        self.peak_full_resolution = 0
        self.created = {"base": 0, "rgb": 0, "gray": 0}
        self.closed = {"base": 0, "rgb": 0, "gray": 0}
        self.small_created = 0
        self.small_closed = 0
        self.transforms = []
        self.next_serial = 0

    def acquire(self, kind):
        self.next_serial += 1
        self.created[kind] += 1
        self.live_full_resolution += 1
        self.peak_full_resolution = max(
            self.peak_full_resolution,
            self.live_full_resolution,
        )
        if self.live_full_resolution > MAX_LIVE_FULL_RESOLUTION_IMAGES:
            raise AssertionError("too many full-resolution images are live")
        return self.next_serial

    def release(self, kind):
        self.closed[kind] += 1
        self.live_full_resolution -= 1


class _SmallTrackedSample:
    def __init__(self, tracker, size):
        self.tracker = tracker
        self.size = size
        self.closed = False
        tracker.small_created += 1

    def getdata(self):
        return (0,) * (self.size[0] * self.size[1])

    def close(self):
        if not self.closed:
            self.closed = True
            self.tracker.small_closed += 1


class _TrackedFullResolutionImage:
    def __init__(self, tracker, size, *, kind="rgb", label=None):
        self.tracker = tracker
        self.size = size
        self.kind = kind
        self.mode = "L" if kind == "gray" else "RGB"
        self.closed = False
        self.serial = tracker.acquire(kind)
        self.label = label or "{}-{}".format(kind, self.serial)

    def transpose(self, transform):
        self.tracker.transforms.append(transform)
        width, height = self.size
        if transform in {
            "transpose",
            "rotate_270",
            "transverse",
            "rotate_90",
        }:
            size = height, width
        else:
            size = width, height
        return _TrackedFullResolutionImage(
            self.tracker,
            size,
            label=str(transform),
        )

    def crop(self, box):
        return _TrackedFullResolutionImage(
            self.tracker,
            (box[2] - box[0], box[3] - box[1]),
            label="crop-{}".format(self.tracker.next_serial + 1),
        )

    def convert(self, mode):
        assert mode == "L"
        return _TrackedFullResolutionImage(
            self.tracker,
            self.size,
            kind="gray",
        )

    def resize(self, size, **_kwargs):
        assert self.kind == "gray"
        return _SmallTrackedSample(self.tracker, size)

    def close(self):
        if not self.closed:
            self.closed = True
            self.tracker.release(self.kind)


_TRACKED_IMAGE_MODULE = SimpleNamespace(
    Transpose=SimpleNamespace(
        FLIP_LEFT_RIGHT="flip_left_right",
        ROTATE_180="rotate_180",
        FLIP_TOP_BOTTOM="flip_top_bottom",
        TRANSPOSE="transpose",
        ROTATE_270="rotate_270",
        TRANSVERSE="transverse",
        ROTATE_90="rotate_90",
    ),
    Resampling=SimpleNamespace(
        LANCZOS="lanczos",
        BOX="box",
    ),
)


def test_feature_extraction_is_deterministic_and_thumbnail_identity_preserves_aspect(tmp_path):
    path = tmp_path / "gradient.png"
    image = Image.new("RGB", (400, 100))
    image.putdata(
        [((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256) for y in range(image.height) for x in range(image.width)]
    )
    image.save(path)

    first = _decode(path)
    second = _decode(path)

    assert first == second
    assert first.feature_version == FEATURE_VERSION
    assert first.dimensions == (400, 100)
    assert len(first.dhashes) == 1
    assert len(first.color_histogram) == 64
    assert sum(first.color_histogram) == 1024
    assert len(first.tile_fingerprints) <= 4
    assert all(
        0 <= item.box[0] < item.box[2] <= 10_000 and 0 <= item.box[1] < item.box[3] <= 10_000
        for item in first.tile_fingerprints
    )
    assert first.quality.bit_depth == 8
    assert first.quality.exif_count == 0
    assert math.isfinite(first.quality.jpeg_artifact_score)
    assert first.thumbnail_size == (256, 64)
    assert len(first.thumbnail_key) == 64
    assert not hasattr(first, "thumbnail_png")


def test_feature_extraction_does_not_encode_thumbnail_png(tmp_path, monkeypatch):
    path = tmp_path / "source.png"
    Image.new("RGB", (400, 100), (10, 20, 30)).save(path)

    def unexpected_save(*_args, **_kwargs):
        raise AssertionError("feature extraction must not encode a display thumbnail")

    monkeypatch.setattr(Image.Image, "save", unexpected_save)

    features = _decode(path)

    assert features.thumbnail_size == (256, 64)
    assert len(features.thumbnail_key) == 64


def test_exif_orientation_is_applied_before_every_feature(tmp_path):
    source = Image.new("RGB", (3, 2))
    source.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (0, 255, 255),
            (255, 0, 255),
        ]
    )
    oriented_path = tmp_path / "oriented.png"
    expected_path = tmp_path / "expected.png"
    exif = Image.Exif()
    exif[274] = 6
    source.save(oriented_path, exif=exif)
    source.transpose(Image.Transpose.ROTATE_270).save(expected_path)

    oriented = _decode(oriented_path)
    expected = _decode(expected_path)

    assert oriented.dimensions == (2, 3)
    assert oriented.blocks == expected.blocks
    assert oriented.phashes == expected.phashes
    assert oriented.dhashes == expected.dhashes
    assert oriented.color_histogram == expected.color_histogram
    assert oriented.thumbnail_key == expected.thumbnail_key


def test_embedded_srgb_profile_and_assumed_srgb_produce_same_pixels(tmp_path):
    untagged_path = tmp_path / "untagged.png"
    tagged_path = tmp_path / "tagged.png"
    image = Image.new("RGB", (12, 8), (23, 91, 177))
    image.save(untagged_path)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(tagged_path, icc_profile=profile)

    untagged = _decode(untagged_path)
    tagged = _decode(tagged_path)

    assert tagged.blocks == untagged.blocks
    assert tagged.phashes == untagged.phashes
    assert tagged.thumbnail_key == untagged.thumbnail_key


def test_lab_icc_profile_is_converted_to_srgb(tmp_path):
    tagged_path = tmp_path / "lab.tiff"
    expected_path = tmp_path / "expected.png"
    lab_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
    srgb_profile = ImageCms.createProfile("sRGB")
    lab = Image.new("LAB", (9, 7), (128, 140, 100))
    lab.save(tagged_path, icc_profile=lab_profile.tobytes())
    ImageCms.profileToProfile(
        lab,
        lab_profile,
        srgb_profile,
        renderingIntent=0,
        outputMode="RGB",
        inPlace=False,
    ).save(expected_path)

    tagged = _decode(tagged_path)
    expected = _decode(expected_path)

    assert tagged.blocks == expected.blocks
    assert tagged.phashes == expected.phashes
    assert tagged.thumbnail_key == expected.thumbnail_key


def test_invalid_icc_profile_is_a_decode_failure_not_a_silent_fallback(tmp_path):
    path = tmp_path / "bad-profile.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path, icc_profile=b"not-an-icc-profile")

    with pytest.raises(ImageDecodeError, match="ICC"):
        _decode(path)


def test_alpha_is_composited_on_white_and_hidden_rgb_does_not_change_features(tmp_path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(first_path)
    Image.new("RGBA", (8, 8), (0, 0, 255, 0)).save(second_path)

    first = _decode(first_path)
    second = _decode(second_path)

    assert first.blocks == second.blocks
    assert set(first.blocks[0]) == {(255, 255, 255)}
    assert first.phashes == second.phashes
    assert first.thumbnail_key == second.thumbnail_key


def test_animated_image_uses_only_first_frame(tmp_path):
    animated_path = tmp_path / "animated.gif"
    first_frame_path = tmp_path / "first-frame.png"
    first = Image.new("RGB", (10, 6), (200, 10, 20))
    second = Image.new("RGB", (10, 6), (20, 10, 200))
    first.save(animated_path, save_all=True, append_images=[second], duration=100, loop=0)
    first.save(first_frame_path)

    animated = _decode(animated_path)
    static = _decode(first_frame_path)

    assert animated.frame_count == 2
    assert animated.blocks == static.blocks
    assert animated.phashes == static.phashes
    assert animated.thumbnail_key == static.thumbnail_key


def test_rotated_mode_emits_all_eight_aligned_orientations(tmp_path):
    path = tmp_path / "asymmetric.png"
    image = Image.new("RGB", (9, 6), (0, 0, 0))
    for x in range(5):
        for y in range(2):
            image.putpixel((x, y), (255, 127, 3))
    image.save(path)

    features = _decode(path, rotated=True)

    assert features.orientation_count == 8
    assert len(features.blocks) == len(features.phashes) == 8
    assert len(features.dhashes) == 8
    assert len(set(features.phashes)) > 1

    transforms = (
        None,
        Image.Transpose.FLIP_LEFT_RIGHT,
        Image.Transpose.ROTATE_180,
        Image.Transpose.FLIP_TOP_BOTTOM,
        Image.Transpose.TRANSPOSE,
        Image.Transpose.ROTATE_270,
        Image.Transpose.TRANSVERSE,
        Image.Transpose.ROTATE_90,
    )
    for index, transform in enumerate(transforms):
        manual = image.copy() if transform is None else image.transpose(transform)
        try:
            manual_path = tmp_path / "manual-{}.png".format(index)
            manual.save(manual_path)
        finally:
            manual.close()
        expected = _decode(manual_path)
        assert features.blocks[index] == expected.blocks[0]
        assert features.phashes[index] == expected.phashes[0]
        assert features.dhashes[index] == expected.dhashes[0]


def test_64mp_orientation_pipeline_bounds_live_full_resolution_images(
    monkeypatch,
):
    tracker = _LiveImageTracker()
    base = _TrackedFullResolutionImage(
        tracker,
        (8_000, 8_000),
        kind="base",
    )
    seen = []

    def tracked_blocks(image, block_count):
        assert block_count == 15
        seen.append(image.label)
        return ((image.serial % 256, 0, 0),)

    monkeypatch.setattr(
        image_features,
        "getblocks2",
        tracked_blocks,
    )

    required = image_features._required_feature_working_bytes(base.size)
    assert required == 576_000_000
    assert required == DEFAULT_MAX_FEATURE_WORKING_BYTES
    assert (
        image_features._require_feature_working_budget(
            base.size,
            required,
        )
        == required
    )
    with pytest.raises(
        ImageResourceLimitError,
        match="working set",
    ):
        image_features._require_feature_working_budget(
            base.size,
            required - 1,
        )

    blocks, phashes, dhashes = image_features._extract_orientation_features(
        base,
        15,
        True,
        _TRACKED_IMAGE_MODULE,
    )

    assert len(blocks) == len(phashes) == len(dhashes) == 8
    assert seen == [
        "base-1",
        "flip_left_right",
        "rotate_180",
        "flip_top_bottom",
        "transpose",
        "rotate_270",
        "transverse",
        "rotate_90",
    ]
    assert tracker.transforms == seen[1:]
    assert tracker.peak_full_resolution == MAX_LIVE_FULL_RESOLUTION_IMAGES == 3
    assert tracker.created["rgb"] == tracker.closed["rgb"] == 7
    assert tracker.created["gray"] == tracker.closed["gray"] == 16
    assert tracker.small_created == tracker.small_closed == 16
    assert tracker.live_full_resolution == 1
    base.close()
    assert tracker.live_full_resolution == 0


def test_64mp_tile_pipeline_streams_and_closes_each_crop(
    monkeypatch,
):
    tracker = _LiveImageTracker()
    base = _TrackedFullResolutionImage(
        tracker,
        (8_000, 8_000),
        kind="base",
    )

    def tracked_content_crop(image, _image_module):
        tile = image.crop((800, 800, 7_200, 7_200))
        return tile, (1_000, 1_000, 9_000, 9_000)

    monkeypatch.setattr(
        image_features,
        "_content_crop",
        tracked_content_crop,
    )

    fingerprints = image_features._tile_fingerprints(
        base,
        _TRACKED_IMAGE_MODULE,
    )

    # Uniform fake samples intentionally deduplicate to one fingerprint, but
    # all four candidates still pass through the streamed close boundary.
    assert len(fingerprints) == 1
    assert tracker.peak_full_resolution == MAX_LIVE_FULL_RESOLUTION_IMAGES == 3
    assert tracker.created["rgb"] == tracker.closed["rgb"] == 4
    assert tracker.created["gray"] == tracker.closed["gray"] == 8
    assert tracker.small_created == tracker.small_closed == 8
    assert tracker.live_full_resolution == 1
    base.close()
    assert tracker.live_full_resolution == 0


def test_configured_pixel_limit_is_reported_as_resource_limit(tmp_path):
    path = tmp_path / "large-for-test.png"
    Image.new("RGB", (11, 10), (1, 2, 3)).save(path)

    with pytest.raises(ImageResourceLimitError, match="configured limit"):
        _decode(path, max_pixels=100)


def test_configured_working_byte_limit_is_checked_before_features(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "working-set.png"
    Image.new("RGB", (11, 10), (1, 2, 3)).save(path)
    feature_calculation_started = False

    def unexpected_feature_calculation(*_args, **_kwargs):
        nonlocal feature_calculation_started
        feature_calculation_started = True
        raise AssertionError("feature extraction must not start")

    monkeypatch.setattr(
        image_features,
        "_extract_orientation_features",
        unexpected_feature_calculation,
    )

    with pytest.raises(ImageResourceLimitError, match="working set"):
        _decode(
            path,
            max_working_bytes=11 * 10 * 9 - 1,
        )

    assert not feature_calculation_started


def test_quality_reader_preserves_source_bit_depth_and_bounds_jpeg_score(tmp_path):
    sixteen_bit = tmp_path / "sixteen-bit.png"
    jpeg = tmp_path / "compressed.jpg"
    Image.new("I;16", (24, 24), 4096).save(sixteen_bit)
    image = Image.new("RGB", (64, 64))
    image.putdata(
        [
            (
                (x * 17 + y * 5) % 256,
                (x * 3 + y * 19) % 256,
                (x * y) % 256,
            )
            for y in range(64)
            for x in range(64)
        ]
    )
    image.save(jpeg, quality=20)

    depth = read_image_quality(sixteen_bit)
    jpeg_quality = read_image_quality(jpeg)

    assert depth.bit_depth == 16
    assert depth.jpeg_artifact_score == 0
    assert 0 <= jpeg_quality.jpeg_artifact_score <= 1
    assert math.isfinite(jpeg_quality.jpeg_artifact_score)
