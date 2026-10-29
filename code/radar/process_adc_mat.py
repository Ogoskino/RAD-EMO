#!/usr/bin/env python
"""Process one supplied RAD-EMO ADC MAT frame with the project radar frontend.

python code/radar/process_adc_mat.py --adc-mat frame.mat --output-dir frame_output
python code/radar/process_adc_mat.py --adc-mat frame.mat --output-dir frame_output --materialize

Default: preserve the ADC as saved, with no additional calibration transform.
Use --variant sample_count_rescaled_180_to_80 --calibration calibration.mat
only for the documented alternative frequency-calibration interpretation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import numpy as np

from mmwave_ti_native_ra import (
    CalibrationVariant, FrontendSettings, Waveform,
    build_ti_frame, load_adc_frame, load_calibration_mat,
)
from mmwave_ti_native_ra.contracts import FrontendError
from mmwave_ti_native_ra.frontend import (
    _spatial_arrays, range_doppler_slots, reorder_rx_for_mimo,
)


USEFUL_ARRAYS = (
    "rad_power", "ra_power", "rd_power", "ti_dynamic_ra_magnitude",
    "tdm_corrected_dynamic_ra_magnitude", "ti_static_ra_magnitude",
    "tdm_corrected_static_ra_magnitude",
)
EXTRA_ARRAYS = (
    "azimuth_axis_deg", "doppler_frequency_axis_hz", "velocity_axis_mps",
    "dynamic_valid", "dynamic_selected_doppler_indices_zero_based",
    "dynamic_selected_doppler_mask", "dynamic_excluded_doppler_indices_zero_based",
)


def as_saved_product(adc):
    """Use the unchanged research operations without inventing a calibration profile.

    In build_ti_frame's as_saved branch, calibration metadata does not alter ADC.
    Calling its existing reorder, FFT and spatial routines directly allows users
    to process an already extracted MAT without a separate calibration MAT.
    """
    waveform, settings = Waveform(), FrontendSettings()
    rd = range_doppler_slots(reorder_rx_for_mimo(adc), waveform=waveform, settings=settings)
    arrays = _spatial_arrays(rd, waveform=waveform, settings=settings, include_complex=False)
    for name in USEFUL_ARRAYS:
        arrays[name + "_ti_useful"] = np.ascontiguousarray(
            arrays[name][..., settings.ti_useful_range_slice]
        )
    arrays["range_axis_ti_useful_m"] = np.ascontiguousarray(
        arrays["range_axis_m"][settings.ti_useful_range_slice]
    )
    return {
        "schema": "rad_emo_public_as_saved_frame_v1",
        "calibration_variant": "as_saved", "calibration_profile": None,
        "waveform": waveform.to_dict(), "settings": settings.to_dict(), "arrays": arrays,
    }


def prepare_arrays(product, *, include_rad=False):
    arrays = product["arrays"]
    result = {name: arrays[name + "_ti_useful"] for name in USEFUL_ARRAYS
              if name != "rad_power" or include_rad}
    result["range_axis_m"] = arrays["range_axis_ti_useful_m"]
    result.update({name: arrays[name] for name in EXTRA_ARRAYS})
    return result


def array_metadata(arrays):
    info = {}
    for key, value in arrays.items():
        if key == "rad_power":
            axes, units = ["azimuth", "doppler", "range"], "arbitrary linear power units"
        elif key == "rd_power":
            axes, units = ["doppler", "range"], "arbitrary linear power units"
        elif key == "ra_power" or key.endswith("ra_magnitude"):
            axes = ["azimuth", "range"]
            units = "arbitrary linear power units" if key == "ra_power" else "arbitrary magnitude units"
        elif key == "range_axis_m":
            axes, units = ["range"], "metres"
        elif key == "azimuth_axis_deg":
            axes, units = ["azimuth"], "degrees"
        elif key == "doppler_frequency_axis_hz":
            axes, units = ["doppler"], "hertz"
        elif key == "velocity_axis_mps":
            axes, units = ["doppler"], "metres per second"
        elif key == "dynamic_valid":
            axes, units = [], "boolean support flag"
        elif key.endswith("mask"):
            axes, units = ["doppler"], "boolean selection mask"
        else:
            axes, units = ["selected_or_excluded_index"], "zero-based shifted Doppler bin"
        info[key] = {"shape": list(value.shape), "dtype": str(value.dtype),
                     "axes": axes, "units": units}
    return info


def _identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def materialize_selected_file(source, target):
    """Copy one explicitly selected file; the original strict loader is unchanged."""
    source = Path(source).expanduser()
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as handle, target.open("xb") as output:
        before = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            output.write(block)
            digest.update(block)
            size += len(block)
        after = os.fstat(handle.fileno())
    if _identity(before) != _identity(after) or size != before.st_size:
        raise FrontendError("Selected source changed while its local copy was written")
    return {"name": source.name, "bytes": size, "sha256": digest.hexdigest(),
            "materialized_selected_file": True, "stable_source_handle_verified": True}


def _input_receipt(source, receipt, copied=None):
    result = {"name": Path(source).name, "bytes": receipt["bytes"],
              "sha256": receipt["sha256"].lower(),
              "materialized_selected_file": copied is not None,
              "strict_loader_path_and_snapshot_checks_passed": True}
    if copied and copied["sha256"] != result["sha256"]:
        raise FrontendError("Materialized input hash differs from the actual parser snapshot")
    if copied:
        result["stable_source_handle_verified"] = copied["stable_source_handle_verified"]
    return result


def process_frame(adc_path, output_dir, *, variant="as_saved", calibration=None,
                  materialize=False, scratch_dir=None, include_rad=False):
    variant = CalibrationVariant(variant)
    if variant is CalibrationVariant.SAMPLE_COUNT_RESCALED and calibration is None:
        raise FrontendError("The rescaled interpretation requires --calibration")
    output = Path(output_dir)
    if output.exists():
        raise FrontendError("Output directory already exists; choose a new directory")
    if scratch_dir is not None and not materialize:
        raise FrontendError("--scratch-dir requires --materialize")
    # Temporary storage contains only the explicitly supplied frame/calibration.
    with tempfile.TemporaryDirectory(prefix="rad_emo_frame_", dir=scratch_dir) as scratch:
        copied_adc = copied_calibration = None
        adc_source, calibration_source = Path(adc_path), Path(calibration) if calibration else None
        if materialize:
            adc_source = Path(scratch) / "adc.mat"
            copied_adc = materialize_selected_file(adc_path, adc_source)
            if calibration:
                calibration_source = Path(scratch) / "calibration.mat"
                copied_calibration = materialize_selected_file(calibration, calibration_source)
        adc, adc_info = load_adc_frame(adc_source)
        adc_receipt = _input_receipt(adc_path, adc_info, copied_adc)
        calibration_receipt = None
        if calibration_source is not None:
            matrix, profile, calibration_info = load_calibration_mat(calibration_source)
            calibration_receipt = _input_receipt(calibration, calibration_info, copied_calibration)
            product = build_ti_frame(adc, matrix, variant, calibration_profile=profile)
        else:
            product = as_saved_product(adc)
        arrays = prepare_arrays(product, include_rad=include_rad)
        report = {
            "schema": "rad_emo_single_frame_processing_v1",
            "input": adc_receipt, "calibration_input": calibration_receipt,
            "input_python_axes": ["tx_storage_slot", "raw_rx", "loop", "sample"],
            "input_python_shape": list(adc.shape),
            "input_matlab_axes": ["sample", "loop", "raw_rx", "tx_storage_slot"],
            "input_matlab_shape": [80, 25, 16, 12],
            "calibration_variant": variant.value,
            "calibration_profile": product["calibration_profile"],
            "upstream_frequency_and_phase_calibration_reapplied": False,
            "sample_count_frequency_ratio_applied": variant is CalibrationVariant.SAMPLE_COUNT_RESCALED,
            "waveform": product["waveform"], "settings": product["settings"],
            "range_bins_zero_based_inclusive": [5, 108],
            "normalisation": "none; unscaled FFTs; no per-frame normalisation",
            "dynamic_policy": {
                "excluded_shifted_doppler_bins": [15, 16, 17],
                "empty_selection": "dynamic_valid false; dynamic magnitude arrays contain zeros",
                "ti_dynamic_map_tdm_corrected": False,
                "tdm_corrected_companion_map_supplied": True,
            },
            "rad_power_saved": include_rad, "arrays": array_metadata(arrays),
            "source_files_modified": False, "model_fitting_performed": False,
            "files": ["representations.npz", "processing.json"],
        }
        output.mkdir(parents=True, exist_ok=False)
        np.savez_compressed(output / "representations.npz", **arrays)
        report["representations_sha256"] = hashlib.sha256(
            (output / "representations.npz").read_bytes()).hexdigest()
        with (output / "processing.json").open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adc-mat", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for the two output files")
    parser.add_argument("--variant", choices=[v.value for v in CalibrationVariant], default="as_saved")
    parser.add_argument("--calibration", type=Path, help="Matching calibration MAT for the optional rescaled interpretation")
    parser.add_argument("--include-rad", action="store_true", help="Also save this frame's [256,32,104] RAD power array")
    parser.add_argument("--materialize", action="store_true", help="Copy only selected input files into regular local temporary files")
    parser.add_argument("--scratch-dir", type=Path, help="Existing regular local temp parent outside cloud/reparse folders")
    args = parser.parse_args(argv)
    try:
        result = process_frame(args.adc_mat, args.output_dir, variant=args.variant,
                               calibration=args.calibration, materialize=args.materialize,
                               scratch_dir=args.scratch_dir, include_rad=args.include_rad)
        print(json.dumps({"status": "processed_one_frame", "input_sha256": result["input"]["sha256"],
                          "variant": result["calibration_variant"], "files": result["files"]}, indent=2))
        return 0
    except (FrontendError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        print("For cloud or reparse-point paths, use a regular local copy or --materialize.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
