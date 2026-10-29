"""Per-frame transient RAD to compact global/camera-ROI reductions."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from .contracts import CalibrationProfile, CalibrationVariant, FrontendSettings, Waveform
from .frontend import build_ti_frame
from .roi import extract_metric_roi


def build_compact_frame(
    adc: np.ndarray,
    range_mat: np.ndarray,
    variant: CalibrationVariant | str,
    *,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    metric_rois: Sequence[Mapping[str, Any]] = (),
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> dict[str, Any]:
    """Build full RAD transiently and return only compact useful-range views.

    ``metric_rois`` must come from a transform already fitted on the allowed
    training fold. No box coordinates or knife labels are consumed here.
    """

    product = build_ti_frame(
        adc,
        range_mat,
        variant,
        calibration_profile=calibration_profile,
        waveform=waveform,
        settings=settings,
    )
    arrays = product["arrays"]
    useful_power = arrays["rad_power_ti_useful"]
    roi_rows: list[dict[str, Any]] = []
    for index, specification in enumerate(metric_rois):
        crop = extract_metric_roi(
            useful_power,
            arrays["azimuth_axis_deg"],
            arrays["range_axis_ti_useful_m"],
            azimuth_bounds_deg=tuple(specification["azimuth_bounds_deg"]),
            range_bounds_m=tuple(specification["range_bounds_m"]),
            output_azimuth_bins=int(specification.get("output_azimuth_bins", 64)),
            output_range_bins=int(specification.get("output_range_bins", 32)),
        )
        roi_rows.append(
            {
                "roi_id": str(specification.get("roi_id", index)),
                **crop,
            }
        )
    compact = {
        "schema": "rad_emo_ti_native_compact_stream_frame_v1",
        "calibration_variant": product["calibration_variant"],
        "calibration_profile": product["calibration_profile"],
        "global_rd_power": np.ascontiguousarray(
            np.mean(useful_power, axis=0), dtype=np.float32
        ),
        "ti_dynamic_ra_magnitude": np.ascontiguousarray(
            arrays["ti_dynamic_ra_magnitude_ti_useful"], dtype=np.float32
        ),
        "tdm_corrected_dynamic_ra_magnitude": np.ascontiguousarray(
            arrays["tdm_corrected_dynamic_ra_magnitude_ti_useful"], dtype=np.float32
        ),
        "dynamic_selected_doppler_indices_zero_based": np.ascontiguousarray(
            arrays["dynamic_selected_doppler_indices_zero_based"], dtype=np.int16
        ),
        "dynamic_valid": bool(np.asarray(arrays["dynamic_valid"]).item()),
        "azimuth_axis_deg": np.ascontiguousarray(arrays["azimuth_axis_deg"], dtype=np.float64),
        "range_axis_m": np.ascontiguousarray(arrays["range_axis_ti_useful_m"], dtype=np.float64),
        "doppler_frequency_axis_hz": np.ascontiguousarray(
            arrays["doppler_frequency_axis_hz"], dtype=np.float64
        ),
        "velocity_axis_mps": np.ascontiguousarray(arrays["velocity_axis_mps"], dtype=np.float64),
        "rois": roi_rows,
        "axes": {
            "global_rd_power": ["doppler", "range"],
            "ti_dynamic_ra_magnitude": ["azimuth", "range"],
            "roi_rad_power": ["azimuth", "doppler", "range"],
        },
        "full_rad_persisted": False,
        "labels_or_boxes_consumed": False,
    }
    # ``product`` and its full [256,32,128] RAD leave scope here.
    return compact


def build_paired_compact_frame(
    adc: np.ndarray,
    range_mat: np.ndarray,
    *,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    metric_rois: Sequence[Mapping[str, Any]] = (),
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> dict[str, Any]:
    """Stream both arms sequentially so their full RAD cubes never coexist."""

    variants: dict[str, Any] = {}
    for variant in CalibrationVariant:
        variants[variant.value] = build_compact_frame(
            adc,
            range_mat,
            variant,
            calibration_profile=calibration_profile,
            metric_rois=metric_rois,
            waveform=waveform,
            settings=settings,
        )
    return {
        "schema": "rad_emo_ti_native_paired_compact_stream_frame_v1",
        "variants": variants,
        "identical_adc_frame_used_for_both_variants": True,
        "full_variant_rad_cubes_coexisted": False,
    }
