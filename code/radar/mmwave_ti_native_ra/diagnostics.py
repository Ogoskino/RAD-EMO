"""Strictly paired, label-free calibration-variant diagnostics."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Sequence

import numpy as np

from .contracts import CalibrationProfile, CalibrationVariant, FrontendSettings, Waveform
from .frontend import (
    _uniform_angle_fft,
    apply_sample_count_calibration_variant,
    extract_robust_aperture,
    range_axis_m,
    range_doppler_slots,
    reorder_rx_for_mimo,
    tdm_correct_rd,
    ti_dynamic_doppler_selection,
    virtual_geometry,
)


# Frozen before reading the real diagnostic values. The rule is intentionally
# conservative: neither arm wins from a small or mixed numerical movement.
DECISION_RULE: dict[str, Any] = {
    "schema": "rad_emo_ti_calibration_variant_decision_rule_v1",
    "comparison": "aggregate_median_rescaled_minus_as_saved_on_identical_frames",
    "minimum_directional_metrics_improved": 4,
    "maximum_directional_metrics_degraded": 1,
    "otherwise": "inconclusive",
    "metrics": {
        "duplicate_complex_coherence": {"direction": "higher", "absolute_tolerance": 0.005},
        "disjoint_subarray_map_cosine": {"direction": "higher", "absolute_tolerance": 0.01},
        "disjoint_peak_azimuth_difference_deg": {"direction": "lower", "absolute_tolerance": 1.0},
        "disjoint_peak_range_difference_m": {"direction": "lower", "absolute_tolerance": 0.11869118847013447},
        "focus_normalized_entropy": {"direction": "lower", "absolute_tolerance": 0.005},
        "focus_peak_to_maximum_sidelobe_db": {"direction": "higher", "absolute_tolerance": 0.25},
    },
    "selected_doppler_bin_count_role": "reported_descriptive_not_a_directional_vote",
}


def _processed_corrected_rd(
    adc: np.ndarray,
    range_mat: np.ndarray,
    variant: CalibrationVariant,
    profile: CalibrationProfile,
    waveform: Waveform,
    settings: FrontendSettings,
) -> np.ndarray:
    interpreted = apply_sample_count_calibration_variant(
        adc,
        range_mat,
        variant,
        calibration_profile=profile,
        waveform=waveform,
    )
    rd = range_doppler_slots(
        reorder_rx_for_mimo(interpreted), waveform=waveform, settings=settings
    )
    return tdm_correct_rd(rd, waveform=waveform, settings=settings)


def _pooled_common_support(
    corrected_by_variant: Mapping[str, np.ndarray],
    *,
    settings: FrontendSettings,
    support_cells: int,
) -> dict[str, Any]:
    geometry = virtual_geometry()
    zero_flat = np.flatnonzero(geometry["elevation_half_lambda"] == 0)
    range_indices = np.arange(settings.ti_useful_range_start, settings.ti_useful_range_stop)
    doppler_indices = np.concatenate((np.arange(0, 15), np.arange(18, 32)))
    powers = []
    for corrected in corrected_by_variant.values():
        flat = corrected.reshape(corrected.shape[0], 32, 192)
        selected = flat[np.ix_(range_indices, doppler_indices, zero_flat)]
        powers.append(np.mean(np.abs(selected) ** 2, axis=2))
    pooled = np.mean(np.stack(powers, axis=0), axis=0)
    count = min(int(support_cells), int(pooled.size))
    if count <= 0 or not np.any(pooled > 0):
        raise ValueError("pooled variants have no nonzero common RD support")
    # Stable full ordering avoids variant/tie-dependent argpartition behavior.
    chosen = np.argsort(pooled.ravel(), kind="mergesort")[-count:]
    chosen.sort()
    local_range, local_doppler = np.unravel_index(chosen, pooled.shape)
    ranges = range_indices[local_range].astype(np.int16)
    dopplers = doppler_indices[local_doppler].astype(np.int16)
    weights = pooled[local_range, local_doppler].astype("<f8")
    pairs = np.stack((ranges, dopplers), axis=1).astype("<i2")
    return {
        "range_indices": ranges,
        "doppler_indices": dopplers,
        "weights": weights,
        "receipt": {
            "definition": "top_pooled_mean_incoherent_power_over_both_variants_and_all_zero_elevation_channels",
            "cell_count": count,
            "indices_sha256": hashlib.sha256(pairs.tobytes(order="C")).hexdigest().upper(),
            "common_weights_sha256": hashlib.sha256(weights.tobytes(order="C")).hexdigest().upper(),
            "range_bounds_zero_based_inclusive": [
                settings.ti_useful_range_start,
                settings.ti_useful_range_stop - 1,
            ],
            "eligible_doppler_indices_zero_based": [
                *range(0, 15),
                *range(18, 32),
            ],
        },
    }


def _duplicate_coherence_on_common_support(
    corrected_rd: np.ndarray,
    support: Mapping[str, Any],
) -> dict[str, Any]:
    geometry = virtual_geometry()
    zero_flat = np.flatnonzero(geometry["elevation_half_lambda"] == 0)
    positions = geometry["azimuth_half_lambda"][zero_flat]
    groups = [
        zero_flat[positions == position]
        for position in np.unique(positions)
        if np.count_nonzero(positions == position) > 1
    ]
    flat = corrected_rd.reshape(corrected_rd.shape[0], 32, 192)
    ranges = np.asarray(support["range_indices"])
    dopplers = np.asarray(support["doppler_indices"])
    common_weights = np.asarray(support["weights"], dtype=np.float64)
    group_scores: list[float] = []
    for group in groups:
        samples = flat[ranges[:, None], dopplers[:, None], group[None, :]]
        incoherent = np.sum(np.abs(samples) ** 2, axis=1)
        denominator = len(group) * incoherent
        valid = denominator > 0
        if not np.any(valid):
            continue
        per_cell = np.abs(np.sum(samples[valid], axis=1)) ** 2 / denominator[valid]
        group_scores.append(float(np.average(per_cell, weights=common_weights[valid])))
    return {
        "definition": "common_weighted_abs_sum_squared_over_n_times_incoherent_power",
        "duplicate_position_group_count": len(groups),
        "duplicate_channel_count": int(sum(len(group) for group in groups)),
        "valid_group_count": len(group_scores),
        "mean_group_coherence": float(np.mean(group_scores)) if group_scores else None,
        "minimum_group_coherence": float(np.min(group_scores)) if group_scores else None,
        "maximum_group_coherence": float(np.max(group_scores)) if group_scores else None,
    }


def _selection_from_proxy(proxy: np.ndarray) -> np.ndarray:
    candidates = np.concatenate((np.arange(0, 15), np.arange(18, 32))).astype(np.int16)
    peak = float(np.max(proxy[candidates], initial=0.0))
    if peak <= 0 or not np.isfinite(peak):
        return np.empty(0, dtype=np.int16)
    return candidates[proxy[candidates] > 0.5 * peak]


def _common_doppler_selection(
    corrected_by_variant: Mapping[str, np.ndarray],
) -> dict[str, Any]:
    proxies = []
    native: dict[str, list[int]] = {}
    for name, corrected in corrected_by_variant.items():
        selection = ti_dynamic_doppler_selection(extract_robust_aperture(corrected))
        proxy = np.asarray(selection["proxy"], dtype=np.float64)
        proxies.append(proxy)
        native[name] = [int(item) for item in selection["selected_indices_zero_based"]]
    pooled_proxy = np.mean(np.stack(proxies, axis=0), axis=0)
    common = _selection_from_proxy(pooled_proxy)
    common_bytes = common.astype("<i2").tobytes()
    return {
        "indices": common,
        "receipt": {
            "definition": "TI_half_peak_rule_on_mean_of_both_variant_global_abs_RD_proxies",
            "indices_zero_based": [int(item) for item in common],
            "indices_sha256": hashlib.sha256(common_bytes).hexdigest().upper(),
            "count": int(common.size),
            "excluded_indices_zero_based": [15, 16, 17],
            "native_indices_by_variant": native,
        },
    }


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float64).ravel()
    b = np.asarray(right, dtype=np.float64).ravel()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator > 0 else 0.0


def _subarray_on_bins(
    corrected_rd: np.ndarray,
    bins: np.ndarray,
    *,
    settings: FrontendSettings,
) -> dict[str, Any]:
    if bins.size == 0:
        return {"valid": False, "reason": "dynamic_doppler_selection_empty"}
    aperture = extract_robust_aperture(corrected_rd)
    left_rad, _, azimuth_axis = _uniform_angle_fft(aperture[:, :, :43], settings=settings)
    right_rad, _, _ = _uniform_angle_fft(aperture[:, :, 43:], settings=settings)
    useful = settings.ti_useful_range_slice
    left = np.sum(np.abs(left_rad[:, bins, useful]), axis=1)
    right = np.sum(np.abs(right_rad[:, bins, useful]), axis=1)
    left_peak = np.unravel_index(np.argmax(left), left.shape)
    right_peak = np.unravel_index(np.argmax(right), right.shape)
    ranges = range_axis_m(settings.range_fft_size)[useful]
    return {
        "valid": True,
        "split": "contiguous_positions_0_to_42_vs_43_to_85",
        "normalized_map_cosine": _cosine(left, right),
        "absolute_peak_azimuth_difference_deg": float(
            abs(azimuth_axis[left_peak[0]] - azimuth_axis[right_peak[0]])
        ),
        "absolute_peak_range_difference_m": float(
            abs(ranges[left_peak[1]] - ranges[right_peak[1]])
        ),
    }


def _focus_on_bins(
    corrected_rd: np.ndarray,
    bins: np.ndarray,
    *,
    settings: FrontendSettings,
) -> dict[str, Any]:
    if bins.size == 0:
        return {"valid": False, "reason": "dynamic_doppler_selection_empty"}
    rad, _, _ = _uniform_angle_fft(extract_robust_aperture(corrected_rd), settings=settings)
    view = np.sum(np.abs(rad[:, bins, settings.ti_useful_range_slice]), axis=1).astype(np.float64)
    total = float(np.sum(view))
    if total <= 0:
        return {"valid": False, "reason": "dynamic_ra_has_zero_energy"}
    probability = view.ravel() / total
    nonzero = probability > 0
    entropy = -float(np.sum(probability[nonzero] * np.log(probability[nonzero]))) / math.log(
        probability.size
    )
    peak_index = np.unravel_index(np.argmax(view), view.shape)
    peak = float(view[peak_index])
    sidelobe = view.copy()
    a0, r0 = peak_index
    sidelobe[max(0, a0 - 3) : a0 + 4, max(0, r0 - 1) : r0 + 2] = 0.0
    maximum_sidelobe = float(np.max(sidelobe, initial=0.0))
    ratio = peak / maximum_sidelobe if maximum_sidelobe > 0 else 1.0
    return {
        "valid": True,
        "normalized_entropy": entropy,
        "peak_to_maximum_sidelobe_ratio": ratio,
        "peak_to_maximum_sidelobe_db": 20.0 * math.log10(ratio),
    }


def diagnose_variant_pair(
    adc: np.ndarray,
    range_mat: np.ndarray,
    *,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
    common_support_cells: int = 512,
) -> dict[str, Any]:
    """Evaluate both calibration arms on identical cells, weights, and bins."""

    profile = CalibrationProfile.from_value(calibration_profile)
    corrected = {
        variant.value: _processed_corrected_rd(
            adc, range_mat, variant, profile, waveform, settings
        )
        for variant in CalibrationVariant
    }
    support = _pooled_common_support(
        corrected, settings=settings, support_cells=common_support_cells
    )
    common_selection = _common_doppler_selection(corrected)
    common_bins = np.asarray(common_selection["indices"], dtype=np.int16)
    rows: dict[str, Any] = {}
    for name, cube in corrected.items():
        native_selection = ti_dynamic_doppler_selection(extract_robust_aperture(cube))
        native_bins = np.asarray(native_selection["selected_indices_zero_based"], dtype=np.int16)
        rows[name] = {
            "native_selected_doppler_indices_zero_based": [int(item) for item in native_bins],
            "native_selected_doppler_bin_count": int(native_bins.size),
            "duplicate_virtual_position_complex_coherence_common_support": (
                _duplicate_coherence_on_common_support(cube, support)
            ),
            "disjoint_subarray_native_bins": _subarray_on_bins(
                cube, native_bins, settings=settings
            ),
            "disjoint_subarray_common_bins": _subarray_on_bins(
                cube, common_bins, settings=settings
            ),
            "focus_native_bins": _focus_on_bins(cube, native_bins, settings=settings),
            "focus_common_bins": _focus_on_bins(cube, common_bins, settings=settings),
        }
    return {
        "schema": "rad_emo_ti_calibration_paired_physics_diagnostic_frame_v1",
        "labels_or_boxes_opened": False,
        "frequency_scale_q": profile.frequency_scale_q(waveform),
        "common_rd_support": support["receipt"],
        "common_doppler_selection": common_selection["receipt"],
        "variants": rows,
        "decision_rule_frozen_before_real_data": DECISION_RULE,
    }


def _metric_value(row: Mapping[str, Any], variant: str, metric: str) -> float | None:
    entry = row["variants"][variant]
    paths = {
        "duplicate_complex_coherence": (
            "duplicate_virtual_position_complex_coherence_common_support",
            "mean_group_coherence",
        ),
        "disjoint_subarray_map_cosine": (
            "disjoint_subarray_common_bins",
            "normalized_map_cosine",
        ),
        "disjoint_peak_azimuth_difference_deg": (
            "disjoint_subarray_common_bins",
            "absolute_peak_azimuth_difference_deg",
        ),
        "disjoint_peak_range_difference_m": (
            "disjoint_subarray_common_bins",
            "absolute_peak_range_difference_m",
        ),
        "focus_normalized_entropy": ("focus_common_bins", "normalized_entropy"),
        "focus_peak_to_maximum_sidelobe_db": (
            "focus_common_bins",
            "peak_to_maximum_sidelobe_db",
        ),
    }
    current: Any = entry
    for key in paths[metric]:
        current = current.get(key) if isinstance(current, Mapping) else None
    if isinstance(current, (int, float)) and not isinstance(current, bool) and math.isfinite(float(current)):
        return float(current)
    return None


def aggregate_paired_diagnostics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the frozen dominance rule to paired per-frame metric medians."""

    if not rows:
        raise ValueError("at least one paired diagnostic row is required")
    variants = (CalibrationVariant.AS_SAVED.value, CalibrationVariant.SAMPLE_COUNT_RESCALED.value)
    aggregates: dict[str, Any] = {name: {} for name in variants}
    oriented: dict[str, Any] = {}
    improved = degraded = tied = missing = 0
    for metric, rule in DECISION_RULE["metrics"].items():
        medians: dict[str, float | None] = {}
        for variant in variants:
            observed = [
                value
                for row in rows
                if (value := _metric_value(row, variant, metric)) is not None
            ]
            median = float(np.median(observed)) if observed else None
            medians[variant] = median
            aggregates[variant][metric] = {
                "valid_frame_count": len(observed),
                "median": median,
                "mean": float(np.mean(observed)) if observed else None,
            }
        left = medians[CalibrationVariant.AS_SAVED.value]
        right = medians[CalibrationVariant.SAMPLE_COUNT_RESCALED.value]
        if left is None or right is None:
            missing += 1
            oriented[metric] = {"classification": "missing"}
            continue
        raw_delta = right - left
        direction_multiplier = 1.0 if rule["direction"] == "higher" else -1.0
        oriented_delta = direction_multiplier * raw_delta
        tolerance = float(rule["absolute_tolerance"])
        if oriented_delta > tolerance:
            classification = "rescaled_improved"
            improved += 1
        elif oriented_delta < -tolerance:
            classification = "as_saved_improved"
            degraded += 1
        else:
            classification = "within_tolerance"
            tied += 1
        oriented[metric] = {
            "raw_rescaled_minus_as_saved": raw_delta,
            "oriented_positive_means_rescaled_better": oriented_delta,
            "absolute_tolerance": tolerance,
            "classification": classification,
        }
    minimum = int(DECISION_RULE["minimum_directional_metrics_improved"])
    maximum_bad = int(DECISION_RULE["maximum_directional_metrics_degraded"])
    if improved >= minimum and degraded <= maximum_bad:
        decision = CalibrationVariant.SAMPLE_COUNT_RESCALED.value
    elif degraded >= minimum and improved <= maximum_bad:
        decision = CalibrationVariant.AS_SAVED.value
    else:
        decision = "inconclusive"
    return {
        "schema": "rad_emo_ti_calibration_paired_physics_diagnostic_aggregate_v1",
        "frame_count": len(rows),
        "labels_or_boxes_opened": False,
        "variant_metrics": aggregates,
        "paired_directional_deltas": oriented,
        "vote_counts": {
            "rescaled_improved": improved,
            "as_saved_improved": degraded,
            "within_tolerance": tied,
            "missing": missing,
        },
        "decision": decision,
        "decision_rule": DECISION_RULE,
    }
