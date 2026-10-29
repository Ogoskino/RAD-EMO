"""Synthetic fixtures only; no detector imports, model downloads or image decoding."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

import yolov8_export_2d_annotations as exporter


def box(coordinates, confidence=0.9, class_id=0):
    return SimpleNamespace(
        cls=SimpleNamespace(item=lambda: class_id),
        conf=SimpleNamespace(item=lambda: confidence),
        xyxy=[SimpleNamespace(tolist=lambda: list(coordinates))],
    )


def result(*boxes):
    return SimpleNamespace(boxes=list(boxes), orig_shape=(480, 640))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.session = self.base / "input/P_999/P_999_S_1"
        self.frames = self.session / "color_frames"
        self.frames.mkdir(parents=True)
        self.output = self.base / "output"

    def call_export(self, model, **overrides):
        arguments = dict(model=model, session_dir=self.session, label="PWP", min_conf=0.25,
                         imgsz=640, select="largest", overwrite=False, include_empty=True,
                         output_root=self.output)
        arguments.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return exporter.export_session(**arguments)

    def test_person_filter_and_original_area_confidence_selection(self):
        detections = result(box((0, 0, 100, 100), 0.99, class_id=67),
                            box((0, 0, 100, 100), 0.1), box((0, 0, 10, 10), 0.95),
                            box((0, 0, 20, 20), 0.6), box((1, 1, 21, 21), 0.8))
        self.assertEqual(exporter.select_person_box(detections, 0.25, "largest"),
                         (1.0, 1.0, 21.0, 21.0, 0.8))
        self.assertEqual(exporter.select_person_box(detections, 0.25, "highest_conf"),
                         (0.0, 0.0, 10.0, 10.0, 0.95))

    def test_box_clamping_keeps_original_pixel_schema(self):
        self.assertEqual(exporter.clamp_box(-2, -4, 700, 600, 640, 480),
                         {"x": 0.0, "y": 0.0, "width": 640.0, "height": 480.0})
        self.assertEqual(exporter.clamp_box(20, 30, 10, 20, 640, 480),
                         {"x": 20.0, "y": 30.0, "width": 0.0, "height": 0.0})

    def test_empty_and_nonperson_detections_have_no_selected_box(self):
        self.assertIsNone(exporter.select_person_box(result(), 0.25, "largest"))
        self.assertIsNone(exporter.select_person_box(result(box((0, 0, 1, 1), class_id=1)), 0.25, "largest"))

    def test_export_preserves_schema_and_writes_only_separate_output(self):
        (self.frames / "color_frame_0.png").write_bytes(b"synthetic filename fixture, not an image")
        original = self.session / exporter.OUTPUT_NAME
        original.write_text("original annotation sentinel", encoding="utf-8")
        model = Mock()
        model.predict.return_value = [result(box((10, 20, 110, 220)))]
        images, detected, target = self.call_export(model)
        self.assertEqual((images, detected), (1, 1))
        self.assertEqual(target, self.output / "P_999/P_999_S_1" / exporter.OUTPUT_NAME)
        payload = json.loads(target.read_text())
        self.assertEqual(payload, {"classes": exporter.CLASSES, "images": [{
            "image": "color_frame_0.png", "annotations": [{"class": "PWP", "boundingBox": {
                "x": 10.0, "y": 20.0, "width": 100.0, "height": 200.0}}]}]})
        self.assertEqual(original.read_text(), "original annotation sentinel")
        model.predict.assert_called_once_with(str(self.frames / "color_frame_0.png"),
                                               classes=[0], conf=0.25, imgsz=640, verbose=False)

    def test_original_lexicographic_filename_order_and_empty_policy(self):
        for name in ("color_frame_2.png", "color_frame_10.png", "ignored.txt"):
            (self.frames / name).write_bytes(b"synthetic filename fixture")
        model = Mock()
        model.predict.side_effect = [[result()], [result(box((0, 0, 10, 10)))]]
        images, detected, target = self.call_export(model)
        payload = json.loads(target.read_text())
        self.assertEqual((images, detected), (2, 1))
        self.assertEqual([entry["image"] for entry in payload["images"]],
                         ["color_frame_10.png", "color_frame_2.png"])
        self.assertEqual(payload["images"][0]["annotations"], [])
        model.predict.side_effect = [[result()], [result(box((0, 0, 10, 10)))]]
        _, _, target = self.call_export(model, overwrite=True, include_empty=False)
        self.assertEqual(len(json.loads(target.read_text())["images"]), 1)

    def test_existing_output_skips_without_touching_detector(self):
        target = exporter.output_path_for_session(self.session, self.output)
        target.parent.mkdir(parents=True)
        target.write_text("existing export")
        model = Mock()
        self.assertEqual(self.call_export(model), (0, 0, target))
        model.predict.assert_not_called()
        self.assertEqual(target.read_text(), "existing export")

    def test_canonical_and_explicit_label_csvs_use_the_same_classes(self):
        label_map = self.base / "labels.csv"
        label_map.write_text("session,label\nP999S1,PWK\n", encoding="utf-8")
        self.assertEqual(exporter.load_label_map(str(label_map)), {"P_999_S_1": "PWK"})
        sessions = self.base / "sessions.csv"
        sessions.write_text("source_session_id,session_id,condition_code\nP_999_S_1,P999S1,PWPK\n", encoding="utf-8")
        self.assertEqual(exporter.load_label_map(str(sessions), canonical=True), {"P_999_S_1": "PWPK"})
        sessions.write_text("session_id,condition_code\nP999S1,PO\n", encoding="utf-8")
        self.assertEqual(exporter.load_label_map(str(sessions), canonical=True), {"P_999_S_1": "PO"})

    def test_bad_or_conflicting_classes_and_missing_labels_are_rejected(self):
        path = self.base / "labels.csv"
        for content in ("session,label\nP999S1,unknown\n",
                        "session,label\nP999S1,PWK\nP_999_S_1,PWP\n"):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                exporter.load_label_map(str(path))
        with self.assertRaises(ValueError):
            exporter.get_label_for_session(self.session, None, {})

    def test_input_and_output_cannot_overlap(self):
        with self.assertRaises(ValueError):
            exporter.validate_roots(self.base / "input", self.base / "input/results")
        with self.assertRaises(ValueError):
            exporter.validate_roots(self.session, self.base / "input")
        exporter.validate_roots(self.base / "input", self.output)
        with self.assertRaises(ValueError):
            exporter.output_path_for_session(self.session, self.base / "input")

    def test_session_search_supports_single_recording_or_collection_root(self):
        self.assertEqual(exporter.find_session_dirs(self.session), [self.session.resolve()])
        self.assertEqual(exporter.find_session_dirs(self.base / "input"), [self.session.resolve()])

    def test_help_documents_all_parameters_without_detector_dependencies(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            exporter.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--output-root", output.getvalue())
        self.assertIn("--sessions-csv", output.getvalue())
        self.assertIn("--include-empty", output.getvalue())


if __name__ == "__main__":
    unittest.main()
