#!/usr/bin/env python
"""Process one recording's ADC MAT files sequentially in numeric frame order.

python code/radar/process_adc_recording.py --adc-dir recording_mat_folder --output-dir output_recording

Uses process_adc_mat.process_frame; no model fitting or recursive dataset scan.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from mmwave_ti_native_ra import CalibrationVariant
from mmwave_ti_native_ra.contracts import FrontendError
from process_adc_mat import process_frame


FRAME_NAME = re.compile(r"adcData_frame_(\d+)\.mat\Z", re.IGNORECASE)


def recording_frames(directory):
    source = Path(directory)
    if not source.is_dir():
        raise FrontendError("--adc-dir must be one existing recording directory")
    numbered = []
    seen_numbers, seen_names = set(), set()
    for path in source.iterdir():
        match = FRAME_NAME.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        number = int(match.group(1))
        if number in seen_numbers or path.stem.casefold() in seen_names:
            raise FrontendError("Ambiguous duplicate ADC frame number or output name")
        seen_numbers.add(number)
        seen_names.add(path.stem.casefold())
        numbered.append((number, path))
    if not numbered:
        raise FrontendError("No adcData_frame_<number>.mat files found; nothing processed")
    return [path for _, path in sorted(numbered, key=lambda pair: pair[0])]


def process_recording(adc_dir, output_dir, *, materialize=False, include_rad=False,
                      variant="as_saved", calibration=None, scratch_dir=None):
    output = Path(output_dir)
    if output.exists():
        raise FrontendError("Output directory already exists; choose a new directory")
    selected = CalibrationVariant(variant)
    if selected is CalibrationVariant.SAMPLE_COUNT_RESCALED and calibration is None:
        raise FrontendError("The rescaled interpretation requires --calibration")
    if scratch_dir is not None and not materialize:
        raise FrontendError("--scratch-dir requires --materialize")
    frames = recording_frames(adc_dir)
    output.mkdir(parents=True, exist_ok=False)
    completed = []
    for source in frames:
        report = process_frame(source, output / source.stem,
                               materialize=materialize, include_rad=include_rad,
                               variant=selected.value, calibration=calibration,
                               scratch_dir=scratch_dir)
        completed.append({"frame_filename": source.name,
                          "output_relative_directory": source.stem,
                          "input_sha256": report["input"]["sha256"]})
    result = {"schema": "rad_emo_recording_processing_v1",
              "frame_order": "numeric suffix in adcData_frame_<number>.mat",
              "processed_frames": len(completed), "calibration_variant": selected.value,
              "rad_power_saved": include_rad, "materialize_selected_files": materialize,
              "frames": completed, "model_fitting_performed": False,
              "source_files_modified": False}
    with (output / "recording_processing.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adc-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory containing one child directory per frame")
    parser.add_argument("--materialize", action="store_true", help="Materialize only the current frame in regular local temporary storage")
    parser.add_argument("--include-rad", action="store_true", help="Also save each frame's [256,32,104] RAD power array")
    parser.add_argument("--variant", choices=[v.value for v in CalibrationVariant], default="as_saved")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--scratch-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = process_recording(args.adc_dir, args.output_dir,
                                   materialize=args.materialize, include_rad=args.include_rad,
                                   variant=args.variant, calibration=args.calibration,
                                   scratch_dir=args.scratch_dir)
        print(json.dumps({"status": "processed_recording", "processed_frames": report["processed_frames"],
                          "variant": report["calibration_variant"],
                          "manifest": "recording_processing.json"}, indent=2))
        return 0
    except (FrontendError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
