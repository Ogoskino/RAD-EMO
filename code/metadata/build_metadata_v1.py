#!/usr/bin/env python3
"""Build the canonical RAD-EMO v1 metadata tables.

This builder is intentionally dependency-free.  It reads the two audited OOXML
sources directly, validates their exact SHA-256 digests and structure, and only
then writes four deterministic CSV files.  It never edits or copies the source
documents or any raw sensor data. All input paths must be explicit. The output
must be a new directory; existing directories and files are never replaced.

The legacy environment-rotation table in the protocol document describes a
planned design.  It is not used as observed acquisition metadata.  Per the
investigator's correction, all released Exp2 sessions are assigned to the one
actual acquisition environment, ISTEC 031C.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET


DATASET_VERSION = "1.0.0"
PROTOCOL_SCHEMA_VERSION = "rad_emo_protocol_metadata_v1"
EMOTION_SCHEMA_VERSION = "rad_emo_emotion_self_report_v1"
SESSION_MAPPING_RULE_VERSION = "participant_blocks_of_5_repeat_conditions_1_38_v1"
LVHA_RULE_VERSION = "lvha_v1_valence_le_4_arousal_ge_6"
LVHA_RULE_EXPRESSION = "valence <= 4 AND arousal >= 6"
RISK_RULE_VERSION = "protocol_risk_index_v1"
RISK_RULE_EXPRESSION = "1 + lvha_label + 2 * knife_present_protocol"
RISK_INTERPRETATION = "experimental_protocol_index_not_observed_dangerousness"

ACTUAL_ENVIRONMENT_ID = "ISTEC_031C"
ACTUAL_ENVIRONMENT_NAME = "ISTEC 031C"
ACTUAL_ENVIRONMENT_SOURCE = "investigator_confirmed_actual_acquisition"

EXPECTED_EMOTION_SHA256 = "A8517A238936B2763530C4DA8CC96C1BDF2FD08BBDEBA10329FE1941711D817A"
EXPECTED_PROTOCOL_SHA256 = "41EAE2C90E3504294A95C67FAC5634201F18CABB9E1F07864C0AB55D58D4B0AA"

XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TRIAL_TABLE_HEADER = [
    "Trial #",
    "Object",
    "Carry Type",
    "Concealment / Material",
    "Position Zone",
    "Emotion",
    "Video",
    "Activity",
]
EMOTION_HEADER = [
    "Session ID",
    "Valence (0-9)",
    "Arousal (0-9)",
    "Dominance (0-9)",
]


class ValidationError(RuntimeError):
    """Raised when an input contradicts the audited schema or study design."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def require_exact_source(path: Path, expected_sha256: str, label: str) -> str:
    if not path.is_file():
        raise ValidationError(f"{label} source does not exist: {path}")
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise ValidationError(
            f"{label} SHA-256 mismatch. Expected {expected_sha256}, observed {observed}. "
            "Refusing to mix an unaudited source into metadata v1."
        )
    return observed


def normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def read_docx_tables(path: Path) -> list[list[list[str]]]:
    try:
        with zipfile.ZipFile(path) as package:
            document_xml = package.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ValidationError(f"Cannot read DOCX protocol source: {exc}") from exc

    root = ET.fromstring(document_xml)
    w = f"{{{WORD_NS}}}"
    tables: list[list[list[str]]] = []
    for table_element in root.iter(f"{w}tbl"):
        table: list[list[str]] = []
        for row_element in table_element.findall(f"{w}tr"):
            row: list[str] = []
            for cell_element in row_element.findall(f"{w}tc"):
                text = "".join(node.text or "" for node in cell_element.iter(f"{w}t"))
                row.append(normalise_space(text))
            table.append(row)
        tables.append(table)
    return tables


def extract_protocol_trials(path: Path) -> list[dict[str, str]]:
    matching_tables = [
        table
        for table in read_docx_tables(path)
        if table and table[0] == TRIAL_TABLE_HEADER
    ]
    if len(matching_tables) != 1:
        raise ValidationError(
            f"Expected exactly one 38-condition table with header {TRIAL_TABLE_HEADER!r}; "
            f"found {len(matching_tables)}."
        )

    table = matching_tables[0]
    if len(table) != 39:
        raise ValidationError(f"Expected header plus 38 trial rows; found {len(table)} rows.")

    records: list[dict[str, str]] = []
    for values in table[1:]:
        if len(values) != len(TRIAL_TABLE_HEADER):
            raise ValidationError(f"Malformed protocol row: {values!r}")
        records.append(dict(zip(TRIAL_TABLE_HEADER, values)))

    trial_numbers = [parse_integer(record["Trial #"], "Trial #") for record in records]
    if trial_numbers != list(range(1, 39)):
        raise ValidationError(f"Protocol trial sequence is not exactly 1..38: {trial_numbers}")
    return records


def shared_string_text(element: ET.Element) -> str:
    ns = f"{{{XLSX_NS}}}"
    return "".join(node.text or "" for node in element.iter(f"{ns}t"))


def xlsx_sheet_path(package: zipfile.ZipFile) -> tuple[str, str]:
    ns = f"{{{XLSX_NS}}}"
    rel_attr = f"{{{REL_NS}}}id"
    workbook = ET.fromstring(package.read("xl/workbook.xml"))
    sheets = workbook.findall(f"{ns}sheets/{ns}sheet")
    if len(sheets) != 1 or sheets[0].get("name") != "Sheet1":
        names = [sheet.get("name") for sheet in sheets]
        raise ValidationError(f"Expected one worksheet named Sheet1; found {names!r}.")
    relationship_id = sheets[0].get(rel_attr)
    if not relationship_id:
        raise ValidationError("Sheet1 has no OOXML relationship ID.")

    relations = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
    relationship = next(
        (
            item
            for item in relations.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
            if item.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None or not relationship.get("Target"):
        raise ValidationError(f"Cannot resolve worksheet relationship {relationship_id!r}.")
    target = relationship.get("Target", "").replace("\\", "/")
    if target.startswith("/"):
        target = target.lstrip("/")
    elif not target.startswith("xl/"):
        target = f"xl/{target}"
    return target, sheets[0].get("name", "")


def column_index(cell_reference: str) -> int:
    match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", cell_reference)
    if not match:
        raise ValidationError(f"Invalid XLSX cell reference: {cell_reference!r}")
    result = 0
    for character in match.group(1):
        result = result * 26 + (ord(character) - ord("A") + 1)
    return result - 1


def read_xlsx_rows(path: Path) -> tuple[str, list[list[str]]]:
    try:
        with zipfile.ZipFile(path) as package:
            shared_strings_root = ET.fromstring(package.read("xl/sharedStrings.xml"))
            shared_strings = [
                shared_string_text(item)
                for item in shared_strings_root.findall(f"{{{XLSX_NS}}}si")
            ]
            sheet_path, sheet_name = xlsx_sheet_path(package)
            sheet_root = ET.fromstring(package.read(sheet_path))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValidationError(f"Cannot read XLSX emotion source: {exc}") from exc

    ns = f"{{{XLSX_NS}}}"
    rows: list[list[str]] = []
    for row_element in sheet_root.findall(f"{ns}sheetData/{ns}row"):
        cells: dict[int, str] = {}
        for cell in row_element.findall(f"{ns}c"):
            reference = cell.get("r", "")
            index = column_index(reference)
            cell_type = cell.get("t", "")
            value_element = cell.find(f"{ns}v")
            raw = "" if value_element is None else (value_element.text or "")
            if cell_type == "s" and raw:
                try:
                    raw = shared_strings[int(raw)]
                except (ValueError, IndexError) as exc:
                    raise ValidationError(f"Invalid shared-string cell {reference}: {raw!r}") from exc
            elif cell_type == "inlineStr":
                inline = cell.find(f"{ns}is")
                raw = "" if inline is None else shared_string_text(inline)
            cells[index] = normalise_space(raw)
        max_index = max(cells, default=-1)
        rows.append([cells.get(index, "") for index in range(max(4, max_index + 1))])
    return sheet_name, rows


def expected_source_session_ids(last_participant: int) -> list[str]:
    identifiers: list[str] = []
    for participant_number in range(1, last_participant + 1):
        position_in_block = ((participant_number - 1) % 5) + 1
        session_count = 10 if position_in_block == 5 else 7
        identifiers.extend(
            f"p{participant_number}_s{session_number}"
            for session_number in range(1, session_count + 1)
        )
    return identifiers


def parse_integer(value: str, field: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} is not numeric: {value!r}") from exc
    if not number.is_integer():
        raise ValidationError(f"{field} must be an integer: {value!r}")
    return int(number)


def extract_emotion_records(path: Path) -> tuple[str, list[dict[str, object]]]:
    sheet_name, rows = read_xlsx_rows(path)
    if not rows or rows[0][:4] != EMOTION_HEADER:
        observed = [] if not rows else rows[0][:4]
        raise ValidationError(f"Emotion workbook header mismatch: {observed!r}")
    if len(rows) != 153:
        raise ValidationError(f"Expected 153 workbook rows including header; found {len(rows)}.")

    data_rows = rows[1:]
    observed_ids = [row[0] for row in data_rows]
    expected_ids = expected_source_session_ids(20)
    if observed_ids != expected_ids:
        raise ValidationError("Workbook session identifiers/order do not match the audited P1-P20 template.")

    intended_ids = set(expected_source_session_ids(15))
    records: list[dict[str, object]] = []
    extra_count = 0
    for source_row, row in enumerate(data_rows, start=2):
        source_session_id = row[0]
        match = re.fullmatch(r"p([1-9][0-9]*)_s([1-9][0-9]*)", source_session_id)
        if not match:
            raise ValidationError(f"Invalid source session ID at row {source_row}: {source_session_id!r}")
        participant_number = int(match.group(1))
        session_number = int(match.group(2))

        if source_session_id not in intended_ids:
            extra_count += 1
            if any(value != "" for value in row[1:4]):
                raise ValidationError(
                    f"Excluded placeholder {source_session_id} has a nonblank score at source row {source_row}."
                )
            continue

        scores = [
            parse_integer(row[index], f"{EMOTION_HEADER[index]} at source row {source_row}")
            for index in range(1, 4)
        ]
        if any(score < 0 or score > 9 for score in scores):
            raise ValidationError(f"Scores outside 0..9 at source row {source_row}: {scores!r}")
        records.append(
            {
                "source_session_id": source_session_id,
                "participant_number": participant_number,
                "session_number": session_number,
                "valence": scores[0],
                "arousal": scores[1],
                "dominance": scores[2],
                "source_row": source_row,
            }
        )

    if len(records) != 114 or extra_count != 38:
        raise ValidationError(
            f"Expected 114 intended rows and 38 blank placeholders; found {len(records)} and {extra_count}."
        )
    return sheet_name, records


def final_prompted_emotion(raw: str) -> str:
    codes = re.findall(r"\bE[0-4]\b", raw.upper())
    if not codes:
        raise ValidationError(f"No prompted-emotion code in protocol value {raw!r}.")
    return codes[-1]


def protocol_object_fields(trial_number: int, raw_object: str) -> dict[str, object]:
    if 1 <= trial_number <= 10:
        expected_prefix = "Phone"
        values = ("PWP", "phone", 1, 0, "none")
    elif 11 <= trial_number <= 20:
        expected_prefix = "Knife"
        values = ("PWK", "knife", 0, 1, "none")
    elif 21 <= trial_number <= 32:
        expected_prefix = "Both"
        values = ("PWPK", "phone_and_knife", 1, 1, "none")
    else:
        expected_prefix = "None"
        distractor = (
            "keys_or_tool" if trial_number in {35, 36} else
            "tablet_or_mug" if trial_number in {37, 38} else
            "none"
        )
        values = ("PO", "neither_target", 0, 0, distractor)
    if not raw_object.startswith(expected_prefix):
        raise ValidationError(
            f"Protocol object contradiction at trial {trial_number}: expected {expected_prefix!r}, "
            f"observed {raw_object!r}."
        )
    condition_code, object_protocol, phone_protocol, knife_protocol, distractor = values
    return {
        "condition_code": condition_code,
        "object_protocol": object_protocol,
        "phone_present_protocol": phone_protocol,
        "knife_present_protocol": knife_protocol,
        "distractor_object_protocol": distractor,
    }


def normalise_visibility(trial_number: int, raw: str) -> str:
    if raw == "Open":
        result = "open"
    elif raw == "Concealed":
        result = "concealed"
    elif raw in {"—", "-", ""}:
        result = "not_applicable"
    else:
        raise ValidationError(f"Unknown carry-type value at trial {trial_number}: {raw!r}")

    expected = (
        "open" if trial_number in {*range(1, 5), *range(11, 17), *range(21, 25)} else
        "concealed" if trial_number in {*range(5, 11), *range(17, 21), *range(25, 33)} else
        "not_applicable"
    )
    if result != expected:
        raise ValidationError(
            f"Open/concealed protocol contradiction at trial {trial_number}: "
            f"expected {expected!r}, observed {result!r}."
        )
    return result


def normalise_planned_material(raw: str) -> str:
    if raw in {"—", "-", ""}:
        return "not_applicable"
    if raw not in {"M1", "M2"}:
        raise ValidationError(f"Unknown planned material code: {raw!r}")
    return raw


def normalise_planned_zone(raw: str) -> str:
    if raw in {"—", "-", ""}:
        return "not_applicable"
    if not re.fullmatch(r"P[1245](\+P[1245])?", raw):
        raise ValidationError(f"Unknown planned carry-zone code: {raw!r}")
    return raw


def build_conditions(protocol_trials: Sequence[dict[str, str]], protocol_sha256: str, source_name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in protocol_trials:
        trial_number = parse_integer(record["Trial #"], "Trial #")
        object_fields = protocol_object_fields(trial_number, record["Object"])
        activity = record["Activity"].lower()
        if activity not in {"still", "walking"}:
            raise ValidationError(f"Unknown activity at trial {trial_number}: {record['Activity']!r}")
        if record["Video"] != "":
            raise ValidationError(f"Unexpected value in unused Video column at trial {trial_number}.")

        knife_present = int(object_fields["knife_present_protocol"])
        rows.append(
            {
                "condition_id": f"C{trial_number:02d}",
                "condition_number": trial_number,
                **object_fields,
                "carry_visibility_protocol": normalise_visibility(trial_number, record["Carry Type"]),
                "carry_zone_code_planned": normalise_planned_zone(record["Position Zone"]),
                "carry_zone_ground_truth_status": "not_enforced_not_ground_truth",
                "material_code_planned": normalise_planned_material(record["Concealment / Material"]),
                "material_ground_truth_status": "not_enforced_not_ground_truth",
                "prompted_emotion_code": final_prompted_emotion(record["Emotion"]),
                "prompted_emotion_protocol_raw": record["Emotion"],
                "activity_protocol": activity,
                "knife_type_protocol": "single_cutlery_knife" if knife_present else "not_applicable",
                "blade_state_label_status": "not_applicable",
                "knife_action_label_status": "not_applicable",
                "environment_scope_actual": ACTUAL_ENVIRONMENT_ID,
                "protocol_source_file": source_name,
                "protocol_source_sha256": protocol_sha256,
                "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
            }
        )

    counts = Counter(str(row["condition_code"]) for row in rows)
    if counts != Counter({"PWP": 10, "PWK": 10, "PWPK": 12, "PO": 6}):
        raise ValidationError(f"Unexpected 38-condition class counts: {dict(counts)}")
    return rows


def condition_number_for_session(participant_number: int, session_number: int) -> tuple[int, int, int]:
    participant_block = ((participant_number - 1) // 5) + 1
    position_in_block = ((participant_number - 1) % 5) + 1
    first_condition = {1: 1, 2: 8, 3: 15, 4: 22, 5: 29}[position_in_block]
    expected_session_count = 10 if position_in_block == 5 else 7
    if not 1 <= session_number <= expected_session_count:
        raise ValidationError(
            f"P{participant_number} session {session_number} contradicts the block mapping."
        )
    return first_condition + session_number - 1, participant_block, position_in_block


def build_metadata_rows(
    emotion_records: Sequence[dict[str, object]],
    conditions: Sequence[dict[str, object]],
    emotion_sha256: str,
    protocol_sha256: str,
    emotion_source_name: str,
    protocol_source_name: str,
    sheet_name: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    conditions_by_number = {int(row["condition_number"]): row for row in conditions}
    session_rows: list[dict[str, object]] = []
    emotion_rows: list[dict[str, object]] = []
    derived_rows: list[dict[str, object]] = []

    for record in emotion_records:
        participant_number = int(record["participant_number"])
        session_number = int(record["session_number"])
        condition_number, participant_block, position_in_block = condition_number_for_session(
            participant_number, session_number
        )
        condition = conditions_by_number[condition_number]
        session_id = f"P{participant_number}S{session_number}"
        participant_id = f"P{participant_number}"
        condition_id = str(condition["condition_id"])
        valence = int(record["valence"])
        arousal = int(record["arousal"])
        dominance = int(record["dominance"])
        knife_present = int(condition["knife_present_protocol"])
        lvha = int(valence <= 4 and arousal >= 6)
        risk = 1 + lvha + (2 * knife_present)

        session_rows.append(
            {
                "session_id": session_id,
                "source_session_id": record["source_session_id"],
                "participant_id": participant_id,
                "participant_number": participant_number,
                "session_number": session_number,
                "participant_block": participant_block,
                "participant_position_in_block": position_in_block,
                "condition_id": condition_id,
                "condition_number": condition_number,
                "condition_code": condition["condition_code"],
                "object_carry_adherence_status": "pending_visual_qc",
                "experiment_id": "EXP2",
                "environment_id": ACTUAL_ENVIRONMENT_ID,
                "environment_name": ACTUAL_ENVIRONMENT_NAME,
                "environment_source": ACTUAL_ENVIRONMENT_SOURCE,
                "legacy_environment_rotation_status": "planned_not_used_as_actual_metadata",
                "session_mapping_rule_version": SESSION_MAPPING_RULE_VERSION,
                "protocol_source_file": protocol_source_name,
                "protocol_source_sha256": protocol_sha256,
                "emotion_source_file": emotion_source_name,
                "emotion_source_sha256": emotion_sha256,
                "dataset_version": DATASET_VERSION,
            }
        )
        emotion_rows.append(
            {
                "session_id": session_id,
                "participant_id": participant_id,
                "source_session_id": record["source_session_id"],
                "valence_0_9": valence,
                "arousal_0_9": arousal,
                "dominance_0_9": dominance,
                "intensity_0_100": "",
                "intensity_status": "not_present_in_source_workbook",
                "source_file": emotion_source_name,
                "source_sheet": sheet_name,
                "source_row": record["source_row"],
                "source_sha256": emotion_sha256,
                "emotion_schema_version": EMOTION_SCHEMA_VERSION,
            }
        )
        derived_rows.append(
            {
                "session_id": session_id,
                "condition_id": condition_id,
                "valence_0_9": valence,
                "arousal_0_9": arousal,
                "knife_present_protocol": knife_present,
                "object_carry_adherence_status": "pending_visual_qc",
                "lvha_label": lvha,
                "lvha_rule_version": LVHA_RULE_VERSION,
                "lvha_rule_expression": LVHA_RULE_EXPRESSION,
                "protocol_risk_index": risk,
                "risk_rule_version": RISK_RULE_VERSION,
                "risk_rule_expression": RISK_RULE_EXPRESSION,
                "risk_interpretation": RISK_INTERPRETATION,
                "emotion_source_sha256": emotion_sha256,
                "protocol_source_sha256": protocol_sha256,
            }
        )

    return session_rows, emotion_rows, derived_rows


def require_unique(rows: Sequence[dict[str, object]], key: str, expected_count: int) -> None:
    values = [str(row[key]) for row in rows]
    if len(values) != expected_count or len(set(values)) != expected_count:
        raise ValidationError(
            f"Expected {expected_count} rows with unique {key}; got {len(values)} rows and "
            f"{len(set(values))} unique values."
        )


def validate_final_tables(
    conditions: Sequence[dict[str, object]],
    sessions: Sequence[dict[str, object]],
    emotions: Sequence[dict[str, object]],
    derived: Sequence[dict[str, object]],
) -> None:
    require_unique(conditions, "condition_id", 38)
    require_unique(sessions, "session_id", 114)
    require_unique(emotions, "session_id", 114)
    require_unique(derived, "session_id", 114)

    session_ids = {str(row["session_id"]) for row in sessions}
    if session_ids != {str(row["session_id"]) for row in emotions} or session_ids != {
        str(row["session_id"]) for row in derived
    }:
        raise ValidationError("Session identifiers do not join exactly across the three session-level tables.")

    class_counts = Counter(str(row["condition_code"]) for row in sessions)
    expected_class_counts = Counter({"PWP": 30, "PWK": 30, "PWPK": 36, "PO": 18})
    if class_counts != expected_class_counts:
        raise ValidationError(f"Unexpected session class counts: {dict(class_counts)}")

    knife_counts = Counter(int(row["knife_present_protocol"]) for row in derived)
    if knife_counts != Counter({1: 66, 0: 48}):
        raise ValidationError(f"Unexpected knife-label counts: {dict(knife_counts)}")

    lvha_counts = Counter(int(row["lvha_label"]) for row in derived)
    if lvha_counts != Counter({0: 70, 1: 44}):
        raise ValidationError(f"Unexpected LVHA-label counts: {dict(lvha_counts)}")

    risk_counts = Counter(int(row["protocol_risk_index"]) for row in derived)
    if risk_counts != Counter({1: 29, 2: 19, 3: 41, 4: 25}):
        raise ValidationError(f"Unexpected protocol-risk counts: {dict(risk_counts)}")

    participant_counts = Counter(str(row["participant_id"]) for row in sessions)
    expected_participant_counts = Counter(
        {f"P{participant}": (10 if participant % 5 == 0 else 7) for participant in range(1, 16)}
    )
    if participant_counts != expected_participant_counts:
        raise ValidationError(f"Unexpected participant/session counts: {dict(participant_counts)}")


def require_new_output_directory(path: Path) -> Path:
    """Validate the destination without creating it or touching input sources."""
    requested = path.expanduser()
    if requested.exists() or requested.is_symlink():
        raise ValidationError(f"Output directory must be new and nonexistent: {requested}")
    output = requested.resolve()
    if output.exists():
        raise ValidationError(f"Output directory must be new and nonexistent: {output}")
    return output


def write_csv_exclusive(path: Path, rows: Sequence[dict[str, object]]) -> None:
    """Write one validated table without replacing any existing file."""
    if not rows:
        raise ValidationError(f"Refusing to write empty table: {path.name}")
    fieldnames = list(rows[0].keys())
    if any(list(row.keys()) != fieldnames for row in rows):
        raise ValidationError(f"Inconsistent columns while writing {path.name}.")

    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def describe_counts(derived: Sequence[dict[str, object]], sessions: Sequence[dict[str, object]]) -> list[str]:
    classes = Counter(str(row["condition_code"]) for row in sessions)
    knife = Counter(int(row["knife_present_protocol"]) for row in derived)
    lvha = Counter(int(row["lvha_label"]) for row in derived)
    risk = Counter(int(row["protocol_risk_index"]) for row in derived)
    return [
        f"condition_classes=PWP:{classes['PWP']},PWK:{classes['PWK']},PWPK:{classes['PWPK']},PO:{classes['PO']}",
        f"knife_present=0:{knife[0]},1:{knife[1]}",
        f"lvha=0:{lvha[0]},1:{lvha[1]}",
        f"protocol_risk_index=1:{risk[1]},2:{risk[2]},3:{risk[3]},4:{risk[4]}",
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emotion-xlsx", type=Path, required=True, help="Approved source workbook; exact frozen SHA-256 required.")
    parser.add_argument("--protocol-docx", type=Path, required=True, help="Approved protocol document; exact frozen SHA-256 required.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New, nonexistent directory for the four generated CSV tables.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.output_dir = require_new_output_directory(args.output_dir)
        emotion_sha256 = require_exact_source(
            args.emotion_xlsx, EXPECTED_EMOTION_SHA256, "Emotion workbook"
        )
        protocol_sha256 = require_exact_source(
            args.protocol_docx, EXPECTED_PROTOCOL_SHA256, "Protocol document"
        )
        protocol_trials = extract_protocol_trials(args.protocol_docx)
        sheet_name, emotion_records = extract_emotion_records(args.emotion_xlsx)
        conditions = build_conditions(
            protocol_trials, protocol_sha256, args.protocol_docx.name
        )
        sessions, emotions, derived = build_metadata_rows(
            emotion_records,
            conditions,
            emotion_sha256,
            protocol_sha256,
            args.emotion_xlsx.name,
            args.protocol_docx.name,
            sheet_name,
        )
        validate_final_tables(conditions, sessions, emotions, derived)

        outputs = [
            ("conditions.csv", conditions),
            ("sessions.csv", sessions),
            ("emotion_self_report.csv", emotions),
            ("derived_labels.csv", derived),
        ]
        args.output_dir.mkdir(parents=True, exist_ok=False)
        for filename, rows in outputs:
            write_csv_exclusive(args.output_dir / filename, rows)

        print("RAD-EMO metadata v1 build succeeded (all validations passed).")
        print(f"emotion_source_sha256={emotion_sha256}")
        print(f"protocol_source_sha256={protocol_sha256}")
        print("sessions=114 participants=15 conditions=38 excluded_blank_placeholders=38")
        for line in describe_counts(derived, sessions):
            print(line)
        for filename, rows in outputs:
            output_path = (args.output_dir / filename).resolve()
            print(f"output={output_path} rows={len(rows)} sha256={sha256_file(output_path)}")
        return 0
    except (ValidationError, ET.ParseError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
