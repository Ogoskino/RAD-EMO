"""Metadata-only batch dispatch fixtures; the ADC processor is mocked."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import process_adc_recording as batch
from mmwave_ti_native_ra.contracts import FrontendError


class RecordingDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "recording"
        self.source.mkdir()
        self.output = self.root / "output"

    def tearDown(self):
        self.temp.cleanup()

    def names(self, *names):
        for name in names:
            (self.source / name).touch()

    def test_numeric_order_child_names_and_options_use_original_processor(self):
        self.names("adcData_frame_0010.mat", "adcData_frame_0002.mat", "adcData_frame_0001.mat", "other.mat")
        fake = {"input": {"sha256": "a" * 64}}
        with mock.patch.object(batch, "process_frame", return_value=fake) as processor:
            result = batch.process_recording(self.source, self.output, materialize=True, include_rad=True)
        self.assertEqual([c.args[0].name for c in processor.call_args_list],
                         ["adcData_frame_0001.mat", "adcData_frame_0002.mat", "adcData_frame_0010.mat"])
        for call in processor.call_args_list:
            self.assertEqual(call.args[1], self.output / call.args[0].stem)
            self.assertTrue(call.kwargs["materialize"])
            self.assertTrue(call.kwargs["include_rad"])
            self.assertEqual(call.kwargs["variant"], "as_saved")
        self.assertEqual(result["processed_frames"], 3)
        self.assertEqual(json.loads((self.output / "recording_processing.json").read_text())["processed_frames"], 3)

    def test_empty_recording_rejected_before_output_or_processing(self):
        self.names("notes.txt", "not_a_frame.mat")
        with mock.patch.object(batch, "process_frame") as processor:
            with self.assertRaisesRegex(FrontendError, "No adcData_frame"):
                batch.process_recording(self.source, self.output)
            processor.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_output_and_duplicate_numeric_id_rejected(self):
        self.names("adcData_frame_0001.mat")
        self.output.mkdir()
        with mock.patch.object(batch, "process_frame") as processor:
            with self.assertRaisesRegex(FrontendError, "already exists"):
                batch.process_recording(self.source, self.output)
            processor.assert_not_called()
        self.names("adcData_frame_1.mat")
        with self.assertRaisesRegex(FrontendError, "duplicate"):
            batch.process_recording(self.source, self.root / "another_output")


if __name__ == "__main__":
    unittest.main()
