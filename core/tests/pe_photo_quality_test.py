import math

from PIL import Image

from core.pe.photo import Photo


def test_photo_lazily_hydrates_bounded_quality_metadata(tmp_path):
    path = tmp_path / "metadata.jpg"
    exif = Image.Exif()
    exif[271] = "dupeGuru test camera"
    exif[272] = "quality fixture"
    Image.new("RGB", (64, 48), (40, 90, 170)).save(
        path,
        quality=35,
        exif=exif,
    )

    photo = Photo(path)

    assert photo.bit_depth == 8
    assert photo.exif_count == 2
    assert photo.metadata_count >= photo.exif_count
    assert 0 <= photo.jpeg_artifact_score <= 1
    assert math.isfinite(photo.jpeg_artifact_score)


def test_photo_reports_original_sixteen_bit_source_depth(tmp_path):
    path = tmp_path / "sixteen-bit.png"
    Image.new("I;16", (24, 18), 4096).save(path)

    photo = Photo(path)

    assert photo.bit_depth == 16
    assert photo.jpeg_artifact_score == 0
