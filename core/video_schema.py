# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Dependency-free schema identifiers shared by video core and service layers."""

VIDEO_LIBRARY_SCHEMA_VERSION = 1
VIDEO_LIBRARY_SCAN_SCHEMA = "dupeguru.video-library-scan"
VIDEO_LIBRARY_GROUP_SCHEMA = "dupeguru.video-library-group"
VIDEO_LIBRARY_RECORD_SCHEMA = "dupeguru.video-library-record"

__all__ = [
    "VIDEO_LIBRARY_GROUP_SCHEMA",
    "VIDEO_LIBRARY_RECORD_SCHEMA",
    "VIDEO_LIBRARY_SCAN_SCHEMA",
    "VIDEO_LIBRARY_SCHEMA_VERSION",
]
