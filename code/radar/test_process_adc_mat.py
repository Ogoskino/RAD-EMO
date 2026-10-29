"""Synthetic HDF5/MAT fixtures only; no participant files or model fitting."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import h5py
import numpy as np
from scipy.io import savemat

import process_adc_mat as cli
from mmwave_ti_native_ra import ADC_SHAPE, CalibrationProfile, build_ti_frame, load_adc_frame
from mmwave_ti_native_ra.contracts import FrontendError
from mmwave_ti_native_ra.io import _reject_reparse_or_symlink_components


class SyntheticFrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        sample, loop = np.arange(80), np.arange(25)
        tone = np.exp(2j * np.pi * (8 * sample[None, :] / 128 + 3 * loop[:, None] / 32))
        self.adc = np.broadcast_to(tone[None, None, :, :], ADC_SHAPE).astype(np.complex64).copy()
        self.frame = self.root / "adcData_frame_0001.mat"
        self.write_adc(self.frame, self.adc)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_adc(path, adc):
        compound = np.empty(adc.shape, dtype=[("real", "<f8"), ("imag", "<f8")])
        compound["real"], compound["imag"] = adc.real, adc.imag
        with h5py.File(path, "w") as handle:
            handle.create_dataset("adcData", data=compound)

    def calibration(self):
        path = self.root / "calibration.mat"
        matrix = np.arange(12 * 16, dtype=np.float64).reshape(12, 16) / 20
        savemat(path, {"calibResult": {"RangeMat": matrix,
                                      "Rx_fft": np.zeros((900, 16, 12), dtype=np.float32)},
                       "params": {"Sampling_Rate_sps": 8e6, "Slope_MHzperus": 78.986}})
        return path, matrix

    def test_compound_complex_loader_preserves_axis_positions_and_hash(self):
        value, receipt = load_adc_frame(self.frame)
        self.assertEqual(value.shape, (12, 16, 25, 80))
        self.assertEqual(value.dtype, np.complex64)
        np.testing.assert_array_equal(value, self.adc)
        self.assertEqual(receipt["sha256"].lower(), hashlib.sha256(self.frame.read_bytes()).hexdigest())

    def test_as_saved_matches_original_frontend_and_tone_bins(self):
        report = cli.process_frame(self.frame, self.root / "output")
        original = build_ti_frame(self.adc, np.ones((12, 16)), "as_saved",
                                  calibration_profile=CalibrationProfile(8e6, 78.986e12))
        with np.load(self.root / "output/representations.npz", allow_pickle=False) as result:
            self.assertNotIn("rad_power", result.files)
            self.assertEqual(result["rd_power"].shape, (32, 104))
            self.assertEqual(result["ra_power"].shape, (256, 104))
            self.assertEqual(np.unravel_index(np.argmax(result["rd_power"]), (32, 104)), (19, 3))
            np.testing.assert_array_equal(result["ra_power"], original["arrays"]["ra_power_ti_useful"])
            np.testing.assert_array_equal(result["rd_power"], original["arrays"]["rd_power_ti_useful"])
            self.assertTrue(bool(result["dynamic_valid"]))
        self.assertIsNone(report["calibration_profile"])
        self.assertFalse(report["upstream_frequency_and_phase_calibration_reapplied"])
        self.assertEqual(report["arrays"]["rd_power"]["axes"], ["doppler", "range"])

    def test_selected_materialization_binds_source_hash_and_optional_rad(self):
        before = self.frame.read_bytes()
        report = cli.process_frame(self.frame, self.root / "copied_output", materialize=True, include_rad=True)
        self.assertTrue(report["input"]["materialized_selected_file"])
        self.assertEqual(report["input"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.frame.read_bytes(), before)
        with np.load(self.root / "copied_output/representations.npz", allow_pickle=False) as data:
            self.assertEqual(data["rad_power"].shape, (256, 32, 104))
        text = (self.root / "copied_output/processing.json").read_text()
        self.assertNotIn(str(self.root), text)

    def test_rescaled_variant_requires_actual_calibration_and_matches_research_path(self):
        with self.assertRaisesRegex(FrontendError, "requires --calibration"):
            cli.process_frame(self.frame, self.root / "missing", variant="sample_count_rescaled_180_to_80")
        calibration, matrix = self.calibration()
        report = cli.process_frame(self.frame, self.root / "rescaled", calibration=calibration,
                                   variant="sample_count_rescaled_180_to_80")
        expected = build_ti_frame(self.adc, matrix, "sample_count_rescaled_180_to_80",
                                  calibration_profile=CalibrationProfile(8e6, 78.986e12))
        with np.load(self.root / "rescaled/representations.npz", allow_pickle=False) as result:
            np.testing.assert_array_equal(result["rd_power"], expected["arrays"]["rd_power_ti_useful"])
        self.assertTrue(report["sample_count_frequency_ratio_applied"])
        self.assertFalse(report["upstream_frequency_and_phase_calibration_reapplied"])

    def test_zero_frame_has_explicit_empty_dynamic_support(self):
        self.write_adc(self.frame, np.zeros(ADC_SHAPE, dtype=np.complex64))
        cli.process_frame(self.frame, self.root / "zero")
        with np.load(self.root / "zero/representations.npz", allow_pickle=False) as data:
            self.assertFalse(bool(data["dynamic_valid"]))
            self.assertEqual(data["dynamic_selected_doppler_indices_zero_based"].size, 0)
            self.assertFalse(np.any(data["ti_dynamic_ra_magnitude"]))

    def test_shape_real_input_and_existing_output_rejected(self):
        invalid = self.root / "invalid.mat"
        for data in [np.zeros((80, 25, 16, 12), dtype=np.complex64), np.zeros(ADC_SHAPE, dtype=np.float32)]:
            with h5py.File(invalid, "w") as handle:
                handle.create_dataset("adcData", data=data)
            with self.assertRaises(FrontendError):
                cli.process_frame(invalid, self.root / "invalid_output")
        out = self.root / "already_exists"
        out.mkdir()
        with self.assertRaisesRegex(FrontendError, "already exists"):
            cli.process_frame(self.frame, out)

    def test_original_reparse_protection_remains_active(self):
        fake = type("Metadata", (), {"st_mode": 0o100000, "st_file_attributes": 0x400})()
        with mock.patch.object(Path, "lstat", return_value=fake):
            with self.assertRaisesRegex(FrontendError, "symlink or reparse"):
                _reject_reparse_or_symlink_components(Path("a/frame.mat"))


if __name__ == "__main__":
    unittest.main()
