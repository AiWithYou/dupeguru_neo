# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from hscommon.trans import tr

from core.scanner import Scanner, ScanType, ScanOption

from core.pe import matchblock, matchexif


class ScannerPE(Scanner):
    cache_path = None
    match_scaled = False
    match_rotated = False
    phash_distance = matchblock.DEFAULT_PHASH_DISTANCE
    dhash_distance = matchblock.DEFAULT_DHASH_DISTANCE
    color_histogram_distance = matchblock.DEFAULT_COLOR_HISTOGRAM_DISTANCE
    max_candidate_pairs = matchblock.DEFAULT_MAX_CANDIDATE_PAIRS
    max_refined_pairs = matchblock.DEFAULT_MAX_REFINED_PAIRS
    max_matches = matchblock.DEFAULT_MAX_MATCHES
    scan_receipt = None
    candidate_stats = None

    @staticmethod
    def get_scan_options():
        return [
            ScanOption(ScanType.FUZZYBLOCK, tr("Contents")),
            ScanOption(ScanType.EXIFTIMESTAMP, tr("EXIF Timestamp")),
        ]

    def _getmatches(self, files, j):
        self.scan_receipt = None
        self.candidate_stats = None
        if self.scan_type == ScanType.FUZZYBLOCK:
            result = matchblock.getmatches(
                files,
                cache_path=self.cache_path,
                threshold=self.min_match_percentage,
                match_scaled=self.match_scaled,
                match_rotated=self.match_rotated,
                j=j,
                phash_distance=self.phash_distance,
                dhash_distance=self.dhash_distance,
                color_histogram_distance=self.color_histogram_distance,
                max_candidate_pairs=self.max_candidate_pairs,
                max_refined_pairs=self.max_refined_pairs,
                max_matches=self.max_matches,
            )
            self.scan_receipt = result.scan_receipt
            self.candidate_stats = result.candidate_stats
            return result
        elif self.scan_type == ScanType.EXIFTIMESTAMP:
            return matchexif.getmatches(files, self.match_scaled, j)
        else:
            raise ValueError("Invalid scan type")
