"""Offline contract tests using only synthetic acquisition metadata and images."""

from __future__ import annotations

import builtins
from collections import Counter
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import align_frames


INTERVAL_FIELDS = (
    "session_id", "window_id", "participant_id", "condition_id", "condition_code",
    "availability_status", "nominal_start_epoch_ms", "nominal_end_epoch_ms",
    "uncertainty_ms", "uncertainty_model", "alignment_source", "alignment_strategy",
    "hardware_synchronised",
)
STREAM_FIELDS = ("session_id", "modality", "acquisition_csv", "frames_dir")
SESSION_FIELDS = ("session_id", "participant_id", "condition_id", "condition_code")
TIMES = (0, 500, 1000, 1500, 2500, 3000, 3500, 4000)


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic_frame_alignment_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "inputs"
        self.intervals = self.inputs / "intervals.csv"
        self.streams = self.inputs / "streams.csv"
        self.sessions = self.inputs / "sessions.csv"
        self.output = self.root / "aligned"
        self.interval_rows = [self.interval()]
        self.stream_rows = []
        self.stream_serial = 0
        self.write_intervals()
        write_csv(self.sessions, SESSION_FIELDS, [{
            "session_id": "P1S1", "participant_id": "P1",
            "condition_id": "C01", "condition_code": "PW",
        }])
        self.add_stream()

    @staticmethod
    def interval(**changes):
        row = {
            "session_id": "P1S1", "window_id": "W1", "participant_id": "P1",
            "condition_id": "C01", "condition_code": "PW",
            "availability_status": "available_provisional",
            "nominal_start_epoch_ms": "1000", "nominal_end_epoch_ms": "3000",
            "uncertainty_ms": "500",
            "uncertainty_model": "shared_additive_session_offset",
            "alignment_source": "synthetic_manual_protocol_anchor",
            "alignment_strategy": "synthetic_shared_session_offset",
            "hardware_synchronised": "false",
        }
        row.update(changes)
        return row

    def write_intervals(self, fields=INTERVAL_FIELDS):
        write_csv(self.intervals, fields, self.interval_rows)

    def write_streams(self):
        write_csv(self.streams, STREAM_FIELDS, self.stream_rows)

    def add_stream(self, modality="rgb", samples=None, image_numbers=None):
        samples = list(enumerate(TIMES, 1)) if samples is None else samples
        image_numbers = [number for number, _ in samples] if image_numbers is None else image_numbers
        name = modality if self.stream_serial == 0 else f"{modality}_{self.stream_serial}"
        self.stream_serial += 1
        directory = self.inputs / "raw" / name
        frames = directory / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        prefix = "color" if modality == "rgb" else "thermal"
        for number in set(image_numbers):
            # Deliberately not a decodable PNG: alignment needs names, not pixels.
            (frames / f"{prefix}_frame_{number}.png").write_bytes(b"synthetic-image-not-a-png")
        acquisition = directory / "acquisition.csv"
        timestamp_field = "Color Timestamp (ms)" if modality == "rgb" else "Timestamp (ms)"
        fields = ["Frame No", timestamp_field]
        if modality == "rgb":
            fields.append("Pulse Sensor Value")
        rows = []
        for number, timestamp in samples:
            row = {"Frame No": number, timestamp_field: timestamp}
            if modality == "rgb":
                row["Pulse Sensor Value"] = "123"
            rows.append(row)
        write_csv(acquisition, fields, rows)
        self.stream_rows.append({
            "session_id": "P1S1", "modality": modality,
            "acquisition_csv": acquisition.relative_to(self.inputs).as_posix(),
            "frames_dir": frames.relative_to(self.inputs).as_posix(),
        })
        self.write_streams()
        return acquisition, frames

    def run_alignment(self, **kwargs):
        return align_frames.run(self.intervals, self.streams, self.output, **kwargs)

    def membership(self):
        return read_csv(self.output / "frame_membership.csv")

    def assert_rejected_without_output(self, **kwargs):
        with self.assertRaises(ValueError):
            self.run_alignment(**kwargs)
        self.assertFalse(self.output.exists(), "validation must finish before output creation")

    def source_snapshot(self):
        return {
            path.relative_to(self.inputs).as_posix(): (
                hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
            )
            for path in self.inputs.rglob("*") if path.is_file()
        }

    def test_recovered_interval_status_preserves_source_and_uncertainty(self):
        status = "available_provisional_rgb_timestamp_recovered_exploratory"
        self.interval_rows[0]["availability_status"] = status
        self.write_intervals()
        self.run_alignment()
        rows = self.membership()
        self.assertEqual(Counter(row["uncertainty_membership"] for row in rows),
                         {"definite_active": 2, "possible_active": 4, "definite_outside": 2})
        self.assertTrue(all(row["availability_status"] == status for row in rows))
        self.assertTrue(all(row["uncertainty_ms"] == "500" for row in rows))
        self.assertTrue(all(row["alignment_source"] == "synthetic_manual_protocol_anchor" for row in rows))

    def test_unavailable_interval_does_not_invent_bounds_or_assign_labels(self):
        self.interval_rows[0].update(availability_status="pending_radar_timestamps",
                                     nominal_start_epoch_ms="", nominal_end_epoch_ms="",
                                     uncertainty_ms="")
        self.write_intervals()
        summary = self.run_alignment()
        rows = self.membership()
        self.assertEqual(summary["primary_membership"], {"interval_unavailable": 8})
        self.assertTrue(all(row["uncertainty_membership"] == "unknown" for row in rows))
        self.assertTrue(all(row["recommended_training_class"] == "pending_interval" for row in rows))
        self.assertTrue(all(row["condition_label_applicability"] == "pending_interval_do_not_assign" for row in rows))
        self.assertTrue(all(row["nominal_start_epoch_ms"] == row["relative_to_nominal_start_ms"] == "" for row in rows))

    def test_nominal_and_uncertainty_membership_use_inclusive_endpoints(self):
        summary = self.run_alignment(sessions_path=self.sessions)
        self.assertIsInstance(summary, dict)
        rows = self.membership()
        self.assertEqual(len(rows), 8)
        by_frame = {int(row["frame_number"]): row for row in rows}
        expected = (
            ("outside_trial", "definite_outside"),
            ("boundary_candidate", "possible_active"),
            ("active_nominal", "possible_active"),
            ("active_nominal", "definite_active"),
            ("active_nominal", "definite_active"),
            ("active_nominal", "possible_active"),
            ("boundary_candidate", "possible_active"),
            ("outside_trial", "definite_outside"),
        )
        training = ("exclude_outside", "exclude_boundary", "exclude_boundary", "active_core",
                    "active_core", "exclude_boundary", "exclude_boundary", "exclude_outside")
        for number, memberships in enumerate(expected, 1):
            with self.subTest(frame=number):
                row = by_frame[number]
                self.assertEqual((row["primary_membership"], row["uncertainty_membership"]), memberships)
                self.assertEqual(row["session_id"], "P1S1")
                self.assertEqual(row["window_id"], "W1")
                self.assertEqual(row["modality"], "rgb")
                self.assertEqual(row["alignment_source"], "synthetic_manual_protocol_anchor")
                self.assertEqual(float(row["uncertainty_ms"]), 500)
                self.assertEqual(row["recommended_training_class"], training[number - 1])
                self.assertTrue(row["condition_label_applicability"])
        self.assertEqual(Counter(row["primary_membership"] for row in rows),
                         {"active_nominal": 4, "boundary_candidate": 2, "outside_trial": 2})
        self.assertEqual(Counter(row["uncertainty_membership"] for row in rows),
                         {"definite_active": 2, "possible_active": 4, "definite_outside": 2})
        self.assertEqual(summary["physical_frame_count"], 8)
        self.assertEqual(summary["frame_window_rows"], 8)
        self.assertEqual(summary["stream_count"], 1)
        self.assertEqual(summary["primary_membership"],
                         {"active_nominal": 4, "boundary_candidate": 2, "outside_trial": 2})
        summaries = read_csv(self.output / "frame_summary.csv")
        self.assertEqual(len(summaries), 1)
        for key, count in {"physical_frame_count": 8, "active_nominal": 4,
                           "boundary_candidate": 2, "outside_trial": 2,
                           "definite_active": 2, "possible_active": 4,
                           "definite_outside": 2, "active_core": 2}.items():
            self.assertEqual(int(summaries[0][key]), count, key)
        self.assertEqual(read_csv(self.output / "timestamp_issues.csv"), [])
        manifest = json.loads((self.output / "build_manifest.json").read_text(encoding="utf-8"))
        self.assertIsInstance(manifest, dict)

    def test_distinct_windows_are_retained_and_equal_twice_uncertainty_has_no_core(self):
        self.interval_rows.append(self.interval(
            window_id="W2", nominal_start_epoch_ms="2000", nominal_end_epoch_ms="4000",
            uncertainty_ms="1000",
        ))
        self.write_intervals()
        self.run_alignment()
        rows = self.membership()
        self.assertEqual(Counter(row["window_id"] for row in rows), {"W1": 8, "W2": 8})
        second = [row for row in rows if row["window_id"] == "W2"]
        self.assertNotIn("definite_active", {row["uncertainty_membership"] for row in second})
        self.assertEqual(Counter(row["uncertainty_membership"] for row in second),
                         {"possible_active": 6, "definite_outside": 2})

    def test_both_original_acquisition_schemas_and_relative_paths_are_supported(self):
        self.add_stream("thermal")
        self.interval_rows[0]["availability_status"] = "available_provisional_rgb_timestamp_recovered_exploratory"
        self.write_intervals()
        self.run_alignment()
        rows = self.membership()
        self.assertEqual(Counter(row["modality"] for row in rows), {"rgb": 8, "thermal": 8})
        for modality in ("rgb", "thermal"):
            self.assertEqual(Counter(row["primary_membership"] for row in rows if row["modality"] == modality),
                             {"active_nominal": 4, "boundary_candidate": 2, "outside_trial": 2})

    def test_frames_join_by_number_instead_of_csv_or_filename_order(self):
        self.stream_rows.clear()
        self.add_stream(samples=[(40, 4000), (10, 500), (20, 1500)], image_numbers=[20, 40, 10])
        self.run_alignment()
        by_frame = {int(row["frame_number"]): row for row in self.membership()}
        self.assertEqual(set(by_frame), {10, 20, 40})
        self.assertEqual(by_frame[10]["primary_membership"], "boundary_candidate")
        self.assertEqual(by_frame[20]["primary_membership"], "active_nominal")
        self.assertEqual(by_frame[40]["primary_membership"], "outside_trial")
        self.assertEqual(float(by_frame[10]["timestamp_epoch_ms"]), 500)
        self.assertEqual(float(by_frame[20]["timestamp_epoch_ms"]), 1500)
        self.assertEqual(float(by_frame[40]["timestamp_epoch_ms"]), 4000)

    def test_invalid_and_duplicate_frame_timestamps_remain_timing_unusable(self):
        self.stream_rows.clear()
        self.add_stream(samples=[(1, 1500), (2, "not-a-time"), (3, 2000), (3, 2500), (4, 3000)],
                        image_numbers=[1, 2, 3, 4])
        self.run_alignment()
        rows = self.membership()
        self.assertEqual({int(row["frame_number"]) for row in rows}, {1, 2, 3, 4})
        for row in rows:
            if int(row["frame_number"]) in {2, 3}:
                self.assertEqual(row["primary_membership"], "timing_unusable")
                self.assertEqual(row["uncertainty_membership"], "unknown")
            else:
                self.assertEqual(row["primary_membership"], "active_nominal")
        self.assertTrue(read_csv(self.output / "timestamp_issues.csv"))

    def test_join_counts_physical_images_and_reports_unmatched_metadata(self):
        self.stream_rows.clear()
        self.add_stream(samples=[(10, 500), (20, 1500), (99, 2500)], image_numbers=[10, 20, 30])
        summary = self.run_alignment()
        rows = self.membership()
        self.assertEqual(summary["physical_frame_count"], 3)
        self.assertEqual(summary["frame_window_rows"], 3)
        self.assertEqual({int(row["frame_number"]) for row in rows}, {10, 20, 30})
        by_frame = {int(row["frame_number"]): row for row in rows}
        self.assertEqual(by_frame[10]["primary_membership"], "boundary_candidate")
        self.assertEqual(by_frame[20]["primary_membership"], "active_nominal")
        self.assertEqual(by_frame[30]["primary_membership"], "timing_unusable")
        self.assertEqual(by_frame[30]["timestamp_record_status"], "missing_csv_record")
        issues = {(row["frame_number"], row["issue_code"])
                  for row in read_csv(self.output / "timestamp_issues.csv")}
        self.assertIn(("99", "timestamp_record_without_frame_file"), issues)
        self.assertIn(("30", "frame_file_without_valid_timestamp_record"), issues)

    def test_nonincreasing_timestamp_stream_retains_all_images_as_timing_unusable(self):
        self.stream_rows.clear()
        self.add_stream(samples=[(1, 1500), (2, 1500), (3, 2500)])
        self.run_alignment()
        rows = self.membership()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["primary_membership"] for row in rows}, {"timing_unusable"})
        self.assertEqual({row["uncertainty_membership"] for row in rows}, {"unknown"})
        self.assertEqual({row["recommended_training_class"] for row in rows}, {"exclude_timing"})
        self.assertIn("timestamp_stream_not_strictly_increasing",
                      {row["issue_code"] for row in read_csv(self.output / "timestamp_issues.csv")})

    def test_malformed_duplicate_preserves_a_unique_valid_alternative(self):
        self.stream_rows.clear()
        self.add_stream(samples=[(1, 1500), (1, "not-a-time"), (2, 2500)])
        self.run_alignment()
        rows = self.membership()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["primary_membership"] for row in rows}, {"active_nominal"})
        issue = next(row for row in read_csv(self.output / "timestamp_issues.csv")
                     if row["issue_code"] == "missing_or_invalid_timestamp")
        self.assertEqual(issue["frame_number"], "1")
        self.assertEqual(issue["valid_alternative_record_present"], "1")
        self.assertEqual(issue["disposition"], "rejected_valid_alternative_retained")

    def test_missing_optional_window_id_is_supported_for_a_single_window(self):
        self.interval_rows[0].pop("window_id")
        self.write_intervals(tuple(field for field in INTERVAL_FIELDS if field != "window_id"))
        self.run_alignment()
        self.assertEqual(len(self.membership()), 8)

    def test_unknown_session_identifier_is_rejected_before_outputs(self):
        self.interval_rows[0]["session_id"] = "not-a-session"
        self.write_intervals()
        self.assert_rejected_without_output(sessions_path=self.sessions)

    def test_stream_without_an_interval_is_rejected_before_outputs(self):
        self.stream_rows[0]["session_id"] = "P1S2"
        self.write_streams()
        self.assert_rejected_without_output()

    def test_identifier_mismatch_is_rejected_before_outputs(self):
        self.interval_rows[0]["participant_id"] = "P2"
        self.write_intervals()
        self.assert_rejected_without_output(sessions_path=self.sessions)

    def test_session_registry_condition_mismatch_is_rejected_before_outputs(self):
        write_csv(self.sessions, SESSION_FIELDS, [{
            "session_id": "P1S1", "participant_id": "P1",
            "condition_id": "C02", "condition_code": "PW",
        }])
        self.assert_rejected_without_output(sessions_path=self.sessions)

    def test_duplicate_window_identifier_is_rejected_before_outputs(self):
        self.interval_rows.append(self.interval(nominal_start_epoch_ms="2000"))
        self.write_intervals()
        self.assert_rejected_without_output()

    def test_unsupported_uncertainty_model_is_rejected_before_outputs(self):
        self.interval_rows[0]["uncertainty_model"] = "independent_frame_noise"
        self.write_intervals()
        self.assert_rejected_without_output()

    def test_image_payloads_are_never_opened_and_inputs_are_unchanged(self):
        before = self.source_snapshot()
        original_builtin_open = builtins.open
        original_io_open = io.open

        def guarded(original):
            def open_without_images(file, *args, **kwargs):
                if isinstance(file, (str, bytes, Path)) and str(file).lower().endswith(".png"):
                    raise AssertionError("alignment attempted to open an image payload")
                return original(file, *args, **kwargs)
            return open_without_images

        with patch("builtins.open", guarded(original_builtin_open)), \
                patch("io.open", guarded(original_io_open)):
            self.run_alignment()
        self.assertEqual(self.source_snapshot(), before)

    def test_existing_output_directory_is_refused_without_changes(self):
        self.output.mkdir()
        sentinel = self.output / "do-not-overwrite.txt"
        sentinel.write_text("existing result", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_alignment()
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing result")

    def test_output_inside_acquisition_or_frame_source_is_refused(self):
        acquisition_parent = self.inputs / "raw" / "rgb"
        for parent in (acquisition_parent, acquisition_parent / "frames"):
            with self.subTest(parent=parent):
                destination = parent / "generated_alignment"
                with self.assertRaises(ValueError):
                    align_frames.run(self.intervals, self.streams, destination)
                self.assertFalse(destination.exists())

    def test_cli_returns_success_and_validation_failure_codes(self):
        args = ["--intervals", str(self.intervals), "--streams", str(self.streams),
                "--output-dir", str(self.output)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(align_frames.main(args), 0)
            self.assertEqual(align_frames.main(args), 2)


if __name__ == "__main__":
    unittest.main()
