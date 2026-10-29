#!/usr/bin/env python3
"""Build the RAD-EMO Exp2 session inventory without reading raw payload bytes.

The script opens only small metadata files (CSV/JSON) after checking Windows
Cloud Files attributes. It never opens RGB, thermal or ADC frame payloads; those
are counted from directory entries only. If a metadata file is cloud-only, it
is left untouched and reported as unread rather than triggering hydration.

The only file written is ``session_inventory.csv`` in the explicitly supplied,
new ``--output-dir``. Existing directories and files are never replaced. The
output is rejected if it is inside any configured source-data root.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = "rad_emo_session_inventory_v1"
EXPECTED_PARTICIPANTS = set(range(1, 16))
SESSION_DIRECTORY_RE = re.compile(r"^P_(\d+)_S_(\d+)$")
PARTICIPANT_DIRECTORY_RE = re.compile(r"^P_(\d+)$")
RADAR_FRAME_RE = re.compile(r"^adcData_frame_\d+\.mat$", re.IGNORECASE)
TIMESTAMP_TABLE_RE = re.compile(
    r"^_Cascade_Capture_p(\d{2})_s(\d{2})_frames\.csv$", re.IGNORECASE
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}

# Windows Cloud Files attributes. UNPINNED alone is not evidence that bytes are
# absent; OFFLINE or either RECALL flag means opening may hydrate the file.
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
CLOUD_ONLY_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


class AuditError(RuntimeError):
    """Fail-closed audit error."""


class CloudOnlyMetadataError(AuditError):
    """Raised before an operation that could hydrate cloud-only metadata."""


def expected_sessions() -> dict[str, dict[str, int | str]]:
    starts = {1: 1, 2: 8, 3: 15, 4: 22, 5: 29}
    expected: dict[str, dict[str, int | str]] = {}
    for participant in range(1, 16):
        session_count = 10 if participant in {5, 10, 15} else 7
        position = ((participant - 1) % 5) + 1
        for session_number in range(1, session_count + 1):
            session_id = f"P{participant}S{session_number}"
            expected[session_id] = {
                "session_id": session_id,
                "source_session_id": f"P_{participant}_S_{session_number}",
                "participant_number": participant,
                "participant_id": f"P{participant}",
                "session_number": session_number,
                "condition_number": starts[position] + session_number - 1,
            }
    if len(expected) != 114:
        raise AssertionError("internal expected-session construction failed")
    return expected


def windows_file_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    attributes = int(get_attributes(str(path)))
    if attributes == INVALID_FILE_ATTRIBUTES:
        error_number = ctypes.get_last_error()
        raise AuditError(
            f"cannot inspect file attributes for {path} (Windows error {error_number})"
        )
    return attributes


def assert_metadata_local(path: Path) -> None:
    """Refuse to open a file whose Windows attributes indicate byte recall."""
    attributes = windows_file_attributes(path)
    if attributes & CLOUD_ONLY_MASK:
        active = []
        if attributes & FILE_ATTRIBUTE_OFFLINE:
            active.append("OFFLINE")
        if attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN:
            active.append("RECALL_ON_OPEN")
        if attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
            active.append("RECALL_ON_DATA_ACCESS")
        raise CloudOnlyMetadataError(
            f"refusing to hydrate cloud-only metadata {path} ({'|'.join(active)})"
        )


@contextmanager
def open_local_text(path: Path) -> Iterator[Any]:
    if not path.is_file():
        raise AuditError(f"required metadata file is missing: {path}")
    assert_metadata_local(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield handle


def read_json_local(path: Path) -> Any:
    with open_local_text(path) as handle:
        return json.load(handle)


def read_dict_rows_local(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open_local_text(path) as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise AuditError(f"CSV has no header: {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Local path configuration JSON declaring raw_data_policy=read_only.",
    )
    parser.add_argument("--rgb-root", type=Path, help="Override RGB/mmWave Exp2 root.")
    parser.add_argument("--thermal-root", type=Path, help="Override thermal Exp2 root.")
    parser.add_argument(
        "--timestamps-root", type=Path, help="Override provisional timestamp root."
    )
    parser.add_argument(
        "--sessions-csv",
        type=Path,
        required=True,
        help="Canonical 114-row session table.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New, nonexistent directory for session_inventory.csv; outside source roots.",
    )
    return parser.parse_args(argv)


def require_new_output_directory(path: Path) -> Path:
    """Reject existing destinations before scanning source metadata."""
    requested = path.expanduser()
    if requested.exists() or requested.is_symlink():
        raise AuditError(f"output directory must be new and nonexistent: {requested}")
    output = requested.resolve()
    if output.exists():
        raise AuditError(f"output directory must be new and nonexistent: {output}")
    return output


def resolve_input_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    config: Mapping[str, Any] = {}
    if args.config:
        loaded = read_json_local(args.config.resolve())
        if not isinstance(loaded, dict):
            raise AuditError("path configuration must be a JSON object")
        config = loaded
        if config.get("raw_data_policy") != "read_only":
            raise AuditError("path configuration does not declare raw_data_policy=read_only")

    def choose(cli_value: Path | None, key: str) -> Path:
        value = cli_value if cli_value is not None else config.get(key)
        if not value:
            raise AuditError(f"missing input path: provide --{key.replace('_', '-')} or config {key}")
        resolved = Path(value).expanduser().resolve()
        if not resolved.is_dir():
            raise AuditError(f"configured source directory is missing: {resolved}")
        return resolved

    return (
        choose(args.rgb_root, "rgb_mmw_exp2_root"),
        choose(args.thermal_root, "thermal_exp2_root"),
        choose(args.timestamps_root, "provisional_radar_timestamps_root"),
    )


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_session_directories(
    root: Path, expected: Mapping[str, Mapping[str, int | str]], label: str
) -> dict[str, Path]:
    participant_numbers: set[int] = set()
    discovered: dict[str, Path] = {}
    duplicate_ids: set[str] = set()

    with os.scandir(root) as root_entries:
        participant_entries = []
        for entry in root_entries:
            match = PARTICIPANT_DIRECTORY_RE.fullmatch(entry.name)
            if match and entry.is_dir(follow_symlinks=False):
                participant_entries.append((entry, int(match.group(1))))

    for participant_entry, participant_number in participant_entries:
        participant_numbers.add(participant_number)
        with os.scandir(participant_entry.path) as session_entries:
            for session_entry in session_entries:
                match = SESSION_DIRECTORY_RE.fullmatch(session_entry.name)
                if not match or not session_entry.is_dir(follow_symlinks=False):
                    continue
                embedded_participant = int(match.group(1))
                session_number = int(match.group(2))
                if embedded_participant != participant_number:
                    raise AuditError(
                        f"{label}: session {session_entry.name} is under the wrong participant directory"
                    )
                session_id = f"P{embedded_participant}S{session_number}"
                if session_id in discovered:
                    duplicate_ids.add(session_id)
                discovered[session_id] = Path(session_entry.path)

    expected_ids = set(expected)
    discovered_ids = set(discovered)
    if participant_numbers != EXPECTED_PARTICIPANTS:
        raise AuditError(
            f"{label}: unexpected participant set; missing={sorted(EXPECTED_PARTICIPANTS - participant_numbers)}, "
            f"extra={sorted(participant_numbers - EXPECTED_PARTICIPANTS)}"
        )
    if discovered_ids != expected_ids or duplicate_ids:
        raise AuditError(
            f"{label}: unexpected session set; missing={sorted(expected_ids - discovered_ids)}, "
            f"extra={sorted(discovered_ids - expected_ids)}, duplicates={sorted(duplicate_ids)}"
        )
    return discovered


def read_canonical_sessions(
    path: Path, expected: Mapping[str, Mapping[str, int | str]]
) -> dict[str, dict[str, str]]:
    required = {
        "session_id",
        "participant_id",
        "participant_number",
        "session_number",
        "condition_id",
        "condition_number",
        "condition_code",
    }
    header, rows = read_dict_rows_local(path.resolve())
    missing_columns = required - set(header)
    if missing_columns:
        raise AuditError(f"canonical sessions CSV lacks columns: {sorted(missing_columns)}")
    by_id: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()
    for row in rows:
        session_id = row["session_id"].strip()
        if session_id in by_id:
            duplicates.add(session_id)
        by_id[session_id] = row
    expected_ids = set(expected)
    if set(by_id) != expected_ids or duplicates or len(rows) != 114:
        raise AuditError(
            "canonical sessions CSV failed exact 114-session validation: "
            f"rows={len(rows)}, missing={sorted(expected_ids - set(by_id))}, "
            f"extra={sorted(set(by_id) - expected_ids)}, duplicates={sorted(duplicates)}"
        )
    for session_id, expectation in expected.items():
        row = by_id[session_id]
        checks = {
            "participant_id": expectation["participant_id"],
            "participant_number": str(expectation["participant_number"]),
            "session_number": str(expectation["session_number"]),
            "condition_id": f"C{int(expectation['condition_number']):02d}",
            "condition_number": str(expectation["condition_number"]),
        }
        mismatches = {
            key: (row.get(key), value)
            for key, value in checks.items()
            if str(row.get(key, "")).strip() != str(value)
        }
        if mismatches:
            raise AuditError(f"canonical mapping mismatch for {session_id}: {mismatches}")
    return by_id


def count_payload_names(directory: Path, predicate: Any) -> tuple[bool, int, set[str]]:
    if not directory.is_dir():
        return False, 0, set()
    names: set[str] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if predicate(entry.name) and not entry.is_dir(follow_symlinks=False):
                names.add(entry.name)
    return True, len(names), names


def inspect_sensor_csv(path: Path, kind: str) -> dict[str, Any]:
    expected_header = (
        ["Frame No", "Color Timestamp (ms)", "Pulse Sensor Value"]
        if kind == "rgb"
        else ["Frame No", "Timestamp (ms)"]
    )
    result: dict[str, Any] = {
        "present": path.is_file(),
        "raw_data_lines": 0,
        "parseable_rows": 0,
        "malformed_rows": 0,
        "unique_timestamps": 0,
        "span_ms": None,
        "timestamp_min": None,
        "timestamp_max": None,
        "strictly_increasing": False,
        "read_status": "missing",
    }
    if not result["present"]:
        return result
    try:
        assert_metadata_local(path)
    except CloudOnlyMetadataError:
        result["read_status"] = "cloud_only_not_read"
        return result

    timestamps: list[float] = []
    malformed = 0
    raw_lines = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [item.strip() for item in next(reader)]
        except StopIteration:
            result["read_status"] = "empty"
            return result
        if header != expected_header:
            raise AuditError(f"unexpected {kind} CSV header at {path}: {header}")
        for row in reader:
            raw_lines += 1
            try:
                if len(row) != len(expected_header):
                    raise ValueError("wrong field count")
                int(row[0].strip())
                timestamp = float(row[1].strip())
                if not math.isfinite(timestamp):
                    raise ValueError("non-finite timestamp")
            except (TypeError, ValueError):
                malformed += 1
                continue
            timestamps.append(timestamp)

    result.update(
        {
            "raw_data_lines": raw_lines,
            "parseable_rows": len(timestamps),
            "malformed_rows": malformed,
            "unique_timestamps": len(set(timestamps)),
            "span_ms": max(timestamps) - min(timestamps) if timestamps else None,
            "timestamp_min": min(timestamps) if timestamps else None,
            "timestamp_max": max(timestamps) if timestamps else None,
            "strictly_increasing": bool(timestamps)
            and all(later > earlier for earlier, later in zip(timestamps, timestamps[1:])),
            "read_status": "read",
        }
    )
    return result


def sensor_status(sensor: Mapping[str, Any], frame_present: bool, frames: int) -> str:
    flags: list[str] = []
    if not frame_present:
        flags.append("frame_directory_missing")
    if not sensor["present"]:
        flags.append("csv_missing")
    elif sensor["read_status"] != "read":
        flags.append(str(sensor["read_status"]))
    else:
        if sensor["malformed_rows"]:
            flags.append(f"malformed_rows={sensor['malformed_rows']}")
        delta = int(sensor["parseable_rows"]) - frames
        if delta:
            flags.append(f"record_frame_delta={delta:+d}")
        if sensor["parseable_rows"] and sensor["unique_timestamps"] < 2:
            flags.append("timestamp_constant")
        elif sensor["parseable_rows"] and not sensor["strictly_increasing"]:
            flags.append("timestamp_not_strictly_increasing")
    return "ok" if not flags else "|".join(flags)


def inspect_local_radar(session_directory: Path) -> dict[str, Any]:
    radar_directories: list[Path] = []
    with os.scandir(session_directory) as entries:
        for entry in entries:
            if entry.name.lower().startswith("cascade_capture") and entry.is_dir(
                follow_symlinks=False
            ):
                radar_directories.append(Path(entry.path))
    frame_names: set[str] = set()
    for directory in radar_directories:
        with os.scandir(directory) as entries:
            for entry in entries:
                if RADAR_FRAME_RE.fullmatch(entry.name) and not entry.is_dir(
                    follow_symlinks=False
                ):
                    if entry.name in frame_names:
                        raise AuditError(
                            f"duplicate radar frame filename across directories in {session_directory}"
                        )
                    frame_names.add(entry.name)
    present = bool(frame_names)
    status = "present" if present and len(radar_directories) == 1 else "not_local"
    if len(radar_directories) > 1:
        status = f"ambiguous_directories={len(radar_directories)}"
    elif radar_directories and not present:
        status = "directory_without_frames"
    return {
        "present": present,
        "directory_count": len(radar_directories),
        "frame_count": len(frame_names),
        "status": status,
    }


def load_timestamp_manifest(root: Path, expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    manifest = read_json_local(root / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sessions"), list):
        raise AuditError("timestamp manifest has an unexpected schema")
    by_id: dict[str, dict[str, Any]] = {}
    for item in manifest["sessions"]:
        if not isinstance(item, dict):
            raise AuditError("timestamp manifest session entry is not an object")
        match = SESSION_DIRECTORY_RE.fullmatch(str(item.get("session", "")))
        if not match:
            raise AuditError(f"invalid timestamp-manifest session ID: {item.get('session')}")
        session_id = f"P{int(match.group(1))}S{int(match.group(2))}"
        if session_id in by_id:
            raise AuditError(f"duplicate timestamp-manifest session: {session_id}")
        by_id[session_id] = item
    unexpected = set(by_id) - expected_ids
    if unexpected:
        raise AuditError(f"timestamp manifest contains unexpected sessions: {sorted(unexpected)}")
    return by_id


def timestamp_table_files(root: Path, expected_ids: set[str]) -> dict[str, Path]:
    directory = root / "frame_timestamps"
    if not directory.is_dir():
        raise AuditError(f"timestamp frame-table directory is missing: {directory}")
    files: dict[str, Path] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            match = TIMESTAMP_TABLE_RE.fullmatch(entry.name)
            if not match or entry.is_dir(follow_symlinks=False):
                continue
            session_id = f"P{int(match.group(1))}S{int(match.group(2))}"
            if session_id not in expected_ids:
                raise AuditError(f"unexpected timestamp table: {entry.name}")
            if session_id in files:
                raise AuditError(f"duplicate timestamp table for {session_id}")
            files[session_id] = Path(entry.path)
    return files


def inspect_timestamp_table(
    path: Path | None, manifest_entry: Mapping[str, Any] | None, radar_frames: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": path is not None and path.is_file(),
        "rows": 0,
        "source": "",
        "uncertainty_ms": "",
        "status": "not_available",
    }
    if manifest_entry is not None:
        result["source"] = str(manifest_entry.get("alignment_source", ""))
        result["uncertainty_ms"] = manifest_entry.get("uncertainty_ms", "")
    if not result["present"]:
        if manifest_entry is not None:
            result["status"] = "manifest_entry_without_table"
        return result
    assert path is not None
    try:
        assert_metadata_local(path)
    except CloudOnlyMetadataError:
        result["status"] = "cloud_only_not_read"
        return result

    expected_header = [
        "Radar_Frame_No",
        "ADC_File",
        "TDA_Timestamp_us",
        "Relative_ms",
        "Provisional_Epoch_ms",
        "Alignment_Source",
        "Uncertainty_ms",
    ]
    rows = 0
    sources: set[str] = set()
    uncertainties: set[float] = set()
    malformed = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [field.strip() for field in next(reader)]
        except StopIteration:
            result["status"] = "empty"
            return result
        if header != expected_header:
            raise AuditError(f"unexpected timestamp-table header at {path}: {header}")
        for row in reader:
            try:
                if len(row) != len(expected_header):
                    raise ValueError("wrong field count")
                int(row[0])
                int(row[2])
                float(row[3])
                float(row[4])
                uncertainty = float(row[6])
            except (TypeError, ValueError):
                malformed += 1
                continue
            rows += 1
            sources.add(row[5])
            uncertainties.add(uncertainty)

    result["rows"] = rows
    flags: list[str] = []
    if malformed:
        flags.append(f"malformed_rows={malformed}")
    if len(sources) != 1:
        flags.append(f"source_values={len(sources)}")
    if len(uncertainties) != 1:
        flags.append(f"uncertainty_values={len(uncertainties)}")
    if rows != radar_frames:
        flags.append(f"radar_frame_delta={rows - radar_frames:+d}")
    if manifest_entry is None:
        flags.append("table_without_manifest_entry")
    else:
        manifest_frames = int(manifest_entry.get("radar_frames", -1))
        if rows != manifest_frames:
            flags.append(f"manifest_frame_delta={rows - manifest_frames:+d}")
        if sources and result["source"] not in sources:
            flags.append("manifest_source_mismatch")
        if uncertainties and float(result["uncertainty_ms"]) not in uncertainties:
            flags.append("manifest_uncertainty_mismatch")
    result["status"] = "ok" if not flags else "|".join(flags)
    return result


def inspect_annotation(path: Path, frame_names: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": path.is_file(),
        "records": 0,
        "boxes": 0,
        "status": "not_present",
    }
    if not result["present"]:
        return result
    try:
        data = read_json_local(path)
    except CloudOnlyMetadataError:
        result["status"] = "cloud_only_not_read"
        return result
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        result["status"] = "invalid_top_level_schema"
        return result

    image_names: list[str] = []
    boxes = 0
    schema_errors = 0
    for image in data["images"]:
        if not isinstance(image, dict) or not isinstance(image.get("image"), str):
            schema_errors += 1
            continue
        image_names.append(image["image"])
        annotations = image.get("annotations")
        if not isinstance(annotations, list):
            schema_errors += 1
            continue
        boxes += len(annotations)
        for annotation in annotations:
            if not isinstance(annotation, dict) or not isinstance(
                annotation.get("boundingBox"), dict
            ):
                schema_errors += 1
                continue
            bounding_box = annotation["boundingBox"]
            try:
                values = [float(bounding_box[key]) for key in ("x", "y", "width", "height")]
            except (KeyError, TypeError, ValueError):
                schema_errors += 1
                continue
            if not all(math.isfinite(value) for value in values) or values[2] < 0 or values[3] < 0:
                schema_errors += 1

    result["records"] = len(data["images"])
    result["boxes"] = boxes
    flags: list[str] = []
    if schema_errors:
        flags.append(f"schema_errors={schema_errors}")
    if len(image_names) != len(set(image_names)):
        flags.append("duplicate_image_references")
    annotation_names = set(image_names)
    missing = frame_names - annotation_names
    extra = annotation_names - frame_names
    if missing:
        flags.append(f"unreferenced_frames={len(missing)}")
    if extra:
        flags.append(f"unknown_image_references={len(extra)}")
    result["status"] = "ok" if not flags else "|".join(flags)
    return result


def format_float(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.3f}"


def build_inventory(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Path]:
    output_directory = require_new_output_directory(args.output_dir)
    expected = expected_sessions()
    expected_ids = set(expected)
    rgb_root, thermal_root, timestamps_root = resolve_input_paths(args)
    output = output_directory / "session_inventory.csv"
    for source_root in (rgb_root, thermal_root, timestamps_root):
        if is_inside(output_directory, source_root):
            raise AuditError(f"refusing to write inventory inside source root: {output}")

    canonical = read_canonical_sessions(args.sessions_csv, expected)
    rgb_directories = discover_session_directories(rgb_root, expected, "RGB")
    thermal_directories = discover_session_directories(thermal_root, expected, "thermal")
    timestamp_manifest = load_timestamp_manifest(timestamps_root, expected_ids)
    timestamp_files = timestamp_table_files(timestamps_root, expected_ids)

    if set(timestamp_manifest) != set(timestamp_files):
        raise AuditError(
            "timestamp manifest/table session sets differ: "
            f"manifest_only={sorted(set(timestamp_manifest) - set(timestamp_files))}, "
            f"table_only={sorted(set(timestamp_files) - set(timestamp_manifest))}"
        )

    records: list[dict[str, Any]] = []
    ordered_ids = sorted(
        expected,
        key=lambda session_id: (
            int(expected[session_id]["participant_number"]),
            int(expected[session_id]["session_number"]),
        ),
    )
    for session_id in ordered_ids:
        metadata = canonical[session_id]
        rgb_directory = rgb_directories[session_id]
        thermal_directory = thermal_directories[session_id]

        rgb_frame_present, rgb_frames, rgb_frame_names = count_payload_names(
            rgb_directory / "color_frames",
            lambda name: Path(name).suffix.lower() in IMAGE_SUFFIXES,
        )
        thermal_frame_present, thermal_frames, _ = count_payload_names(
            thermal_directory / "thermal_frames",
            lambda name: Path(name).suffix.lower() in IMAGE_SUFFIXES,
        )
        rgb_csv = inspect_sensor_csv(rgb_directory / "data.csv", "rgb")
        thermal_csv = inspect_sensor_csv(thermal_directory / "data.csv", "thermal")
        rgb_status = sensor_status(rgb_csv, rgb_frame_present, rgb_frames)
        thermal_status = sensor_status(thermal_csv, thermal_frame_present, thermal_frames)

        radar = inspect_local_radar(rgb_directory)
        timestamp = inspect_timestamp_table(
            timestamp_files.get(session_id),
            timestamp_manifest.get(session_id),
            int(radar["frame_count"]),
        )
        annotation = inspect_annotation(
            rgb_directory / "2D-on-2D_annotations_export.json", rgb_frame_names
        )

        cross_modal_status = "unavailable"
        overlap_ms: float | None = None
        if (
            rgb_csv["timestamp_min"] is not None
            and thermal_csv["timestamp_min"] is not None
        ):
            overlap_ms = min(
                float(rgb_csv["timestamp_max"]), float(thermal_csv["timestamp_max"])
            ) - max(
                float(rgb_csv["timestamp_min"]), float(thermal_csv["timestamp_min"])
            )
            cross_modal_status = "overlap" if overlap_ms >= 0 else "no_epoch_overlap"

        overall_flags: list[str] = []
        if rgb_status != "ok":
            overall_flags.append("RGB_REVIEW")
        if thermal_status != "ok":
            overall_flags.append("THERMAL_REVIEW")
        if not radar["present"]:
            overall_flags.append("RADAR_NOT_LOCAL")
        elif radar["status"] != "present":
            overall_flags.append("RADAR_REVIEW")
        if timestamp["status"] != "ok":
            overall_flags.append("RADAR_TIMESTAMP_MISSING_OR_REVIEW")
        if annotation["status"] == "not_present":
            overall_flags.append("ANNOTATION_PENDING")
        elif annotation["status"] != "ok":
            overall_flags.append("ANNOTATION_REVIEW")
        if cross_modal_status == "no_epoch_overlap":
            overall_flags.append("NO_RGB_THERMAL_EPOCH_OVERLAP")
        if timestamp["uncertainty_ms"] not in {"", None} and float(
            timestamp["uncertainty_ms"]
        ) >= 15000:
            overall_flags.append("RADAR_TIME_HIGH_UNCERTAINTY")

        records.append(
            {
                "inventory_schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "participant_id": metadata["participant_id"],
                "participant_number": metadata["participant_number"],
                "session_number": metadata["session_number"],
                "condition_id": metadata["condition_id"],
                "condition_number": metadata["condition_number"],
                "condition_code": metadata["condition_code"],
                "rgb_session_present": 1,
                "rgb_frame_directory_present": int(rgb_frame_present),
                "rgb_frame_count": rgb_frames,
                "rgb_csv_present": int(rgb_csv["present"]),
                "rgb_csv_raw_data_lines": rgb_csv["raw_data_lines"],
                "rgb_csv_parseable_rows": rgb_csv["parseable_rows"],
                "rgb_csv_malformed_rows": rgb_csv["malformed_rows"],
                "rgb_timestamp_unique_count": rgb_csv["unique_timestamps"],
                "rgb_timestamp_span_ms": format_float(rgb_csv["span_ms"]),
                "rgb_status": rgb_status,
                "thermal_session_present": 1,
                "thermal_frame_directory_present": int(thermal_frame_present),
                "thermal_frame_count": thermal_frames,
                "thermal_csv_present": int(thermal_csv["present"]),
                "thermal_csv_raw_data_lines": thermal_csv["raw_data_lines"],
                "thermal_csv_parseable_rows": thermal_csv["parseable_rows"],
                "thermal_csv_malformed_rows": thermal_csv["malformed_rows"],
                "thermal_timestamp_unique_count": thermal_csv["unique_timestamps"],
                "thermal_timestamp_span_ms": format_float(thermal_csv["span_ms"]),
                "thermal_status": thermal_status,
                "local_radar_present": int(radar["present"]),
                "local_radar_directory_count": radar["directory_count"],
                "local_radar_frame_count": radar["frame_count"],
                "local_radar_status": radar["status"],
                "provisional_timestamp_table_present": int(timestamp["present"]),
                "provisional_timestamp_rows": timestamp["rows"],
                "provisional_timestamp_source": timestamp["source"],
                "provisional_timestamp_uncertainty_ms": format_float(
                    timestamp["uncertainty_ms"]
                ),
                "provisional_timestamp_status": timestamp["status"],
                "annotation_json_present": int(annotation["present"]),
                "annotation_record_count": annotation["records"],
                "annotation_box_count": annotation["boxes"],
                "annotation_structural_status": annotation["status"],
                "rgb_thermal_epoch_overlap_ms": format_float(overlap_ms),
                "rgb_thermal_epoch_status": cross_modal_status,
                "overall_qc_flag": "PASS_STRUCTURAL"
                if not overall_flags
                else "|".join(overall_flags),
            }
        )
    if len(records) != 114 or {row["session_id"] for row in records} != expected_ids:
        raise AuditError("internal output failed exact 114-session validation")
    return records, output


def write_inventory(records: Sequence[Mapping[str, Any]], output: Path) -> str:
    # Exclusive creation also rejects a destination created after validation.
    output.parent.mkdir(parents=True, exist_ok=False)
    fieldnames = list(records[0].keys())
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(output.read_bytes()).hexdigest().upper()


def summarize(records: Sequence[Mapping[str, Any]], output: Path, digest: str) -> dict[str, Any]:
    timestamp_sources = Counter(
        str(row["provisional_timestamp_source"])
        for row in records
        if row["provisional_timestamp_table_present"] == 1
    )
    return {
        "schema": SCHEMA_VERSION,
        "sessions": len(records),
        "participants": len({row["participant_id"] for row in records}),
        "conditions": len({row["condition_id"] for row in records}),
        "rgb_frames": sum(int(row["rgb_frame_count"]) for row in records),
        "rgb_parseable_csv_rows": sum(
            int(row["rgb_csv_parseable_rows"]) for row in records
        ),
        "rgb_malformed_csv_rows": sum(
            int(row["rgb_csv_malformed_rows"]) for row in records
        ),
        "thermal_frames": sum(int(row["thermal_frame_count"]) for row in records),
        "thermal_parseable_csv_rows": sum(
            int(row["thermal_csv_parseable_rows"]) for row in records
        ),
        "local_radar_sessions": sum(int(row["local_radar_present"]) for row in records),
        "local_radar_frames": sum(int(row["local_radar_frame_count"]) for row in records),
        "provisional_timestamp_sessions": sum(
            int(row["provisional_timestamp_table_present"]) for row in records
        ),
        "provisional_timestamp_rows": sum(
            int(row["provisional_timestamp_rows"]) for row in records
        ),
        "timestamp_source_counts": dict(sorted(timestamp_sources.items())),
        "annotation_sessions": sum(int(row["annotation_json_present"]) for row in records),
        "annotation_records": sum(int(row["annotation_record_count"]) for row in records),
        "annotation_boxes": sum(int(row["annotation_box_count"]) for row in records),
        "output": str(output),
        "output_sha256": digest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        records, output = build_inventory(args)
        digest = write_inventory(records, output)
    except (AuditError, csv.Error, json.JSONDecodeError, OSError) as error:
        print(f"AUDIT_FAILED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summarize(records, output, digest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
