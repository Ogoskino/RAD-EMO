#!/usr/bin/env python3
"""Classify RGB/thermal frames against supplied reconstructed recording intervals.

Uses the original timestamp parser and uncertainty classification functions from
reference/build_active_intervals_v1.py. Reads acquisition CSVs and frame filenames,
never image pixels. It neither reconstructs clocks nor reproduces paper models.
Input intervals must use the same millisecond clock as the acquisition tables.
All outputs go to a new directory; raw inputs are never edited.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

sys.dont_write_bytecode = True
from reference import build_active_intervals_v1 as original

AVAILABLE = {
    "available_provisional",
    "available_provisional_rgb_timestamp_recovered_exploratory",
}
JOIN_FIELDS = ("participant_id", "condition_id", "condition_code")
PROVENANCE_FIELDS = (
    "availability_status", "nominal_start_epoch_ms", "nominal_end_epoch_ms",
    "core_exists", "alignment_source", "alignment_strategy",
    "absolute_epoch_status", "uncertainty_model", "uncertainty_ms",
    "hardware_synchronised",
)
FRAME_FIELDS = [
    "session_id", "window_id", *JOIN_FIELDS, "modality", "frame_number",
    "frame_filename", "timestamp_record_status", "timestamp_epoch_ms",
    "timestamp_source_line", "timestamp_source_file", "timestamp_source_sha256",
    "relative_to_nominal_start_ms", "primary_membership", "uncertainty_membership",
    "recommended_training_class", "condition_label_applicability", *PROVENANCE_FIELDS,
]
SUMMARY_FIELDS = [
    "session_id", "window_id", "modality", "physical_frame_count", "stream_status",
    "active_nominal", "boundary_candidate", "outside_trial", "timing_unusable",
    "interval_unavailable", "definite_active", "possible_active", "definite_outside",
    "unknown", "active_core",
]
ISSUE_FIELDS = [
    "session_id", "modality", "source_relpath", "source_line_number", "frame_number",
    "issue_code", "valid_alternative_record_present", "frame_file_present",
    "disposition", "record_sha256",
]


class AlignmentError(ValueError):
    """Invalid supplied inputs; no inferred replacement timestamps are generated."""


def read_rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required <= set(reader.fieldnames or []):
            raise AlignmentError(f"{path.name} lacks fields: {sorted(required - set(reader.fieldnames or []))}")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise AlignmentError(f"Malformed CSV record in {path.name}, line {reader.line_num}")
            rows.append({key: value.strip() for key, value in row.items()})
        return rows


def finite(value: str, field: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise AlignmentError(f"Invalid {field}; a finite supplied value is required.") from None
    if not math.isfinite(number):
        raise AlignmentError(f"Invalid {field}; a finite supplied value is required.")
    return number


def load_intervals(path: Path, sessions_path: Path | None) -> dict[str, list[dict[str, Any]]]:
    rows = read_rows(path, {"session_id", "availability_status"})
    if not rows:
        raise AlignmentError("The supplied interval table is empty.")
    sessions = {}
    if sessions_path is not None:
        for session in read_rows(sessions_path, {"session_id"}):
            sid = session["session_id"]
            if not sid or sid in sessions:
                raise AlignmentError("Sessions table contains an empty or duplicate session identifier.")
            sessions[sid] = session
    grouped: dict[str, list[dict[str, Any]]] = {}
    keys = set()
    for row in rows:
        sid = row["session_id"]
        window = row.get("window_id", "") or "recording"
        if not sid or (sid, window) in keys:
            raise AlignmentError("Intervals contain an empty identifier or duplicate session/window.")
        keys.add((sid, window))
        row["window_id"] = window
        if sessions_path is not None:
            if sid not in sessions:
                raise AlignmentError(f"Interval session {sid} does not join the supplied sessions table.")
            for field in JOIN_FIELDS:
                expected = sessions[sid].get(field, "")
                observed = row.get(field, "")
                if expected and observed and expected != observed:
                    raise AlignmentError(f"Conflicting {field} for session {sid}.")
                row[field] = observed or expected
        status = row["availability_status"]
        if status.startswith("available") and status not in AVAILABLE:
            raise AlignmentError(f"Unsupported available interval status for {sid}: {status}")
        if status in AVAILABLE:
            start = finite(row.get("nominal_start_epoch_ms", ""), "nominal_start_epoch_ms")
            end = finite(row.get("nominal_end_epoch_ms", ""), "nominal_end_epoch_ms")
            uncertainty = finite(row.get("uncertainty_ms", ""), "uncertainty_ms")
            if end < start or uncertainty < 0:
                raise AlignmentError("Interval end precedes start or uncertainty is negative.")
            if row.get("uncertainty_model") != "shared_additive_session_offset":
                raise AlignmentError("Available intervals must declare shared_additive_session_offset uncertainty.")
            if not row.get("alignment_source"):
                raise AlignmentError("Available intervals must retain their supplied alignment_source.")
            core = int(end - start > 2 * uncertainty)
            if row.get("core_exists", "") not in {"", str(core)}:
                raise AlignmentError("Supplied core_exists contradicts the interval bounds and uncertainty.")
            row["core_exists"] = core
            # Normalise only the adapter passed to the unchanged original function.
            # The source status and reconstruction fields are preserved in outputs.
        grouped.setdefault(sid, []).append(row)
    return grouped


def resolve_from(base: Path, value: str) -> Path:
    if not value:
        raise AlignmentError("A stream input path is empty.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def run(intervals_path: Path, streams_path: Path, output_dir: Path,
        sessions_path: Path | None = None) -> dict[str, Any]:
    intervals_path, streams_path = Path(intervals_path).resolve(), Path(streams_path).resolve()
    sessions_path = Path(sessions_path).resolve() if sessions_path is not None else None
    output_arg = Path(output_dir).expanduser()
    if output_arg.is_symlink() or os.path.lexists(output_arg):
        raise AlignmentError("Output directory must be new; existing outputs are never overwritten.")
    output = output_arg.resolve()
    intervals = load_intervals(intervals_path, sessions_path)
    streams = read_rows(streams_path, {"session_id", "modality", "acquisition_csv", "frames_dir"})
    if not streams:
        raise AlignmentError("The stream index is empty.")
    keys = set()
    for stream in streams:
        sid, kind = stream["session_id"], stream["modality"]
        if sid not in intervals:
            raise AlignmentError(f"Stream session {sid} has no supplied interval.")
        if kind not in {"rgb", "thermal"} or (sid, kind) in keys:
            raise AlignmentError("Stream modality is unsupported or a session/modality is duplicated.")
        keys.add((sid, kind))
        acquisition = resolve_from(streams_path.parent, stream["acquisition_csv"])
        frames = resolve_from(streams_path.parent, stream["frames_dir"])
        if not acquisition.is_file() or not frames.is_dir():
            raise AlignmentError("An acquisition table or frame directory is unavailable.")
        if inside(output, frames) or inside(output, acquisition.parent):
            raise AlignmentError("Outputs cannot be written inside a raw acquisition directory.")
        stream["acquisition_path"], stream["frames_path"] = acquisition, frames
    source_bindings = [
        {"role": "intervals", "file": intervals_path.name, "sha256": original.sha256_file(intervals_path)},
        {"role": "stream_index", "file": streams_path.name, "sha256": original.sha256_file(streams_path)},
    ]
    if sessions_path is not None:
        source_bindings.append({"role": "sessions", "file": sessions_path.name,
                                "sha256": original.sha256_file(sessions_path)})
    counts = Counter()
    issue_count = 0
    summary_count = 0
    physical_count = 0
    stream_bindings = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rad_emo_alignment_", dir=output.parent) as temporary:
        stage = Path(temporary)
        with (stage / "frame_membership.csv").open("w", encoding="utf-8", newline="") as frame_file, \
                (stage / "frame_summary.csv").open("w", encoding="utf-8", newline="") as summary_file, \
                (stage / "timestamp_issues.csv").open("w", encoding="utf-8", newline="") as issue_file:
            frame_writer = csv.DictWriter(frame_file, FRAME_FIELDS, lineterminator="\n")
            summary_writer = csv.DictWriter(summary_file, SUMMARY_FIELDS, lineterminator="\n")
            issue_writer = csv.DictWriter(issue_file, ISSUE_FIELDS, lineterminator="\n")
            for writer in (frame_writer, summary_writer, issue_writer):
                writer.writeheader()
            for stream in streams:
                sid, kind = stream["session_id"], stream["modality"]
                pattern = original.RGB_FRAME_RE if kind == "rgb" else original.THERMAL_FRAME_RE
                frames = original.enumerate_frames(stream["frames_path"], pattern)
                if not frames:
                    raise AlignmentError(f"No recognised {kind} frame filenames for {sid}.")
                physical_count += len(frames)
                source = stream["acquisition_path"]
                source_sha = original.sha256_file(source)
                parsed = original.parse_sensor_csv(source, kind, sid, source.name, set(frames))
                if original.sha256_file(source) != source_sha:
                    raise AlignmentError("An acquisition table changed during parsing; no output was committed.")
                for issue in parsed.issues:
                    issue_writer.writerow(issue)
                    issue_count += 1
                stream_bindings.append({"session_id": sid, "modality": kind, "file": source.name,
                                        "sha256": source_sha, "physical_frame_count": len(frames),
                                        "stream_status": parsed.stream_status})
                for interval in intervals[sid]:
                    classifier_interval = dict(interval)
                    if interval["availability_status"] in AVAILABLE:
                        classifier_interval["availability_status"] = "available_provisional"
                    per = Counter()
                    for number, (filename, _) in sorted(frames.items()):
                        record = parsed.valid_by_frame.get(number)
                        ambiguous = number in parsed.ambiguous_frames
                        timing = ("session_timestamp_stream_invalid" if parsed.stream_status != "valid"
                                  else "ambiguous_duplicate_valid_records" if ambiguous
                                  else "missing_csv_record" if record is None else "valid")
                        primary, uncertainty, recommended, label, relative = original.classify_sensor_frame(
                            classifier_interval, parsed.stream_status, record, ambiguous
                        )
                        per.update((primary, uncertainty, recommended))
                        counts[primary] += 1
                        frame_writer.writerow({
                            "session_id": sid, "window_id": interval["window_id"],
                            **{field: interval.get(field, "") for field in JOIN_FIELDS},
                            "modality": kind, "frame_number": number, "frame_filename": filename,
                            "timestamp_record_status": timing,
                            "timestamp_epoch_ms": "" if record is None else original.clean_float(record.timestamp_ms),
                            "timestamp_source_line": "" if record is None else record.source_line,
                            "timestamp_source_file": source.name, "timestamp_source_sha256": source_sha,
                            "relative_to_nominal_start_ms": relative, "primary_membership": primary,
                            "uncertainty_membership": uncertainty, "recommended_training_class": recommended,
                            "condition_label_applicability": label,
                            **{field: interval.get(field, "") for field in PROVENANCE_FIELDS},
                        })
                    summary_writer.writerow({
                        "session_id": sid, "window_id": interval["window_id"], "modality": kind,
                        "physical_frame_count": len(frames), "stream_status": parsed.stream_status,
                        **{field: per[field] for field in SUMMARY_FIELDS[5:]},
                    })
                    summary_count += 1
        manifest = {
            "schema": "rad_emo_portable_frame_alignment_v1", "physical_frame_count": physical_count,
            "frame_window_rows": sum(counts.values()), "stream_count": len(streams),
            "session_window_modality_rows": summary_count, "timestamp_issue_count": issue_count,
            "primary_membership": dict(counts), "inputs": source_bindings, "streams": stream_bindings,
            "intervals_reconstructed_by_this_tool": False, "image_payloads_opened": False,
            "raw_inputs_modified": False, "sensor_resampling_performed": False,
            "uncertainty_model": "shared_additive_session_offset",
            "interval_endpoints": "inclusive; core exists only if duration > 2*uncertainty",
            "window_note": "Multiple supplied windows repeat frame rows; physical frames are counted once per stream.",
            "scope": "Metadata-only classification against supplied approximate intervals; no hardware-sync or model-reproduction claim.",
            "code": {"align_frames_sha256": original.sha256_file(Path(__file__)),
                     "original_reference_sha256": original.sha256_file(Path(original.__file__))},
        }
        manifest["outputs"] = [{"file": name, "sha256": original.sha256_file(stage / name)}
                               for name in ("frame_membership.csv", "frame_summary.csv", "timestamp_issues.csv")]
        (stage / "build_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # Refuse to replace a directory created concurrently after the initial check.
        if os.path.lexists(output):
            raise AlignmentError("Output directory appeared during processing; it was not replaced.")
        stage.rename(output)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intervals", type=Path, required=True, help="Supplied active_intervals.csv; millisecond bounds and uncertainties.")
    parser.add_argument("--streams", type=Path, required=True, help="CSV: session_id,modality,acquisition_csv,frames_dir; paths relative to this CSV.")
    parser.add_argument("--sessions", type=Path, help="Optional supplied sessions.csv for exact identifier/condition joins.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New output directory, outside raw acquisition directories.")
    args = parser.parse_args(argv)
    try:
        manifest = run(args.intervals, args.streams, args.output_dir, args.sessions)
    except (AlignmentError, original.BuildError, OSError, csv.Error) as error:
        print(f"ALIGNMENT_FAILED: {error}", file=sys.stderr)
        return 2
    print(json.dumps({key: manifest[key] for key in ("physical_frame_count", "frame_window_rows", "stream_count", "primary_membership")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
