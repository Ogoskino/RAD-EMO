#!/usr/bin/env python3
"""Build fail-closed RAD-EMO active-interval and frame-membership tables.

This builder reads only directory metadata and CSV/JSON timestamp metadata. It
does not open image, thermal-image, ADC MAT, or other acquisition payloads and
does not write anywhere in the acquisition roots.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


DATASET_VERSION = "1.0.0"
INTERVAL_SCHEMA = "rad_emo_active_intervals_v1"
FRAME_SCHEMA = "rad_emo_frame_membership_v1"
SUMMARY_SCHEMA = "rad_emo_active_interval_summary_v1"
ISSUE_SCHEMA = "rad_emo_timestamp_ingestion_issues_v1"
MANIFEST_SCHEMA = "rad-emo-active-interval-build-manifest-v1"
ALGORITHM_VERSION = "shared_offset_core_v1"
UNCERTAINTY_MODEL = "shared_additive_session_offset"
USE_SCOPE = "approximate_session_window_alignment_exploratory"

RGB_FRAME_RE = re.compile(r"^color_frame_(\d+)\.(?:png|jpg|jpeg)$", re.IGNORECASE)
THERMAL_FRAME_RE = re.compile(
    r"^thermal_frame_(\d+)\.(?:png|jpg|jpeg)$", re.IGNORECASE
)
RADAR_FRAME_RE = re.compile(r"^adcData_frame_(\d+)\.mat$", re.IGNORECASE)
MANIFEST_SESSION_RE = re.compile(r"^P_(\d+)_S_(\d+)$")

EXPECTED_INTERVALS = 114
EXPECTED_AVAILABLE_INTERVALS = 90
EXPECTED_PENDING_INTERVALS = 24
EXPECTED_RGB_FRAMES = 189_835
EXPECTED_THERMAL_FRAMES = 65_367
EXPECTED_RADAR_FRAMES = 39_647
EXPECTED_MALFORMED_SENSOR_ROWS = 9
EXPECTED_IMAGE_WITHOUT_TIMESTAMP = 3

# Independently audited expected partitions for the frozen v1 source snapshot.
EXPECTED_PRIMARY = {
    "rgb": Counter(
        {
            "active_nominal": 75_589,
            "boundary_candidate": 11_643,
            "outside_trial": 59_592,
            "timing_unusable": 1_699,
            "interval_unavailable": 41_312,
        }
    ),
    "thermal": Counter(
        {
            "active_nominal": 27_395,
            "boundary_candidate": 3_897,
            "outside_trial": 20_652,
            "timing_unusable": 0,
            "interval_unavailable": 13_423,
        }
    ),
}
EXPECTED_UNCERTAINTY = {
    "rgb": Counter(
        {
            "definite_active": 62_675,
            "possible_active": 24_557,
            "definite_outside": 59_592,
            "unknown": 43_011,
        }
    ),
    "thermal": Counter(
        {
            "definite_active": 21_602,
            "possible_active": 9_690,
            "definite_outside": 20_652,
            "unknown": 13_423,
        }
    ),
}

INTERVAL_FIELDS = [
    "schema_version",
    "dataset_version",
    "session_id",
    "participant_id",
    "condition_id",
    "condition_code",
    "interval_anchor",
    "availability_status",
    "nominal_start_epoch_ms",
    "nominal_end_epoch_ms",
    "nominal_duration_ms",
    "absolute_epoch_status",
    "alignment_source",
    "alignment_strategy",
    "uncertainty_model",
    "uncertainty_ms",
    "possible_start_epoch_ms",
    "possible_end_epoch_ms",
    "core_start_epoch_ms",
    "core_end_epoch_ms",
    "core_exists",
    "radar_frame_count",
    "radar_first_frame_no",
    "radar_last_frame_no",
    "radar_relative_start_ms",
    "radar_relative_end_ms",
    "radar_timestamp_table_relpath",
    "radar_timestamp_table_sha256",
    "radar_index_source_sha256",
    "timestamp_manifest_sha256",
    "hardware_synchronised",
    "use_scope",
    "interval_qc_status",
    "qc_flags",
    "algorithm_version",
]

SENSOR_FRAME_FIELDS = [
    "schema_version",
    "dataset_version",
    "session_id",
    "participant_id",
    "condition_id",
    "condition_code",
    "modality",
    "frame_number",
    "frame_filename",
    "frame_relpath",
    "timestamp_record_status",
    "timestamp_epoch_ms",
    "timestamp_source_line",
    "timestamp_source_relpath",
    "timestamp_source_sha256",
    "interval_availability",
    "relative_to_nominal_start_ms",
    "primary_membership",
    "uncertainty_membership",
    "recommended_training_class",
    "condition_label_applicability",
    "frame_qc_flags",
]

RADAR_FRAME_FIELDS = [
    "schema_version",
    "dataset_version",
    "session_id",
    "participant_id",
    "condition_id",
    "condition_code",
    "modality",
    "frame_number",
    "frame_filename",
    "frame_relpath",
    "tda_timestamp_us",
    "relative_ms",
    "provisional_epoch_ms",
    "alignment_source",
    "uncertainty_ms",
    "timestamp_source_relpath",
    "timestamp_source_sha256",
    "primary_membership",
    "uncertainty_membership",
    "recommended_training_class",
    "condition_label_applicability",
    "frame_qc_flags",
]

SUMMARY_FIELDS = [
    "schema_version",
    "dataset_version",
    "session_id",
    "modality",
    "physical_frame_count",
    "csv_raw_data_rows",
    "csv_valid_timestamp_rows",
    "csv_malformed_rows",
    "valid_frame_timestamp_mappings",
    "missing_frame_timestamp_mappings",
    "stream_timestamp_status",
    "interval_availability",
    "active_anchor_relative_count",
    "active_nominal_count",
    "boundary_candidate_count",
    "outside_trial_count",
    "timing_unusable_count",
    "interval_unavailable_count",
    "definite_active_count",
    "possible_active_count",
    "definite_outside_count",
    "uncertainty_unknown_count",
    "recommended_training_frame_count",
    "qc_flags",
]

ISSUE_FIELDS = [
    "schema_version",
    "issue_id",
    "session_id",
    "modality",
    "source_relpath",
    "source_line_number",
    "frame_number",
    "issue_code",
    "valid_alternative_record_present",
    "frame_file_present",
    "disposition",
    "record_sha256",
]


class BuildError(RuntimeError):
    """Raised when an input or invariant fails closed."""


@dataclass(frozen=True)
class Session:
    session_id: str
    participant_id: str
    participant_number: int
    session_number: int
    condition_id: str
    condition_code: str


@dataclass(frozen=True)
class TimestampRecord:
    timestamp_ms: float
    source_line: int


@dataclass
class SensorParse:
    valid_by_frame: dict[int, TimestampRecord]
    ambiguous_frames: set[int]
    raw_rows: int
    valid_rows: int
    malformed_rows: int
    stream_status: str
    issues: list[dict[str, Any]]


@dataclass
class StagedFile:
    target: Path
    temporary: Path
    rows: int | None
    sha256: str


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def record_sha256(fields: Sequence[str]) -> str:
    payload = "\x1f".join(fields).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def clean_float(value: float, decimals: int = 3) -> str:
    if not math.isfinite(value):
        raise BuildError(f"Refusing to format non-finite number: {value!r}")
    text = f"{value:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def parse_finite(value: str, context: str) -> float:
    try:
        result = float(value.strip())
    except (TypeError, ValueError) as exc:
        raise BuildError(f"{context} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise BuildError(f"{context} is not finite: {value!r}")
    return result


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read JSON {path}: {exc}") from exc


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    except (OSError, csv.Error) as exc:
        raise BuildError(f"Cannot read CSV {path}: {exc}") from exc


def relative_posix(path: Path, root: Path, context: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise BuildError(f"{context} is outside its declared root: {path}") from exc


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_sessions(path: Path) -> list[Session]:
    rows = read_csv_dicts(path)
    sessions: list[Session] = []
    for row in rows:
        try:
            sessions.append(
                Session(
                    session_id=row["session_id"],
                    participant_id=row["participant_id"],
                    participant_number=int(row["participant_number"]),
                    session_number=int(row["session_number"]),
                    condition_id=row["condition_id"],
                    condition_code=row["condition_code"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildError(f"Malformed canonical session row: {row}") from exc
    ids = [item.session_id for item in sessions]
    if len(sessions) != EXPECTED_INTERVALS or len(set(ids)) != EXPECTED_INTERVALS:
        raise BuildError(
            f"Expected 114 unique canonical sessions; got {len(sessions)} rows and "
            f"{len(set(ids))} unique IDs."
        )
    return sessions


def load_inventory(path: Path, expected_ids: set[str]) -> dict[str, dict[str, str]]:
    rows = read_csv_dicts(path)
    by_id = {row.get("session_id", ""): row for row in rows}
    if len(rows) != EXPECTED_INTERVALS or set(by_id) != expected_ids:
        raise BuildError("Session inventory does not join exactly to canonical sessions.")
    return by_id


def require_registered_output_hash(
    manifest_path: Path, relative_name: str, actual_path: Path
) -> str:
    manifest = read_json(manifest_path)
    registered = {
        str(item.get("file", "")): str(item.get("sha256", "")).lower()
        for item in manifest.get("outputs", [])
        if isinstance(item, dict)
    }
    expected = registered.get(relative_name)
    if not expected:
        raise BuildError(
            f"{manifest_path.name} does not register an output hash for {relative_name}."
        )
    actual = sha256_file(actual_path)
    if actual.lower() != expected:
        raise BuildError(
            f"Frozen source hash mismatch for {relative_name}: {actual} != {expected}"
        )
    return actual


def manifest_session_id(raw: str) -> str:
    match = MANIFEST_SESSION_RE.fullmatch(raw)
    if not match:
        raise BuildError(f"Invalid provisional manifest session identifier: {raw!r}")
    return f"P{int(match.group(1))}S{int(match.group(2))}"


def load_provisional_manifest(
    registry_path: Path, expected_available_ids: set[str]
) -> tuple[Path, dict[str, dict[str, Any]], str, Mapping[str, Any]]:
    registry = read_json(registry_path)
    if registry.get("schema") != "rad-emo-provisional-timestamp-registry-v1":
        raise BuildError("Unexpected provisional timestamp registry schema.")
    manifest_path = Path(str(registry.get("source_manifest", "")))
    if not manifest_path.is_file():
        raise BuildError(f"Provisional timestamp manifest is unavailable: {manifest_path}")
    manifest_sha = sha256_file(manifest_path)
    registered_sha = str(registry.get("source_manifest_sha256", "")).lower()
    if manifest_sha.lower() != registered_sha:
        raise BuildError(
            "Provisional timestamp manifest hash does not match the frozen registry: "
            f"{manifest_sha} != {registered_sha}"
        )
    manifest = read_json(manifest_path)
    if manifest.get("schema") != "exp2-provisional-radar-timestamps-v1.0.0":
        raise BuildError("Unexpected provisional timestamp manifest schema.")
    entries: dict[str, dict[str, Any]] = {}
    for value in manifest.get("sessions", []):
        if not isinstance(value, dict):
            raise BuildError("Non-object session entry in provisional manifest.")
        session_id = manifest_session_id(str(value.get("session", "")))
        if session_id in entries:
            raise BuildError(f"Duplicate provisional manifest entry for {session_id}.")
        entries[session_id] = value
    if set(entries) != expected_available_ids:
        missing = sorted(expected_available_ids - set(entries))
        extra = sorted(set(entries) - expected_available_ids)
        raise BuildError(f"Provisional manifest session mismatch: missing={missing}, extra={extra}")
    counts = manifest.get("counts", {})
    if int(counts.get("sessions", -1)) != EXPECTED_AVAILABLE_INTERVALS:
        raise BuildError("Provisional manifest does not report 90 sessions.")
    if int(counts.get("total_frame_timestamps", -1)) != EXPECTED_RADAR_FRAMES:
        raise BuildError("Provisional manifest radar timestamp total is not 39,647.")
    return manifest_path, entries, manifest_sha, manifest


def enumerate_frames(directory: Path, pattern: re.Pattern[str]) -> dict[int, tuple[str, Path]]:
    if not directory.is_dir():
        raise BuildError(f"Frame directory is missing: {directory}")
    frames: dict[int, tuple[str, Path]] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            match = pattern.fullmatch(entry.name)
            if not match or entry.is_dir(follow_symlinks=False):
                continue
            frame_number = int(match.group(1))
            if frame_number in frames:
                raise BuildError(f"Duplicate frame number {frame_number} in {directory}")
            frames[frame_number] = (entry.name, Path(entry.path))
    return frames


def parse_sensor_csv(
    path: Path,
    kind: str,
    session_id: str,
    source_relpath: str,
    physical_frame_ids: set[int],
) -> SensorParse:
    expected_header = (
        ["Frame No", "Color Timestamp (ms)", "Pulse Sensor Value"]
        if kind == "rgb"
        else ["Frame No", "Timestamp (ms)"]
    )
    valid_records: dict[int, list[TimestampRecord]] = {}
    invalid_records: list[tuple[int, list[str], int | None, str]] = []
    raw_rows = 0
    valid_rows = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            try:
                header = [field.strip() for field in next(reader)]
            except StopIteration as exc:
                raise BuildError(f"Empty {kind} timestamp CSV: {path}") from exc
            if header != expected_header:
                raise BuildError(
                    f"Unexpected {kind} timestamp header at {path}: {header!r}"
                )
            for row in reader:
                raw_rows += 1
                line_number = reader.line_num
                frame_number: int | None = None
                issue_code = ""
                if row:
                    try:
                        frame_number = int(row[0].strip())
                        if frame_number < 0:
                            raise ValueError("negative")
                    except (TypeError, ValueError):
                        issue_code = "invalid_frame_number"
                        frame_number = None
                else:
                    issue_code = "empty_record"
                if not issue_code and len(row) != len(expected_header):
                    issue_code = "wrong_field_count"
                timestamp: float | None = None
                if not issue_code:
                    try:
                        timestamp = float(row[1].strip())
                        if not math.isfinite(timestamp):
                            raise ValueError("non-finite")
                    except (TypeError, ValueError):
                        issue_code = "missing_or_invalid_timestamp"
                if issue_code:
                    invalid_records.append((line_number, row, frame_number, issue_code))
                    continue
                assert frame_number is not None and timestamp is not None
                valid_rows += 1
                valid_records.setdefault(frame_number, []).append(
                    TimestampRecord(timestamp_ms=timestamp, source_line=line_number)
                )
    except (OSError, csv.Error) as exc:
        raise BuildError(f"Cannot parse {kind} timestamp CSV {path}: {exc}") from exc

    valid_by_frame: dict[int, TimestampRecord] = {}
    ambiguous_frames: set[int] = set()
    issues: list[dict[str, Any]] = []
    for frame_number, records in sorted(valid_records.items()):
        if len(records) == 1:
            valid_by_frame[frame_number] = records[0]
            continue
        ambiguous_frames.add(frame_number)
        issues.append(
            {
                "session_id": session_id,
                "modality": kind,
                "source_relpath": source_relpath,
                "source_line_number": ";".join(str(item.source_line) for item in records),
                "frame_number": frame_number,
                "issue_code": "duplicate_valid_timestamp_records",
                "valid_alternative_record_present": 0,
                "frame_file_present": int(frame_number in physical_frame_ids),
                "disposition": "frame_retained_timing_ambiguous",
                "record_sha256": "",
            }
        )

    for line_number, row, frame_number, issue_code in invalid_records:
        valid_alternative = frame_number is not None and frame_number in valid_by_frame
        frame_present = frame_number is not None and frame_number in physical_frame_ids
        if valid_alternative:
            disposition = "rejected_valid_alternative_retained"
        elif frame_present:
            disposition = "frame_retained_timing_unusable"
        else:
            disposition = "rejected_no_physical_frame_mapping"
        issues.append(
            {
                "session_id": session_id,
                "modality": kind,
                "source_relpath": source_relpath,
                "source_line_number": line_number,
                "frame_number": "" if frame_number is None else frame_number,
                "issue_code": issue_code,
                "valid_alternative_record_present": int(valid_alternative),
                "frame_file_present": int(frame_present),
                "disposition": disposition,
                "record_sha256": record_sha256(row),
            }
        )

    for frame_number in sorted(set(valid_records) - physical_frame_ids):
        issues.append(
            {
                "session_id": session_id,
                "modality": kind,
                "source_relpath": source_relpath,
                "source_line_number": ";".join(
                    str(item.source_line) for item in valid_records[frame_number]
                ),
                "frame_number": frame_number,
                "issue_code": "timestamp_record_without_frame_file",
                "valid_alternative_record_present": 0,
                "frame_file_present": 0,
                "disposition": "metadata_retained_not_in_frame_membership",
                "record_sha256": "",
            }
        )

    for frame_number in sorted(physical_frame_ids - set(valid_by_frame) - ambiguous_frames):
        issues.append(
            {
                "session_id": session_id,
                "modality": kind,
                "source_relpath": source_relpath,
                "source_line_number": "",
                "frame_number": frame_number,
                "issue_code": "frame_file_without_valid_timestamp_record",
                "valid_alternative_record_present": 0,
                "frame_file_present": 1,
                "disposition": "frame_retained_timing_unusable",
                "record_sha256": "",
            }
        )

    ordered_timestamps = [
        valid_by_frame[key].timestamp_ms
        for key in sorted(physical_frame_ids & set(valid_by_frame))
    ]
    if len(ordered_timestamps) < 2:
        stream_status = "insufficient_valid_timestamps"
    elif len(set(ordered_timestamps)) < 2:
        stream_status = "constant_timestamp_stream"
    elif not all(
        later > earlier
        for earlier, later in zip(ordered_timestamps, ordered_timestamps[1:])
    ):
        stream_status = "timestamp_stream_not_strictly_increasing"
    else:
        stream_status = "valid"

    if stream_status != "valid":
        issues.append(
            {
                "session_id": session_id,
                "modality": kind,
                "source_relpath": source_relpath,
                "source_line_number": "",
                "frame_number": "",
                "issue_code": stream_status,
                "valid_alternative_record_present": 0,
                "frame_file_present": 1,
                "disposition": "entire_modality_stream_retained_timing_unusable",
                "record_sha256": "",
            }
        )

    return SensorParse(
        valid_by_frame=valid_by_frame,
        ambiguous_frames=ambiguous_frames,
        raw_rows=raw_rows,
        valid_rows=valid_rows,
        malformed_rows=len(invalid_records),
        stream_status=stream_status,
        issues=issues,
    )


def read_radar_timestamp_table(path: Path) -> list[dict[str, Any]]:
    expected_header = [
        "Radar_Frame_No",
        "ADC_File",
        "TDA_Timestamp_us",
        "Relative_ms",
        "Provisional_Epoch_ms",
        "Alignment_Source",
        "Uncertainty_ms",
    ]
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            try:
                header = [field.strip() for field in next(reader)]
            except StopIteration as exc:
                raise BuildError(f"Empty radar timestamp table: {path}") from exc
            if header != expected_header:
                raise BuildError(f"Unexpected radar timestamp header at {path}: {header!r}")
            for row in reader:
                if len(row) != len(expected_header):
                    raise BuildError(f"Malformed radar timestamp row {reader.line_num} at {path}")
                try:
                    frame_number = int(row[0])
                    tda_us = int(row[2])
                    relative_ms = float(row[3])
                    epoch_ms = float(row[4])
                    uncertainty_ms = float(row[6])
                except (TypeError, ValueError) as exc:
                    raise BuildError(
                        f"Non-numeric radar timestamp row {reader.line_num} at {path}"
                    ) from exc
                if frame_number < 1 or not all(
                    math.isfinite(value)
                    for value in (relative_ms, epoch_ms, uncertainty_ms)
                ):
                    raise BuildError(f"Invalid radar timestamp row {reader.line_num} at {path}")
                rows.append(
                    {
                        "frame_number": frame_number,
                        "adc_file": row[1],
                        "tda_us": tda_us,
                        "relative_ms": relative_ms,
                        "epoch_ms": epoch_ms,
                        "alignment_source": row[5],
                        "uncertainty_ms": uncertainty_ms,
                    }
                )
    except (OSError, csv.Error) as exc:
        raise BuildError(f"Cannot parse radar timestamp table {path}: {exc}") from exc
    if not rows:
        raise BuildError(f"Radar timestamp table has no rows: {path}")
    return rows


def validate_radar_rows(
    session_id: str,
    rows: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
    physical_frames: Mapping[int, tuple[str, Path]],
) -> None:
    frame_numbers = [int(row["frame_number"]) for row in rows]
    if frame_numbers != list(range(1, len(rows) + 1)):
        raise BuildError(f"Radar frame numbers are not contiguous 1..N for {session_id}.")
    if set(frame_numbers) != set(physical_frames):
        raise BuildError(f"Radar timestamp/file frame-ID mismatch for {session_id}.")
    for row in rows:
        frame_number = int(row["frame_number"])
        if str(row["adc_file"]).lower() != physical_frames[frame_number][0].lower():
            raise BuildError(f"Radar ADC filename mismatch for {session_id} frame {frame_number}.")
    for field in ("tda_us", "relative_ms", "epoch_ms"):
        values = [float(row[field]) for row in rows]
        if not all(later > earlier for earlier, later in zip(values, values[1:])):
            raise BuildError(f"Radar {field} is not strictly increasing for {session_id}.")
    if abs(float(rows[0]["relative_ms"])) > 0.001:
        raise BuildError(f"Radar relative time does not start at zero for {session_id}.")
    sources = {str(row["alignment_source"]) for row in rows}
    uncertainties = {float(row["uncertainty_ms"]) for row in rows}
    if sources != {str(entry.get("alignment_source", ""))}:
        raise BuildError(f"Radar alignment source mismatch for {session_id}.")
    if uncertainties != {float(entry.get("uncertainty_ms", -1))}:
        raise BuildError(f"Radar uncertainty mismatch for {session_id}.")
    if len(rows) != int(entry.get("radar_frames", -1)):
        raise BuildError(f"Radar row count contradicts manifest for {session_id}.")
    if abs(float(rows[0]["epoch_ms"]) - float(entry["first_provisional_epoch_ms"])) > 0.001:
        raise BuildError(f"Radar first epoch mismatch for {session_id}.")
    if abs(float(rows[-1]["epoch_ms"]) - float(entry["last_provisional_epoch_ms"])) > 0.001:
        raise BuildError(f"Radar last epoch mismatch for {session_id}.")
    first_tda = int(rows[0]["tda_us"])
    first_epoch = float(rows[0]["epoch_ms"])
    for row in rows:
        expected_relative = (int(row["tda_us"]) - first_tda) / 1000.0
        expected_epoch = first_epoch + expected_relative
        if abs(float(row["relative_ms"]) - expected_relative) > 0.0011:
            raise BuildError(f"Radar relative/TDA mismatch for {session_id}.")
        # Epoch values are written to 0.001 ms beside ~1.7e12 ms magnitudes;
        # allow the documented decimal rounding plus binary-float ULP.
        if abs(float(row["epoch_ms"]) - expected_epoch) > 0.0015:
            raise BuildError(f"Radar epoch/relative mismatch for {session_id}.")


def known_session_flags(session_id: str) -> list[str]:
    flags: list[str] = []
    if session_id == "P10S3":
        flags.append("RGB_TIMESTAMP_INVALID")
    if session_id == "P11S1":
        flags.append("RADAR_FRAME_COUNT_REVIEW")
    if session_id == "P11S6":
        flags.extend(
            [
                "RGB_THERMAL_END_CONFLICT_APPROX_19S",
                "TIMING_DEPENDENT_FUSION_MANUAL_REVIEW",
            ]
        )
    if session_id in {"P14S2", "P14S3"}:
        flags.append("RGB_THERMAL_NO_EPOCH_OVERLAP")
    if session_id == "P14S3":
        flags.append("THERMAL_TRUNCATED")
    if session_id in {"P11S6", "P14S4", "P14S6"}:
        flags.append("RGB_IMAGE_WITHOUT_TIMESTAMP_RECORD")
    malformed_sessions = {
        "P4S4",
        "P4S6",
        "P5S1",
        "P7S3",
        "P9S3",
        "P9S4",
        "P10S7",
        "P12S3",
        "P12S5",
    }
    if session_id in malformed_sessions:
        flags.append("RGB_MALFORMED_DUPLICATE_METADATA_RECORD")
    return flags


def deduplicate_flags(values: Iterable[str]) -> str:
    return ";".join(dict.fromkeys(value for value in values if value))


def build_intervals(
    sessions: Sequence[Session],
    entries: Mapping[str, Mapping[str, Any]],
    provisional_root: Path,
    manifest_sha: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    interval_rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    radar_tables: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        entry = entries.get(session.session_id)
        flags = known_session_flags(session.session_id)
        if entry is None:
            flags.insert(0, "RADAR_INTERVAL_UNAVAILABLE")
            qc_status = (
                "pending_radar_and_crossmodal_review"
                if session.session_id in {"P14S2", "P14S3"}
                else "pending_radar_timestamps"
            )
            row = {
                "schema_version": INTERVAL_SCHEMA,
                "dataset_version": DATASET_VERSION,
                "session_id": session.session_id,
                "participant_id": session.participant_id,
                "condition_id": session.condition_id,
                "condition_code": session.condition_code,
                "interval_anchor": "radar",
                "availability_status": "pending_radar_timestamps",
                "nominal_start_epoch_ms": "",
                "nominal_end_epoch_ms": "",
                "nominal_duration_ms": "",
                "absolute_epoch_status": "unavailable",
                "alignment_source": "",
                "alignment_strategy": "",
                "uncertainty_model": UNCERTAINTY_MODEL,
                "uncertainty_ms": "",
                "possible_start_epoch_ms": "",
                "possible_end_epoch_ms": "",
                "core_start_epoch_ms": "",
                "core_end_epoch_ms": "",
                "core_exists": 0,
                "radar_frame_count": 0,
                "radar_first_frame_no": "",
                "radar_last_frame_no": "",
                "radar_relative_start_ms": "",
                "radar_relative_end_ms": "",
                "radar_timestamp_table_relpath": "",
                "radar_timestamp_table_sha256": "",
                "radar_index_source_sha256": "",
                "timestamp_manifest_sha256": manifest_sha,
                "hardware_synchronised": 0,
                "use_scope": USE_SCOPE,
                "interval_qc_status": qc_status,
                "qc_flags": deduplicate_flags(flags),
                "algorithm_version": ALGORITHM_VERSION,
            }
            interval_rows.append(row)
            by_id[session.session_id] = row
            continue

        frame_table = provisional_root / str(entry["frame_timestamp_file"])
        relative_table = relative_posix(frame_table, provisional_root, "Radar timestamp table")
        radar_rows = read_radar_timestamp_table(frame_table)
        radar_tables[session.session_id] = radar_rows
        start = float(radar_rows[0]["epoch_ms"])
        end = float(radar_rows[-1]["epoch_ms"])
        uncertainty = float(entry["uncertainty_ms"])
        duration = end - start
        if duration <= 0 or uncertainty < 0:
            raise BuildError(f"Invalid interval geometry for {session.session_id}.")
        core_exists = duration > 2 * uncertainty
        if not core_exists:
            flags.append("NO_CONSERVATIVE_CORE")
        flags = list(entry.get("qc_flags", [])) + flags
        if session.session_id in {"P11S1", "P11S6"}:
            qc_status = "manual_review_required"
        elif session.session_id == "P10S3":
            qc_status = "available_provisional_rgb_timing_unusable"
        else:
            qc_status = "available_provisional_exploratory"
        row = {
            "schema_version": INTERVAL_SCHEMA,
            "dataset_version": DATASET_VERSION,
            "session_id": session.session_id,
            "participant_id": session.participant_id,
            "condition_id": session.condition_id,
            "condition_code": session.condition_code,
            "interval_anchor": "radar",
            "availability_status": "available_provisional",
            "nominal_start_epoch_ms": clean_float(start),
            "nominal_end_epoch_ms": clean_float(end),
            "nominal_duration_ms": clean_float(duration),
            "absolute_epoch_status": "provisional_reconstructed_not_measured",
            "alignment_source": str(entry["alignment_source"]),
            "alignment_strategy": str(entry["alignment_strategy"]),
            "uncertainty_model": UNCERTAINTY_MODEL,
            "uncertainty_ms": clean_float(uncertainty),
            "possible_start_epoch_ms": clean_float(start - uncertainty),
            "possible_end_epoch_ms": clean_float(end + uncertainty),
            "core_start_epoch_ms": clean_float(start + uncertainty) if core_exists else "",
            "core_end_epoch_ms": clean_float(end - uncertainty) if core_exists else "",
            "core_exists": int(core_exists),
            "radar_frame_count": len(radar_rows),
            "radar_first_frame_no": int(radar_rows[0]["frame_number"]),
            "radar_last_frame_no": int(radar_rows[-1]["frame_number"]),
            "radar_relative_start_ms": clean_float(float(radar_rows[0]["relative_ms"])),
            "radar_relative_end_ms": clean_float(float(radar_rows[-1]["relative_ms"])),
            "radar_timestamp_table_relpath": relative_table,
            "radar_timestamp_table_sha256": sha256_file(frame_table),
            "radar_index_source_sha256": str(entry["master_idx_sha256"]),
            "timestamp_manifest_sha256": manifest_sha,
            "hardware_synchronised": 0,
            "use_scope": USE_SCOPE,
            "interval_qc_status": qc_status,
            "qc_flags": deduplicate_flags(flags),
            "algorithm_version": ALGORITHM_VERSION,
        }
        interval_rows.append(row)
        by_id[session.session_id] = row
    return interval_rows, by_id, radar_tables


def classify_sensor_frame(
    interval: Mapping[str, Any] | None,
    stream_status: str,
    timestamp_record: TimestampRecord | None,
    ambiguous: bool,
) -> tuple[str, str, str, str, str]:
    if interval is None or interval["availability_status"] != "available_provisional":
        return (
            "interval_unavailable",
            "unknown",
            "pending_interval",
            "pending_interval_do_not_assign",
            "",
        )
    if stream_status != "valid" or timestamp_record is None or ambiguous:
        return (
            "timing_unusable",
            "unknown",
            "exclude_timing",
            "timing_unusable_do_not_assign",
            "",
        )
    timestamp = timestamp_record.timestamp_ms
    start = float(interval["nominal_start_epoch_ms"])
    end = float(interval["nominal_end_epoch_ms"])
    uncertainty = float(interval["uncertainty_ms"])
    possible_start = start - uncertainty
    possible_end = end + uncertainty
    if start <= timestamp <= end:
        primary = "active_nominal"
    elif possible_start <= timestamp <= possible_end:
        primary = "boundary_candidate"
    else:
        primary = "outside_trial"
    core_exists = str(interval["core_exists"]) == "1" or interval["core_exists"] == 1
    if core_exists and start + uncertainty <= timestamp <= end - uncertainty:
        uncertainty_class = "definite_active"
        recommended = "active_core"
        label_status = "candidate_core_pending_visual_qc"
    elif possible_start <= timestamp <= possible_end:
        uncertainty_class = "possible_active"
        recommended = "exclude_boundary"
        label_status = (
            "candidate_nominal_timing_uncertain_pending_visual_qc"
            if primary == "active_nominal"
            else "timing_boundary_do_not_assign_automatically"
        )
    else:
        uncertainty_class = "definite_outside"
        recommended = "exclude_outside"
        label_status = "outside_trial_do_not_assign"
    return (
        primary,
        uncertainty_class,
        recommended,
        label_status,
        clean_float(timestamp - start),
    )


def session_directory(root: Path, session: Session) -> Path:
    return root / f"P_{session.participant_number}" / (
        f"P_{session.participant_number}_S_{session.session_number}"
    )


def sensor_membership_rows(
    sessions: Sequence[Session],
    intervals: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, str]],
    root: Path,
    kind: str,
    issues: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    aggregate_primary: Counter[str],
    aggregate_uncertainty: Counter[str],
) -> Iterator[dict[str, Any]]:
    if kind == "rgb":
        subdirectory = "color_frames"
        pattern = RGB_FRAME_RE
        expected_inventory_field = "rgb_frame_count"
    elif kind == "thermal":
        subdirectory = "thermal_frames"
        pattern = THERMAL_FRAME_RE
        expected_inventory_field = "thermal_frame_count"
    else:
        raise BuildError(f"Unknown sensor kind: {kind}")

    for session in sessions:
        directory = session_directory(root, session)
        frame_directory = directory / subdirectory
        frames = enumerate_frames(frame_directory, pattern)
        expected_count = int(inventory[session.session_id][expected_inventory_field])
        if len(frames) != expected_count:
            raise BuildError(
                f"{kind} physical-frame count changed for {session.session_id}: "
                f"{len(frames)} != {expected_count}"
            )
        csv_path = directory / "data.csv"
        source_relpath = relative_posix(csv_path, root, f"{kind} timestamp CSV")
        source_sha = sha256_file(csv_path)
        parsed = parse_sensor_csv(
            csv_path, kind, session.session_id, source_relpath, set(frames)
        )
        if parsed.stream_status != "valid" and not (
            kind == "rgb"
            and session.session_id == "P10S3"
            and parsed.stream_status == "constant_timestamp_stream"
        ):
            raise BuildError(
                f"Unexpected unusable {kind} timestamp stream for {session.session_id}: "
                f"{parsed.stream_status}"
            )
        issues.extend(parsed.issues)
        primary_counts: Counter[str] = Counter()
        uncertainty_counts: Counter[str] = Counter()
        recommended_counts: Counter[str] = Counter()
        timestamp_status_counts: Counter[str] = Counter()
        interval = intervals[session.session_id]
        for frame_number in sorted(frames):
            filename, frame_path = frames[frame_number]
            record = parsed.valid_by_frame.get(frame_number)
            ambiguous = frame_number in parsed.ambiguous_frames
            if parsed.stream_status != "valid":
                timestamp_status = "session_timestamp_stream_invalid"
            elif ambiguous:
                timestamp_status = "ambiguous_duplicate_valid_records"
            elif record is None:
                timestamp_status = "missing_csv_record"
            else:
                timestamp_status = "valid"
            primary, uncertainty_class, recommended, label_status, relative_ms = (
                classify_sensor_frame(interval, parsed.stream_status, record, ambiguous)
            )
            frame_flags: list[str] = []
            if timestamp_status != "valid":
                frame_flags.append(timestamp_status.upper())
            primary_counts[primary] += 1
            uncertainty_counts[uncertainty_class] += 1
            recommended_counts[recommended] += 1
            timestamp_status_counts[timestamp_status] += 1
            aggregate_primary[primary] += 1
            aggregate_uncertainty[uncertainty_class] += 1
            yield {
                "schema_version": FRAME_SCHEMA,
                "dataset_version": DATASET_VERSION,
                "session_id": session.session_id,
                "participant_id": session.participant_id,
                "condition_id": session.condition_id,
                "condition_code": session.condition_code,
                "modality": kind,
                "frame_number": frame_number,
                "frame_filename": filename,
                "frame_relpath": relative_posix(frame_path, root, f"{kind} frame"),
                "timestamp_record_status": timestamp_status,
                "timestamp_epoch_ms": "" if record is None else clean_float(record.timestamp_ms),
                "timestamp_source_line": "" if record is None else record.source_line,
                "timestamp_source_relpath": source_relpath,
                "timestamp_source_sha256": source_sha,
                "interval_availability": interval["availability_status"],
                "relative_to_nominal_start_ms": relative_ms,
                "primary_membership": primary,
                "uncertainty_membership": uncertainty_class,
                "recommended_training_class": recommended,
                "condition_label_applicability": label_status,
                "frame_qc_flags": deduplicate_flags(frame_flags),
            }

        summaries.append(
            {
                "schema_version": SUMMARY_SCHEMA,
                "dataset_version": DATASET_VERSION,
                "session_id": session.session_id,
                "modality": kind,
                "physical_frame_count": len(frames),
                "csv_raw_data_rows": parsed.raw_rows,
                "csv_valid_timestamp_rows": parsed.valid_rows,
                "csv_malformed_rows": parsed.malformed_rows,
                "valid_frame_timestamp_mappings": timestamp_status_counts["valid"],
                "missing_frame_timestamp_mappings": (
                    timestamp_status_counts["missing_csv_record"]
                    + timestamp_status_counts["ambiguous_duplicate_valid_records"]
                ),
                "stream_timestamp_status": parsed.stream_status,
                "interval_availability": interval["availability_status"],
                "active_anchor_relative_count": 0,
                "active_nominal_count": primary_counts["active_nominal"],
                "boundary_candidate_count": primary_counts["boundary_candidate"],
                "outside_trial_count": primary_counts["outside_trial"],
                "timing_unusable_count": primary_counts["timing_unusable"],
                "interval_unavailable_count": primary_counts["interval_unavailable"],
                "definite_active_count": uncertainty_counts["definite_active"],
                "possible_active_count": uncertainty_counts["possible_active"],
                "definite_outside_count": uncertainty_counts["definite_outside"],
                "uncertainty_unknown_count": uncertainty_counts["unknown"],
                "recommended_training_frame_count": recommended_counts["active_core"],
                "qc_flags": deduplicate_flags(known_session_flags(session.session_id)),
            }
        )


def radar_membership_rows(
    sessions: Sequence[Session],
    entries: Mapping[str, Mapping[str, Any]],
    radar_tables: Mapping[str, Sequence[Mapping[str, Any]]],
    intervals: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, str]],
    rgb_root: Path,
    provisional_root: Path,
    summaries: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    for session in sessions:
        interval = intervals[session.session_id]
        entry = entries.get(session.session_id)
        if entry is None:
            if int(inventory[session.session_id]["local_radar_frame_count"]) != 0:
                raise BuildError(f"Unexpected local radar inventory for {session.session_id}.")
            summaries.append(
                {
                    "schema_version": SUMMARY_SCHEMA,
                    "dataset_version": DATASET_VERSION,
                    "session_id": session.session_id,
                    "modality": "radar",
                    "physical_frame_count": 0,
                    "csv_raw_data_rows": 0,
                    "csv_valid_timestamp_rows": 0,
                    "csv_malformed_rows": 0,
                    "valid_frame_timestamp_mappings": 0,
                    "missing_frame_timestamp_mappings": 0,
                    "stream_timestamp_status": "pending_radar_timestamps",
                    "interval_availability": "pending_radar_timestamps",
                    "active_anchor_relative_count": 0,
                    "active_nominal_count": 0,
                    "boundary_candidate_count": 0,
                    "outside_trial_count": 0,
                    "timing_unusable_count": 0,
                    "interval_unavailable_count": 0,
                    "definite_active_count": 0,
                    "possible_active_count": 0,
                    "definite_outside_count": 0,
                    "uncertainty_unknown_count": 0,
                    "recommended_training_frame_count": 0,
                    "qc_flags": deduplicate_flags(known_session_flags(session.session_id)),
                }
            )
            continue

        rows = list(radar_tables[session.session_id])
        radar_directory = Path(str(entry["radar_dir"]))
        physical_frames = enumerate_frames(radar_directory, RADAR_FRAME_RE)
        expected_count = int(inventory[session.session_id]["local_radar_frame_count"])
        if len(physical_frames) != expected_count:
            raise BuildError(
                f"Radar physical-frame count changed for {session.session_id}: "
                f"{len(physical_frames)} != {expected_count}"
            )
        validate_radar_rows(session.session_id, rows, entry, physical_frames)
        timestamp_path = provisional_root / str(entry["frame_timestamp_file"])
        timestamp_relpath = relative_posix(
            timestamp_path, provisional_root, "Radar timestamp source"
        )
        timestamp_sha = sha256_file(timestamp_path)
        for row in rows:
            frame_number = int(row["frame_number"])
            filename, frame_path = physical_frames[frame_number]
            yield {
                "schema_version": FRAME_SCHEMA,
                "dataset_version": DATASET_VERSION,
                "session_id": session.session_id,
                "participant_id": session.participant_id,
                "condition_id": session.condition_id,
                "condition_code": session.condition_code,
                "modality": "radar",
                "frame_number": frame_number,
                "frame_filename": filename,
                "frame_relpath": relative_posix(frame_path, rgb_root, "Radar frame"),
                "tda_timestamp_us": int(row["tda_us"]),
                "relative_ms": clean_float(float(row["relative_ms"])),
                "provisional_epoch_ms": clean_float(float(row["epoch_ms"])),
                "alignment_source": str(row["alignment_source"]),
                "uncertainty_ms": clean_float(float(row["uncertainty_ms"])),
                "timestamp_source_relpath": timestamp_relpath,
                "timestamp_source_sha256": timestamp_sha,
                "primary_membership": "active_anchor_relative",
                "uncertainty_membership": "active_anchor_relative",
                "recommended_training_class": "active_anchor",
                "condition_label_applicability": "candidate_trial_label_pending_visual_qc",
                "frame_qc_flags": "",
            }
        summaries.append(
            {
                "schema_version": SUMMARY_SCHEMA,
                "dataset_version": DATASET_VERSION,
                "session_id": session.session_id,
                "modality": "radar",
                "physical_frame_count": len(rows),
                "csv_raw_data_rows": len(rows),
                "csv_valid_timestamp_rows": len(rows),
                "csv_malformed_rows": 0,
                "valid_frame_timestamp_mappings": len(rows),
                "missing_frame_timestamp_mappings": 0,
                "stream_timestamp_status": "valid_authentic_relative_time",
                "interval_availability": interval["availability_status"],
                "active_anchor_relative_count": len(rows),
                "active_nominal_count": 0,
                "boundary_candidate_count": 0,
                "outside_trial_count": 0,
                "timing_unusable_count": 0,
                "interval_unavailable_count": 0,
                "definite_active_count": 0,
                "possible_active_count": 0,
                "definite_outside_count": 0,
                "uncertainty_unknown_count": 0,
                "recommended_training_frame_count": len(rows),
                "qc_flags": deduplicate_flags(known_session_flags(session.session_id)),
            }
        )


def stage_csv(
    target: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> StagedFile:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                if set(row) != set(fieldnames):
                    missing = set(fieldnames) - set(row)
                    extra = set(row) - set(fieldnames)
                    raise BuildError(
                        f"Column mismatch for {target.name}: missing={missing}, extra={extra}"
                    )
                writer.writerow(row)
                count += 1
        temporary = Path(temporary_name)
        return StagedFile(target, temporary, count, sha256_file(temporary))
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def stage_json(target: Path, value: Mapping[str, Any]) -> StagedFile:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(value, stream, indent=2, sort_keys=False)
            stream.write("\n")
        temporary = Path(temporary_name)
        return StagedFile(target, temporary, None, sha256_file(temporary))
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def commit_stages(stages: Sequence[StagedFile]) -> None:
    for stage in stages:
        stage.temporary.replace(stage.target)


def clean_stages(stages: Iterable[StagedFile]) -> None:
    for stage in stages:
        stage.temporary.unlink(missing_ok=True)


def assign_issue_ids(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    def key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        session_match = re.fullmatch(r"P(\d+)S(\d+)", str(item["session_id"]))
        if not session_match:
            raise BuildError(f"Invalid issue session ID: {item['session_id']}")
        line = str(item.get("source_line_number", ""))
        line_key = int(line) if line.isdigit() else 10**9
        frame = str(item.get("frame_number", ""))
        frame_key = int(frame) if frame.isdigit() else 10**9
        return (
            int(session_match.group(1)),
            int(session_match.group(2)),
            str(item["modality"]),
            line_key,
            frame_key,
            str(item["issue_code"]),
        )

    output: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(issues, key=key), start=1):
        output.append(
            {
                "schema_version": ISSUE_SCHEMA,
                "issue_id": f"TSI-{index:06d}",
                **dict(item),
            }
        )
    return output


def validate_outputs(
    interval_rows: Sequence[Mapping[str, Any]],
    stages_by_name: Mapping[str, StagedFile],
    summaries: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    aggregate_primary: Mapping[str, Counter[str]],
    aggregate_uncertainty: Mapping[str, Counter[str]],
) -> dict[str, Any]:
    if len(interval_rows) != EXPECTED_INTERVALS:
        raise BuildError("Active interval table does not contain 114 rows.")
    availability = Counter(str(row["availability_status"]) for row in interval_rows)
    expected_availability = Counter(
        {
            "available_provisional": EXPECTED_AVAILABLE_INTERVALS,
            "pending_radar_timestamps": EXPECTED_PENDING_INTERVALS,
        }
    )
    if availability != expected_availability:
        raise BuildError(f"Unexpected interval availability counts: {availability}")
    uncertainty = Counter(
        float(row["uncertainty_ms"])
        for row in interval_rows
        if row["availability_status"] == "available_provisional"
    )
    if uncertainty != Counter({1000.0: 27, 5000.0: 62, 15000.0: 1}):
        raise BuildError(f"Unexpected uncertainty distribution: {uncertainty}")
    core_count = sum(int(row["core_exists"]) for row in interval_rows)
    if core_count != 89:
        raise BuildError(f"Expected 89 non-empty conservative cores; got {core_count}.")
    expected_rows = {
        "rgb_frame_membership.csv": EXPECTED_RGB_FRAMES,
        "thermal_frame_membership.csv": EXPECTED_THERMAL_FRAMES,
        "radar_frame_membership.csv": EXPECTED_RADAR_FRAMES,
        "active_interval_summary.csv": EXPECTED_INTERVALS * 3,
    }
    for name, expected in expected_rows.items():
        observed = stages_by_name[name].rows
        if observed != expected:
            raise BuildError(f"{name} row count {observed} != expected {expected}.")
    if len(summaries) != EXPECTED_INTERVALS * 3:
        raise BuildError("Summary rows do not cover all session/modality combinations.")
    for modality in ("rgb", "thermal"):
        if Counter(aggregate_primary[modality]) != EXPECTED_PRIMARY[modality]:
            raise BuildError(
                f"Unexpected {modality} primary membership counts: "
                f"{aggregate_primary[modality]}"
            )
        if Counter(aggregate_uncertainty[modality]) != EXPECTED_UNCERTAINTY[modality]:
            raise BuildError(
                f"Unexpected {modality} uncertainty membership counts: "
                f"{aggregate_uncertainty[modality]}"
            )
    malformed = sum(
        1
        for issue in issues
        if issue["issue_code"] in {
            "wrong_field_count",
            "missing_or_invalid_timestamp",
            "invalid_frame_number",
            "empty_record",
        }
    )
    missing_images = sum(
        1
        for issue in issues
        if issue["issue_code"] == "frame_file_without_valid_timestamp_record"
    )
    if malformed != EXPECTED_MALFORMED_SENSOR_ROWS:
        raise BuildError(f"Expected 9 malformed sensor rows; got {malformed}.")
    if missing_images != EXPECTED_IMAGE_WITHOUT_TIMESTAMP:
        raise BuildError(f"Expected 3 image/timestamp orphans; got {missing_images}.")
    p10_issue = [
        issue
        for issue in issues
        if issue["session_id"] == "P10S3"
        and issue["modality"] == "rgb"
        and issue["issue_code"] == "constant_timestamp_stream"
    ]
    if len(p10_issue) != 1:
        raise BuildError("P10S3 constant RGB timestamp stream was not retained exactly once.")
    return {
        "intervals": len(interval_rows),
        "available_intervals": availability["available_provisional"],
        "pending_intervals": availability["pending_radar_timestamps"],
        "nonempty_conservative_cores": core_count,
        "rgb_frames": EXPECTED_RGB_FRAMES,
        "thermal_frames": EXPECTED_THERMAL_FRAMES,
        "radar_frames": EXPECTED_RADAR_FRAMES,
        "timestamp_ingestion_issues": len(issues),
        "malformed_duplicate_metadata_rows": malformed,
        "image_without_timestamp_records": missing_images,
        "rgb_primary_membership": dict(EXPECTED_PRIMARY["rgb"]),
        "rgb_uncertainty_membership": dict(EXPECTED_UNCERTAINTY["rgb"]),
        "thermal_primary_membership": dict(EXPECTED_PRIMARY["thermal"]),
        "thermal_uncertainty_membership": dict(EXPECTED_UNCERTAINTY["thermal"]),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    dataset_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        type=Path,
        default=dataset_root / "config" / "paths.local.json",
        help="Local read-only source-path configuration.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=dataset_root / "synchronization",
        help="Derived synchronization output directory.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dataset_root = Path(__file__).resolve().parents[1]
    stages: list[StagedFile] = []
    try:
        paths = read_json(args.paths)
        if paths.get("schema") != "rad-emo-local-paths-v1":
            raise BuildError("Unexpected paths.local.json schema.")
        if paths.get("raw_data_policy") != "read_only":
            raise BuildError("Local path configuration does not declare raw_data_policy=read_only.")
        rgb_root = Path(str(paths["rgb_mmw_exp2_root"]))
        thermal_root = Path(str(paths["thermal_exp2_root"]))
        provisional_root = Path(str(paths["provisional_radar_timestamps_root"]))
        for root, label in (
            (rgb_root, "RGB/radar root"),
            (thermal_root, "thermal root"),
            (provisional_root, "provisional timestamp root"),
        ):
            if not root.is_dir():
                raise BuildError(f"{label} is unavailable: {root}")
        output_dir = args.output_dir.resolve()
        for source_root in (rgb_root, thermal_root, provisional_root):
            if is_within(output_dir, source_root):
                raise BuildError(
                    f"Refusing to write derived outputs inside a read-only source root: "
                    f"{output_dir}"
                )
        required_output_dir = (dataset_root / "synchronization").resolve()
        if output_dir != required_output_dir:
            raise BuildError(
                "Frozen v1 outputs must be written exactly to the dataset synchronization "
                f"directory: {required_output_dir}"
            )

        sessions_path = dataset_root / "metadata" / "sessions.csv"
        inventory_path = dataset_root / "qc" / "session_inventory.csv"
        metadata_manifest_path = (
            dataset_root / "metadata" / "metadata_build_manifest_v1.json"
        )
        audit_manifest_path = (
            dataset_root / "qc" / "audit_manifest_2026-08-14.json"
        )
        registry_path = dataset_root / "synchronization" / "provisional_timestamp_registry.json"
        sessions_sha = require_registered_output_hash(
            metadata_manifest_path, "metadata/sessions.csv", sessions_path
        )
        inventory_sha = require_registered_output_hash(
            audit_manifest_path, "qc/session_inventory.csv", inventory_path
        )
        sessions = load_sessions(sessions_path)
        expected_ids = {session.session_id for session in sessions}
        available_ids = {
            session.session_id for session in sessions if session.participant_number <= 12
        }
        inventory = load_inventory(inventory_path, expected_ids)
        manifest_path, entries, manifest_sha, source_manifest = load_provisional_manifest(
            registry_path, available_ids
        )
        interval_rows, intervals, radar_tables = build_intervals(
            sessions, entries, provisional_root, manifest_sha
        )

        summaries: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        aggregate_primary = {"rgb": Counter(), "thermal": Counter()}
        aggregate_uncertainty = {"rgb": Counter(), "thermal": Counter()}

        stages.append(
            stage_csv(
                args.output_dir / "active_intervals.csv",
                INTERVAL_FIELDS,
                interval_rows,
            )
        )
        stages.append(
            stage_csv(
                args.output_dir / "rgb_frame_membership.csv",
                SENSOR_FRAME_FIELDS,
                sensor_membership_rows(
                    sessions,
                    intervals,
                    inventory,
                    rgb_root,
                    "rgb",
                    issues,
                    summaries,
                    aggregate_primary["rgb"],
                    aggregate_uncertainty["rgb"],
                ),
            )
        )
        stages.append(
            stage_csv(
                args.output_dir / "thermal_frame_membership.csv",
                SENSOR_FRAME_FIELDS,
                sensor_membership_rows(
                    sessions,
                    intervals,
                    inventory,
                    thermal_root,
                    "thermal",
                    issues,
                    summaries,
                    aggregate_primary["thermal"],
                    aggregate_uncertainty["thermal"],
                ),
            )
        )
        stages.append(
            stage_csv(
                args.output_dir / "radar_frame_membership.csv",
                RADAR_FRAME_FIELDS,
                radar_membership_rows(
                    sessions,
                    entries,
                    radar_tables,
                    intervals,
                    inventory,
                    rgb_root,
                    provisional_root,
                    summaries,
                ),
            )
        )
        summaries.sort(
            key=lambda row: (
                int(re.fullmatch(r"P(\d+)S(\d+)", str(row["session_id"])).group(1)),
                int(re.fullmatch(r"P(\d+)S(\d+)", str(row["session_id"])).group(2)),
                {"rgb": 0, "thermal": 1, "radar": 2}[str(row["modality"])],
            )
        )
        stages.append(
            stage_csv(
                args.output_dir / "active_interval_summary.csv",
                SUMMARY_FIELDS,
                summaries,
            )
        )
        issue_rows = assign_issue_ids(issues)
        stages.append(
            stage_csv(
                args.output_dir / "timestamp_ingestion_issues.csv",
                ISSUE_FIELDS,
                issue_rows,
            )
        )

        stages_by_name = {stage.target.name: stage for stage in stages}
        validated_counts = validate_outputs(
            interval_rows,
            stages_by_name,
            summaries,
            issue_rows,
            aggregate_primary,
            aggregate_uncertainty,
        )
        builder_path = Path(__file__).resolve()
        source_binding_entries = [
            ("config/paths.local.json", sha256_file(args.paths)),
            ("metadata/metadata_build_manifest_v1.json", sha256_file(metadata_manifest_path)),
            ("metadata/sessions.csv", sessions_sha),
            ("qc/audit_manifest_2026-08-14.json", sha256_file(audit_manifest_path)),
            ("qc/session_inventory.csv", inventory_sha),
            (
                "synchronization/provisional_timestamp_registry.json",
                sha256_file(registry_path),
            ),
            (str(manifest_path), manifest_sha),
        ]
        binding = hashlib.sha256()
        for name, digest in sorted(source_binding_entries):
            binding.update(f"{name}\t{digest}\n".encode("utf-8"))
        output_records = [
            {
                "file": relative_posix(stage.target, dataset_root, "Derived output"),
                **({"rows": stage.rows} if stage.rows is not None else {}),
                "sha256": stage.sha256,
            }
            for stage in stages
        ]
        build_manifest = {
            "schema": MANIFEST_SCHEMA,
            "dataset_version": DATASET_VERSION,
            "status": "validated_development_derivative",
            "builder": {
                "file": "processing/build_active_intervals_v1.py",
                "sha256": sha256_file(builder_path),
                "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "dependencies": "python_standard_library_only",
            },
            "policy": {
                "file": "synchronization/ACTIVE_INTERVAL_POLICY_v1.md",
                "sha256": sha256_file(
                    dataset_root / "synchronization" / "ACTIVE_INTERVAL_POLICY_v1.md"
                ),
            },
            "sources": {
                "paths_config_sha256": sha256_file(args.paths),
                "metadata_build_manifest_sha256": sha256_file(metadata_manifest_path),
                "sessions_sha256": sessions_sha,
                "audit_manifest_sha256": sha256_file(audit_manifest_path),
                "session_inventory_sha256": inventory_sha,
                "timestamp_registry_sha256": sha256_file(registry_path),
                "provisional_manifest_path": str(manifest_path),
                "provisional_manifest_sha256": manifest_sha,
                "provisional_manifest_content_binding_sha256": source_manifest.get(
                    "content_binding_sha256", ""
                ),
                "source_binding_sha256": binding.hexdigest(),
                "raw_payload_files_opened": False,
                "raw_source_files_modified": False,
            },
            "algorithm": {
                "version": ALGORITHM_VERSION,
                "uncertainty_model": UNCERTAINTY_MODEL,
                "nominal_interval": "[s,e]",
                "possible_interval": "[s-u,e+u]",
                "conservative_core": "[s+u,e-u] when e-s>2u",
                "endpoints": "inclusive",
                "radar_membership": "all native radar frames active_anchor_relative",
                "native_frame_rates_preserved": True,
                "resampling_performed": False,
            },
            "outputs": output_records,
            "validated_counts": validated_counts,
            "required_wording": (
                "Approximately session/window aligned; not hardware frame-synchronised."
            ),
        }
        manifest_stage = stage_json(
            args.output_dir / "active_interval_build_manifest_v1.json", build_manifest
        )
        stages.append(manifest_stage)
        commit_stages(stages)

        print("RAD-EMO active-interval v1 build succeeded (all validations passed).")
        print("raw_payload_files_opened=false raw_source_files_modified=false")
        print(
            "intervals=114 available=90 pending=24 conservative_cores=89 "
            "rgb_frames=189835 thermal_frames=65367 radar_frames=39647"
        )
        for stage in stages:
            row_text = "" if stage.rows is None else f" rows={stage.rows}"
            print(f"output={stage.target.resolve()}{row_text} sha256={stage.sha256}")
        return 0
    except (BuildError, KeyError, OSError, ValueError) as exc:
        clean_stages(stages)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
