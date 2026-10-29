"""Synthetic, dependency-free checks; no private workbook or sensor data needed."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET


CODE = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, CODE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_script("build_metadata_v1")
audit = load_script("audit_dataset_v1")


def protocol_rows():
    """A synthetic 38-condition protocol with the frozen class/visibility design."""
    rows = []
    for number in range(1, 39):
        object_name = (
            "Phone" if number <= 10 else "Knife" if number <= 20
            else "Both" if number <= 32 else "None"
        )
        visibility = (
            "Open" if number in {1, 2, 3, 4, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24}
            else "Concealed" if number <= 32 else "-"
        )
        rows.append(dict(zip(builder.TRIAL_TABLE_HEADER, [
            str(number), object_name, visibility, "-", "-", "E0", "",
            "Still" if number % 2 else "Walking",
        ])))
    return rows


def emotion_rows(include_placeholders=False):
    """Independent synthetic 15-participant cohort, optionally the full template."""
    rows = []
    selected_lvha = {False: 0, True: 0}
    for participant in range(1, 21 if include_placeholders else 16):
        position = (participant - 1) % 5
        first_condition = (1, 8, 15, 22, 29)[position]
        for session in range(1, (10 if position == 4 else 7) + 1):
            source_id = f"p{participant}_s{session}"
            if participant > 15:
                rows.append([source_id, "", "", ""])
                continue
            knife = 11 <= first_condition + session - 1 <= 32
            lvha = selected_lvha[knife] < (25 if knife else 19)
            selected_lvha[knife] += int(lvha)
            rows.append([source_id, "2" if lvha else "7", "7" if lvha else "2", "5"])
    return rows


def synthetic_tables():
    conditions = builder.build_conditions(protocol_rows(), "synthetic-protocol", "protocol.docx")
    emotions = []
    for source_row, (source_id, valence, arousal, dominance) in enumerate(emotion_rows(), start=2):
        participant, session = source_id[1:].split("_s")
        emotions.append({
            "source_session_id": source_id,
            "participant_number": int(participant),
            "session_number": int(session),
            "valence": int(valence), "arousal": int(arousal), "dominance": int(dominance),
            "source_row": source_row,
        })
    sessions, reports, derived = builder.build_metadata_rows(
        emotions, conditions, "synthetic-emotion", "synthetic-protocol",
        "emotion.xlsx", "protocol.docx", "Sheet1",
    )
    return conditions, sessions, reports, derived


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_protocol(path, records):
    ns = "{" + builder.WORD_NS + "}"
    document = ET.Element(ns + "document")
    table = ET.SubElement(ET.SubElement(document, ns + "body"), ns + "tbl")
    for values in [builder.TRIAL_TABLE_HEADER] + [list(record.values()) for record in records]:
        row = ET.SubElement(table, ns + "tr")
        for value in values:
            cell = ET.SubElement(row, ns + "tc")
            ET.SubElement(ET.SubElement(ET.SubElement(cell, ns + "p"), ns + "r"), ns + "t").text = value
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", ET.tostring(document))


def write_workbook(path, rows):
    ns = "{" + builder.XLSX_NS + "}"
    worksheet = ET.Element(ns + "worksheet")
    data = ET.SubElement(worksheet, ns + "sheetData")
    for row_number, values in enumerate([builder.EMOTION_HEADER] + rows, start=1):
        row = ET.SubElement(data, ns + "row")
        for column, value in zip("ABCD", values):
            cell = ET.SubElement(row, ns + "c", {"r": f"{column}{row_number}", "t": "inlineStr"})
            ET.SubElement(ET.SubElement(cell, ns + "is"), ns + "t").text = value
    workbook = ET.Element(ns + "workbook")
    sheets = ET.SubElement(workbook, ns + "sheets")
    ET.SubElement(sheets, ns + "sheet", {"name": "Sheet1", "{" + builder.REL_NS + "}id": "rId1"})
    relations = ET.Element("{" + builder.PACKAGE_REL_NS + "}Relationships")
    ET.SubElement(relations, "{" + builder.PACKAGE_REL_NS + "}Relationship", {"Id": "rId1", "Target": "worksheets/sheet1.xml"})
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("xl/workbook.xml", ET.tostring(workbook))
        package.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relations))
        package.writestr("xl/sharedStrings.xml", ET.tostring(ET.Element(ns + "sst")))
        package.writestr("xl/worksheets/sheet1.xml", ET.tostring(worksheet))


class MetadataCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_every_public_cli_input_is_required(self):
        for module, options in [
            (builder, ["--emotion-xlsx", "--protocol-docx", "--output-dir"]),
            (audit, ["--config", "--sessions-csv", "--output-dir"]),
        ]:
            for missing in options:
                arguments = [value for option in options if option != missing for value in (option, "fixture")]
                with self.subTest(module=module.__name__, missing=missing):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        module.parse_args(arguments)
                    self.assertEqual(error.exception.code, 2)

    def test_no_personal_source_paths_or_private_config_defaults(self):
        for name in ["build_metadata_v1.py", "audit_dataset_v1.py", "config/paths.example.json"]:
            text = (CODE / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)[A-Z]:[\\/]Users[\\/]")
            self.assertNotIn("paths.local.json", text)
        config = json.loads((CODE / "config/paths.example.json").read_text())
        self.assertEqual(config["raw_data_policy"], "read_only")
        self.assertEqual(set(config), {
            "schema", "rgb_mmw_exp2_root", "thermal_exp2_root",
            "provisional_radar_timestamps_root", "raw_data_policy",
        })
        self.assertTrue(all(value.startswith("<path-to-") for key, value in config.items() if key.endswith("_root")))

    def test_frozen_source_hashes_and_mismatch_rejection(self):
        self.assertEqual(builder.EXPECTED_EMOTION_SHA256, "A8517A238936B2763530C4DA8CC96C1BDF2FD08BBDEBA10329FE1941711D817A")
        self.assertEqual(builder.EXPECTED_PROTOCOL_SHA256, "41EAE2C90E3504294A95C67FAC5634201F18CABB9E1F07864C0AB55D58D4B0AA")
        source = self.root / "synthetic.xlsx"
        source.write_bytes(b"synthetic fixture, not the approved workbook")
        with self.assertRaisesRegex(builder.ValidationError, "SHA-256 mismatch"):
            builder.require_exact_source(source, builder.EXPECTED_EMOTION_SHA256, "Emotion workbook")

    def test_synthetic_docx_preserves_exact_condition_classes(self):
        source = self.root / "protocol.docx"
        write_protocol(source, protocol_rows())
        trials = builder.extract_protocol_trials(source)
        conditions = builder.build_conditions(trials, "synthetic", source.name)
        self.assertEqual(Counter(row["condition_code"] for row in conditions), {"PWP": 10, "PWK": 10, "PWPK": 12, "PO": 6})
        self.assertEqual(Counter(row["carry_visibility_protocol"] for row in conditions), {"open": 14, "concealed": 18, "not_applicable": 6})

    def test_protocol_rejects_duplicate_trial_number(self):
        rows = protocol_rows()
        rows[-1]["Trial #"] = "37"
        source = self.root / "invalid.docx"
        write_protocol(source, rows)
        with self.assertRaisesRegex(builder.ValidationError, "exactly 1..38"):
            builder.extract_protocol_trials(source)

    def test_synthetic_xlsx_keeps_114_and_excludes_38_blank_placeholders(self):
        source = self.root / "emotion.xlsx"
        write_workbook(source, emotion_rows(include_placeholders=True))
        sheet, records = builder.extract_emotion_records(source)
        self.assertEqual(sheet, "Sheet1")
        self.assertEqual(len(records), 114)
        self.assertEqual({row["participant_number"] for row in records}, set(range(1, 16)))
        self.assertEqual(records[-1]["source_session_id"], "p15_s10")

    def test_xlsx_rejects_scored_excluded_placeholder(self):
        rows = emotion_rows(include_placeholders=True)
        rows[-1][1] = "5"
        source = self.root / "invalid.xlsx"
        write_workbook(source, rows)
        with self.assertRaisesRegex(builder.ValidationError, "Excluded placeholder"):
            builder.extract_emotion_records(source)

    def test_xlsx_rejects_session_order_and_out_of_range_score(self):
        for corruption in ("order", "score"):
            rows = emotion_rows(include_placeholders=True)
            if corruption == "order":
                rows[0], rows[1] = rows[1], rows[0]
            else:
                rows[0][1] = "10"
            source = self.root / f"{corruption}.xlsx"
            write_workbook(source, rows)
            with self.subTest(corruption=corruption), self.assertRaises(builder.ValidationError):
                builder.extract_emotion_records(source)

    def test_synthetic_114_session_exact_joins_and_frozen_distributions(self):
        conditions, sessions, reports, derived = synthetic_tables()
        builder.validate_final_tables(conditions, sessions, reports, derived)
        expected_ids = {f"P{p}S{s}" for p in range(1, 16) for s in range(1, (10 if p in {5, 10, 15} else 7) + 1)}
        for table in (sessions, reports, derived):
            self.assertEqual({row["session_id"] for row in table}, expected_ids)
        by_id = {row["session_id"]: row for row in sessions}
        self.assertEqual({key: by_id[key]["condition_id"] for key in ("P1S1", "P2S7", "P5S10", "P10S10", "P15S10")}, {
            "P1S1": "C01", "P2S7": "C14", "P5S10": "C38", "P10S10": "C38", "P15S10": "C38",
        })
        self.assertEqual(Counter(row["protocol_risk_index"] for row in derived), {1: 29, 2: 19, 3: 41, 4: 25})
        reports[0]["session_id"] = "P999S1"
        with self.assertRaisesRegex(builder.ValidationError, "do not join exactly"):
            builder.validate_final_tables(conditions, sessions, reports, derived)

    def test_existing_destinations_fail_before_input_reads(self):
        for module, arguments in [
            (builder, ["--emotion-xlsx", "missing.xlsx", "--protocol-docx", "missing.docx"]),
            (audit, ["--config", "missing.json", "--sessions-csv", "missing.csv"]),
        ]:
            output = self.root / module.__name__
            output.mkdir()
            marker = output / "unchanged.csv"
            marker.write_bytes(b"retain this existing file")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = module.main(arguments + ["--output-dir", str(output)])
            self.assertEqual(result, 2)
            self.assertIn("must be new and nonexistent", errors.getvalue())
            self.assertEqual(marker.read_bytes(), b"retain this existing file")
            self.assertEqual(list(output.iterdir()), [marker])

    def test_metadata_writer_exclusively_preserves_existing_file(self):
        output = self.root / "existing.csv"
        output.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            builder.write_csv_exclusive(output, [{"column": "replacement"}])
        self.assertEqual(output.read_bytes(), b"keep")

    def test_failed_validation_does_not_create_destination(self):
        output = self.root / "unwritten"
        with contextlib.redirect_stderr(io.StringIO()):
            result = builder.main(["--emotion-xlsx", "missing.xlsx", "--protocol-docx", "missing.docx", "--output-dir", str(output)])
        self.assertEqual(result, 2)
        self.assertFalse(output.exists())


class InventoryCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = [self.root / name for name in ("rgb", "thermal", "timestamps")]
        for source in self.sources:
            source.mkdir()
        self.config = self.root / "paths.json"
        self.config.write_text(json.dumps({
            "rgb_mmw_exp2_root": str(self.sources[0]),
            "thermal_exp2_root": str(self.sources[1]),
            "provisional_radar_timestamps_root": str(self.sources[2]),
            "raw_data_policy": "read_only",
        }), encoding="utf-8")
        self.sessions = self.root / "sessions.csv"
        write_csv(self.sessions, synthetic_tables()[1])

    def arguments(self, output):
        return audit.parse_args(["--config", str(self.config), "--sessions-csv", str(self.sessions), "--output-dir", str(output)])

    def make_source_layout(self):
        for participant in range(1, 16):
            for session in range(1, (10 if participant in {5, 10, 15} else 7) + 1):
                for root, child in zip(self.sources[:2], ("color_frames", "thermal_frames")):
                    directory = root / f"P_{participant}" / f"P_{participant}_S_{session}" / child
                    directory.mkdir(parents=True)
                    (directory / "frame_1.png").write_bytes(b"not an image; inventory must never open this payload")
        timestamp_root = self.sources[2]
        (timestamp_root / "frame_timestamps").mkdir()
        (timestamp_root / "manifest.json").write_text('{"sessions": []}', encoding="utf-8")

    def test_output_rejected_inside_each_raw_root_before_creating(self):
        for source in self.sources:
            output = source / "forbidden"
            with self.subTest(source=source.name), self.assertRaisesRegex(audit.AuditError, "inside source root"):
                audit.build_inventory(self.arguments(output))
            self.assertFalse(output.exists())

    def test_config_must_explicitly_declare_read_only(self):
        config = json.loads(self.config.read_text())
        del config["raw_data_policy"]
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(audit.AuditError, "raw_data_policy=read_only"):
            audit.build_inventory(self.arguments(self.root / "output"))

    def test_exact_cohort_mapping_rejects_missing_duplicate_or_wrong_join(self):
        original = synthetic_tables()[1]
        for corruption in ("missing", "duplicate", "mapping"):
            rows = [dict(row) for row in original]
            if corruption == "missing":
                rows.pop()
            elif corruption == "duplicate":
                rows[-1] = dict(rows[0])
            else:
                rows[0]["condition_id"] = "C38"
            write_csv(self.sessions, rows)
            with self.subTest(corruption=corruption), self.assertRaises(audit.AuditError):
                audit.read_canonical_sessions(self.sessions, audit.expected_sessions())

    def test_complete_synthetic_inventory_never_opens_sensor_payloads(self):
        self.make_source_layout()
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".mat"}:
                raise AssertionError(f"inventory opened sensor payload: {path}")
            return original_open(path, *args, **kwargs)

        output_directory = self.root / "new_inventory"
        with mock.patch.object(Path, "open", guarded_open):
            records, output = audit.build_inventory(self.arguments(output_directory))
            self.assertFalse(output_directory.exists())
            digest = audit.write_inventory(records, output)
        self.assertEqual(output, output_directory / "session_inventory.csv")
        self.assertEqual(len(records), 114)
        self.assertEqual(sum(row["rgb_frame_count"] for row in records), 114)
        self.assertEqual(sum(row["thermal_frame_count"] for row in records), 114)
        self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest().upper())
        with self.assertRaises(FileExistsError):
            audit.write_inventory(records, output)
        self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest().upper())

    def test_source_layout_rejects_missing_session(self):
        self.make_source_layout()
        (self.sources[0] / "P_1" / "P_1_S_1" / "color_frames" / "frame_1.png").unlink()
        (self.sources[0] / "P_1" / "P_1_S_1" / "color_frames").rmdir()
        (self.sources[0] / "P_1" / "P_1_S_1").rmdir()
        with self.assertRaisesRegex(audit.AuditError, "unexpected session set"):
            audit.build_inventory(self.arguments(self.root / "output"))

    def test_cloud_only_metadata_is_not_opened(self):
        source = self.root / "cloud.csv"
        source.write_text("not opened", encoding="utf-8")
        with mock.patch.object(audit, "windows_file_attributes", return_value=audit.FILE_ATTRIBUTE_OFFLINE):
            result = audit.inspect_sensor_csv(source, "rgb")
        self.assertEqual(result["read_status"], "cloud_only_not_read")


if __name__ == "__main__":
    unittest.main()
