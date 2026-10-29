"""Ordered TI range/Doppler and robust 86-element azimuth processing."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .contracts import (
    ADC_SHAPE,
    FRAME_SCHEMA,
    LEGACY_FRAME_SCHEMA,
    RAD_POWER_SHAPE,
    RD_SLOT_SHAPE,
    ROBUST_APERTURE_SIZE,
    RX_AZIMUTH_HALF_LAMBDA_BY_RAW_ID,
    RX_ELEVATION_HALF_LAMBDA_BY_RAW_ID,
    RX_RAW_ID_BY_PROCESSING_SLOT,
    SPEED_OF_LIGHT_MPS,
    TI_USEFUL_RAD_POWER_SHAPE,
    TX_AZIMUTH_HALF_LAMBDA_BY_GLOBAL_ID,
    TX_ELEVATION_HALF_LAMBDA_BY_GLOBAL_ID,
    TX_GLOBAL_ID_BY_STORAGE_SLOT,
    CalibrationProfile,
    CalibrationVariant,
    FrontendError,
    FrontendSettings,
    Waveform,
)


def _validated_adc(adc: np.ndarray) -> np.ndarray:
    value = np.asarray(adc)
    if value.shape != ADC_SHAPE or not np.iscomplexobj(value):
        raise FrontendError(f"ADC must be complex with shape {ADC_SHAPE}, got {value.shape}")
    if not np.isfinite(value.real).all() or not np.isfinite(value.imag).all():
        raise FrontendError("ADC contains NaN or infinity")
    return value.astype(np.complex64, copy=False)


def calibration_ratio_phasor(
    range_mat: np.ndarray,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    *,
    waveform: Waveform = Waveform(),
) -> np.ndarray:
    """Return desired/saved frequency-calibration ratio ``[TX,RX,N]``.

    TI builds ``exp(+j*n*f)`` and applies MATLAB ``'``; conjugation makes the
    applied factor negative-sign.  The desired/saved ratio is consequently
    positive-sign.  ``q`` is computed from the MAT's embedded Fs/slope rather
    than silently assumed to be one.
    """

    matrix = np.asarray(range_mat, dtype=np.float64)
    if matrix.shape != (12, 16) or not np.isfinite(matrix).all():
        raise FrontendError("RangeMat must be finite [12,16]")
    profile = CalibrationProfile.from_value(calibration_profile)
    tx_ids = np.asarray(TX_GLOBAL_ID_BY_STORAGE_SLOT, dtype=np.int64)
    reference_value = matrix[profile.reference_tx_global_id - 1, profile.reference_rx_raw_id - 1]
    # RangeMat rows are physical TX identities; columns are raw RX identities.
    delta_k = matrix[tx_ids - 1, :] - reference_value
    q = profile.frequency_scale_q(waveform)
    coefficient = q * (
        1.0 / (waveform.samples * profile.interpolation_factor)
        - 1.0 / (profile.basis_samples * profile.interpolation_factor)
    )
    sample = np.arange(waveform.samples, dtype=np.float64)
    phase = 2.0 * np.pi * delta_k[:, :, None] * sample[None, None, :] * coefficient
    return np.exp(1j * phase).astype(np.complex64)


def apply_sample_count_calibration_variant(
    adc: np.ndarray,
    range_mat: np.ndarray,
    variant: CalibrationVariant | str,
    *,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    waveform: Waveform = Waveform(),
) -> np.ndarray:
    """Apply exactly one preregistered interpretation to calibrated ADC."""

    value = _validated_adc(adc)
    profile = CalibrationProfile.from_value(calibration_profile)
    try:
        selected = CalibrationVariant(variant)
    except ValueError as exc:
        raise FrontendError(f"unknown calibration variant: {variant!r}") from exc
    if selected is CalibrationVariant.AS_SAVED:
        return np.ascontiguousarray(value.copy())
    phasor = calibration_ratio_phasor(range_mat, profile, waveform=waveform)
    return np.ascontiguousarray(value * phasor[:, :, None, :], dtype=np.complex64)


def reorder_rx_for_mimo(adc: np.ndarray) -> np.ndarray:
    """Apply TI's downstream raw-RX reorder after ``get_adcData``."""

    value = _validated_adc(adc)
    order = np.asarray(RX_RAW_ID_BY_PROCESSING_SLOT, dtype=np.int64) - 1
    return np.ascontiguousarray(value[:, order, :, :])


def ti_range_window(waveform: Waveform = Waveform()) -> np.ndarray:
    """Reproduce ``hanning(80)`` plus TI's first-half mirror construction."""

    original = np.hanning(waveform.samples).astype(np.float64)
    half = waveform.samples // 2
    result = np.ones(waveform.samples, dtype=np.float64)
    result[:half] = original[:half]
    result[-half:] = result[:half][::-1]
    return result


def range_doppler_slots(
    adc_rx_ordered: np.ndarray,
    *,
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> np.ndarray:
    """Return unscaled complex ``[range=128,doppler=32,TX,RX]``.

    Active TI settings are symmetric Hanning in range, rectangular in
    Doppler, per-chirp range DC subtraction, no clutter removal, and no FFT
    output scaling.
    """

    value = _validated_adc(adc_rx_ordered).astype(np.complex128, copy=True)
    if settings.fast_time_dc_removal:
        value -= value.mean(axis=-1, keepdims=True)
    ranged = np.fft.fft(
        value * ti_range_window(waveform).reshape(1, 1, 1, waveform.samples),
        n=settings.range_fft_size,
        axis=-1,
    )
    if settings.clutter_remove:  # contract rejects this; branch documents order
        ranged -= ranged.mean(axis=2, keepdims=True)
    doppler = np.fft.fftshift(
        np.fft.fft(ranged, n=settings.doppler_fft_size, axis=2), axes=2
    )
    output = doppler.transpose(3, 2, 0, 1)
    if output.shape != RD_SLOT_SHAPE:
        raise RuntimeError(f"internal RD shape differs: {output.shape}")
    return np.ascontiguousarray(output, dtype=np.complex64)


def _validate_rd_slots(rd_slots: np.ndarray) -> np.ndarray:
    value = np.asarray(rd_slots)
    if (
        value.ndim != 4
        or value.shape[1:] != RD_SLOT_SHAPE[1:]
        or value.shape[0] <= 0
        or not np.iscomplexobj(value)
    ):
        raise FrontendError("RD slots must be complex [range,32,12,16]")
    if not np.isfinite(value.real).all() or not np.isfinite(value.imag).all():
        raise FrontendError("RD slots contain NaN or infinity")
    return value


def tdm_correct_rd(
    rd_slots: np.ndarray,
    *,
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> np.ndarray:
    """Apply TI's per-Doppler-bin emission-slot phase correction."""

    value = _validate_rd_slots(rd_slots)
    shifted_bin = np.arange(settings.doppler_fft_size, dtype=np.float64) - (
        settings.doppler_fft_size / 2.0
    )
    delta_phi = 2.0 * np.pi * shifted_bin / (
        waveform.tx_slots * settings.doppler_fft_size
    )
    tx_slot = np.arange(waveform.tx_slots, dtype=np.float64)
    correction = np.exp(-1j * delta_phi[:, None] * tx_slot[None, :])
    return np.ascontiguousarray(value * correction[None, :, :, None], dtype=np.complex64)


def virtual_geometry() -> dict[str, np.ndarray]:
    """Return all 192 virtual coordinates in TX-major/RX-processing order."""

    tx_ids = np.asarray(TX_GLOBAL_ID_BY_STORAGE_SLOT, dtype=np.int16)
    rx_ids = np.asarray(RX_RAW_ID_BY_PROCESSING_SLOT, dtype=np.int16)
    tx_az_table = np.asarray(TX_AZIMUTH_HALF_LAMBDA_BY_GLOBAL_ID, dtype=np.int16)
    tx_el_table = np.asarray(TX_ELEVATION_HALF_LAMBDA_BY_GLOBAL_ID, dtype=np.int16)
    rx_az_table = np.asarray(RX_AZIMUTH_HALF_LAMBDA_BY_RAW_ID, dtype=np.int16)
    rx_el_table = np.asarray(RX_ELEVATION_HALF_LAMBDA_BY_RAW_ID, dtype=np.int16)
    tx_az = tx_az_table[tx_ids - 1]
    tx_el = tx_el_table[tx_ids - 1]
    rx_az = rx_az_table[rx_ids - 1]
    rx_el = rx_el_table[rx_ids - 1]
    return {
        "azimuth_half_lambda": (tx_az[:, None] + rx_az[None, :]).reshape(-1),
        "elevation_half_lambda": (tx_el[:, None] + rx_el[None, :]).reshape(-1),
        "tx_storage_index": np.repeat(np.arange(12, dtype=np.int16), 16),
        "rx_processing_index": np.tile(np.arange(16, dtype=np.int16), 12),
        "tx_global_id": np.repeat(tx_ids, 16),
        "rx_raw_id": np.tile(rx_ids, 12),
    }


def robust_azimuth_aperture() -> dict[str, np.ndarray]:
    """Return TI-first unique zero-elevation channels at x=0..85."""

    geometry = virtual_geometry()
    zero = np.flatnonzero(geometry["elevation_half_lambda"] == 0)
    first_by_position: dict[int, int] = {}
    for flat_index in zero:
        position = int(geometry["azimuth_half_lambda"][flat_index])
        first_by_position.setdefault(position, int(flat_index))
    positions = np.asarray(sorted(first_by_position), dtype=np.int16)
    flat = np.asarray([first_by_position[int(item)] for item in positions], dtype=np.int16)
    if positions.size != ROBUST_APERTURE_SIZE or not np.array_equal(
        positions, np.arange(ROBUST_APERTURE_SIZE, dtype=np.int16)
    ):
        raise RuntimeError("TI robust aperture is not the expected contiguous 0..85 array")
    return {
        "position_half_lambda": positions,
        "virtual_flat_index": flat,
        "tx_storage_index": geometry["tx_storage_index"][flat],
        "rx_processing_index": geometry["rx_processing_index"][flat],
        "tx_global_id": geometry["tx_global_id"][flat],
        "rx_raw_id": geometry["rx_raw_id"][flat],
    }


def extract_robust_aperture(rd_slots: np.ndarray) -> np.ndarray:
    """Extract ordered complex ``[range,doppler,azimuth-element=86]``."""

    value = _validate_rd_slots(rd_slots)
    aperture = robust_azimuth_aperture()
    return np.ascontiguousarray(
        value[:, :, aperture["tx_storage_index"], aperture["rx_processing_index"]],
        dtype=np.complex64,
    )


def _uniform_angle_fft(
    aperture_rd: np.ndarray,
    *,
    settings: FrontendSettings,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    value = np.asarray(aperture_rd)
    if value.ndim != 3 or value.shape[1] != 32 or not np.iscomplexobj(value):
        raise FrontendError("aperture RD must be complex [range,32,element]")
    transformed = np.fft.fftshift(
        np.fft.fft(value, n=settings.angle_fft_size, axis=2), axes=2
    )
    shifted_bin = np.arange(settings.angle_fft_size, dtype=np.float64) - (
        settings.angle_fft_size / 2.0
    )
    sine_increasing = (-2.0 * shifted_bin / settings.angle_fft_size)[::-1]
    azimuth_deg = np.rad2deg(np.arcsin(sine_increasing))
    complex_rad = np.ascontiguousarray(
        transformed[:, :, ::-1].transpose(2, 1, 0), dtype=np.complex64
    )
    return complex_rad, sine_increasing, azimuth_deg


def ti_angle_fft(
    aperture_rd: np.ndarray,
    *,
    settings: FrontendSettings = FrontendSettings(),
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    value = np.asarray(aperture_rd)
    if value.shape[2:] != (ROBUST_APERTURE_SIZE,):
        raise FrontendError("TI primary aperture must contain 86 ordered elements")
    return _uniform_angle_fft(value, settings=settings)


def ti_dynamic_doppler_selection(aperture_rd: np.ndarray) -> dict[str, np.ndarray | bool | float]:
    """Reproduce TI's global mean-|RD|, >0.5*peak dynamic-bin rule."""

    value = np.asarray(aperture_rd)
    if value.ndim != 3 or value.shape[1] != 32 or not np.iscomplexobj(value):
        raise FrontendError("aperture RD must be complex [range,32,element]")
    proxy = np.sum(np.mean(np.abs(value), axis=2), axis=0, dtype=np.float64)
    candidates = np.concatenate((np.arange(0, 15), np.arange(18, 32))).astype(np.int16)
    peak = float(np.max(proxy[candidates], initial=0.0))
    if not np.isfinite(peak) or peak <= 0.0:
        selected = np.empty(0, dtype=np.int16)
        valid = False
    else:
        selected = candidates[proxy[candidates] > (0.5 * peak)]
        valid = bool(selected.size)
    mask = np.zeros(32, dtype=np.bool_)
    mask[selected] = True
    return {
        "proxy": proxy.astype(np.float64),
        "excluded_indices_zero_based": np.asarray([15, 16, 17], dtype=np.int16),
        "selected_indices_zero_based": selected,
        "selected_mask": mask,
        "peak": peak,
        "threshold": 0.5 * peak,
        "valid": valid,
    }


def range_axis_m(
    range_bins: int = 128,
    *,
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> np.ndarray:
    if range_bins <= 0 or range_bins > settings.range_fft_size:
        raise FrontendError("range_bins must be within the 128-bin FFT")
    spacing = SPEED_OF_LIGHT_MPS * waveform.sample_rate_hz / (
        2.0 * waveform.slope_hz_per_s * settings.range_fft_size
    )
    return np.arange(range_bins, dtype=np.float64) * spacing


def doppler_axes(
    *,
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
) -> tuple[np.ndarray, np.ndarray]:
    shifted_bin = np.arange(settings.doppler_fft_size, dtype=np.float64) - (
        settings.doppler_fft_size / 2.0
    )
    frequency = shifted_bin / (settings.doppler_fft_size * waveform.slow_time_period_s)
    return frequency, frequency * waveform.wavelength_m / 2.0


def _spatial_arrays(
    rd_slots: np.ndarray,
    *,
    waveform: Waveform,
    settings: FrontendSettings,
    include_complex: bool,
) -> dict[str, np.ndarray]:
    uncorrected_aperture = extract_robust_aperture(rd_slots)
    corrected_aperture = extract_robust_aperture(
        tdm_correct_rd(rd_slots, waveform=waveform, settings=settings)
    )
    corrected_rad, sine_axis, azimuth_axis = ti_angle_fft(
        corrected_aperture, settings=settings
    )
    # plot_range_azimuth_2D.m hardcodes DopplerCorrection=0. Retain its exact
    # angle map separately from the corrected classifier RAD.
    ti_plot_rad, _, _ = ti_angle_fft(uncorrected_aperture, settings=settings)
    selection = ti_dynamic_doppler_selection(uncorrected_aperture)
    selected = np.asarray(selection["selected_indices_zero_based"], dtype=np.int16)
    if selected.size:
        ti_dynamic = np.sum(np.abs(ti_plot_rad[:, selected, :]), axis=1)
        corrected_dynamic = np.sum(np.abs(corrected_rad[:, selected, :]), axis=1)
    else:
        shape = (settings.angle_fft_size, rd_slots.shape[0])
        ti_dynamic = np.zeros(shape, dtype=np.float32)
        corrected_dynamic = np.zeros(shape, dtype=np.float32)
    power = np.ascontiguousarray(np.abs(corrected_rad) ** 2, dtype=np.float32)
    ranges = range_axis_m(power.shape[2], waveform=waveform, settings=settings)
    doppler_hz, velocity_mps = doppler_axes(waveform=waveform, settings=settings)
    aperture = robust_azimuth_aperture()
    arrays: dict[str, np.ndarray] = {
        "rad_power": power,
        "ra_power": np.ascontiguousarray(power.sum(axis=1), dtype=np.float32),
        "rd_power": np.ascontiguousarray(
            np.mean(np.abs(corrected_aperture) ** 2, axis=2).T, dtype=np.float32
        ),
        "ti_dynamic_ra_magnitude": np.ascontiguousarray(ti_dynamic, dtype=np.float32),
        "tdm_corrected_dynamic_ra_magnitude": np.ascontiguousarray(
            corrected_dynamic, dtype=np.float32
        ),
        "ti_static_ra_magnitude": np.ascontiguousarray(np.abs(ti_plot_rad[:, 16, :]), dtype=np.float32),
        "tdm_corrected_static_ra_magnitude": np.ascontiguousarray(
            np.abs(corrected_rad[:, 16, :]), dtype=np.float32
        ),
        "dynamic_doppler_proxy": np.asarray(selection["proxy"], dtype=np.float64),
        "dynamic_selected_doppler_indices_zero_based": selected,
        "dynamic_selected_doppler_mask": np.asarray(selection["selected_mask"], dtype=np.bool_),
        "dynamic_excluded_doppler_indices_zero_based": np.asarray(
            selection["excluded_indices_zero_based"], dtype=np.int16
        ),
        "dynamic_valid": np.asarray(bool(selection["valid"]), dtype=np.bool_),
        "range_axis_m": ranges,
        "doppler_frequency_axis_hz": doppler_hz,
        "velocity_axis_mps": velocity_mps,
        "azimuth_sine_axis": sine_axis,
        "azimuth_axis_deg": azimuth_axis,
        "aperture_position_half_lambda": aperture["position_half_lambda"],
        "aperture_tx_storage_index": aperture["tx_storage_index"],
        "aperture_rx_processing_index": aperture["rx_processing_index"],
        "aperture_tx_global_id": aperture["tx_global_id"],
        "aperture_rx_raw_id": aperture["rx_raw_id"],
    }
    if include_complex:
        arrays["rad_complex"] = corrected_rad
    return arrays


def build_ti_frame(
    adc: np.ndarray,
    range_mat: np.ndarray,
    variant: CalibrationVariant | str,
    *,
    calibration_profile: CalibrationProfile | Mapping[str, Any],
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
    include_complex: bool = False,
) -> dict[str, Any]:
    """Build full transient physical RAD and exact TI useful-range views."""

    selected = CalibrationVariant(variant)
    profile = CalibrationProfile.from_value(calibration_profile)
    calibrated = apply_sample_count_calibration_variant(
        adc,
        range_mat,
        selected,
        calibration_profile=profile,
        waveform=waveform,
    )
    rd_slots = range_doppler_slots(
        reorder_rx_for_mimo(calibrated), waveform=waveform, settings=settings
    )
    arrays = _spatial_arrays(
        rd_slots, waveform=waveform, settings=settings, include_complex=include_complex
    )
    if arrays["rad_power"].shape != RAD_POWER_SHAPE:
        raise RuntimeError(f"full RAD shape differs: {arrays['rad_power'].shape}")
    useful = settings.ti_useful_range_slice
    for name in (
        "rad_power",
        "ra_power",
        "rd_power",
        "ti_dynamic_ra_magnitude",
        "tdm_corrected_dynamic_ra_magnitude",
        "ti_static_ra_magnitude",
        "tdm_corrected_static_ra_magnitude",
    ):
        arrays[f"{name}_ti_useful"] = np.ascontiguousarray(arrays[name][..., useful])
    arrays["range_axis_ti_useful_m"] = np.ascontiguousarray(
        arrays["range_axis_m"][useful]
    )
    if arrays["rad_power_ti_useful"].shape != TI_USEFUL_RAD_POWER_SHAPE:
        raise RuntimeError("TI useful RAD shape differs")
    return {
        "schema": FRAME_SCHEMA,
        "calibration_variant": selected.value,
        "calibration_profile": profile.to_dict(waveform),
        "arrays": arrays,
        "axes": {
            "rad_power": ["azimuth", "doppler", "range"],
            "rad_power_ti_useful": ["azimuth", "doppler", "range"],
            "ti_dynamic_ra_magnitude": ["azimuth", "range"],
            "ra_power": ["azimuth", "range"],
            "rd_power": ["doppler", "range"],
        },
        "waveform": waveform.to_dict(),
        "settings": settings.to_dict(),
        "range_bin_policy": {
            "full_fft_zero_based": [0, 127],
            "ti_display_useful_zero_based_inclusive": [5, 108],
            "first_64_is_ti_native": False,
        },
        "dynamic_ra_policy": {
            "excluded_shifted_doppler_indices_zero_based": [15, 16, 17],
            "selection": "global_sum_range_mean_antenna_abs_rd_strictly_greater_than_half_peak",
            "ti_plot_helper_doppler_correction": False,
            "corrected_companion_also_exposed": True,
            "empty_selection": "abstain_dynamic_valid_false_and_zero_fill",
        },
        "normalization": "none_unscaled_ti_ffts_no_per_frame_normalization",
        "persistence": "full_linear_rad_is_transient_reduce_immediately_do_not_materialize_full_dataset_cache",
        "rx_reorder_one_based": list(RX_RAW_ID_BY_PROCESSING_SLOT),
        "tx_global_id_by_storage_slot": list(TX_GLOBAL_ID_BY_STORAGE_SLOT),
    }


def build_ti_frame_from_legacy_rd_slots(
    rd_slots: np.ndarray,
    *,
    source_processing_contract: str,
    waveform: Waveform = Waveform(),
    settings: FrontendSettings = FrontendSettings(),
    include_complex: bool = False,
) -> dict[str, Any]:
    """Spatialize the existing first-64 RD cache as a comparator only."""

    if not isinstance(source_processing_contract, str) or not source_processing_contract.strip():
        raise FrontendError("source_processing_contract must be explicit")
    value = _validate_rd_slots(rd_slots)
    if value.shape != (64, 32, 12, 16):
        raise FrontendError("legacy cached RD must be [64,32,12,16]")
    arrays = _spatial_arrays(
        value, waveform=waveform, settings=settings, include_complex=include_complex
    )
    return {
        "schema": LEGACY_FRAME_SCHEMA,
        "calibration_variant": CalibrationVariant.AS_SAVED.value,
        "arrays": arrays,
        "input_stage": "precomputed_complex_rd_as_saved_only",
        "source_processing_contract": source_processing_contract,
        "sample_count_rescaling_available": False,
        "ti_native_primary": False,
        "why_not_primary": "upstream_windows_differ_and_fft_bins_64_to_127_are_absent",
        "first_64_is_ti_native": False,
    }


def product_summary(product: Mapping[str, Any]) -> dict[str, Any]:
    arrays = product["arrays"]
    power = np.asarray(arrays["rad_power"])
    result = {
        "schema": product["schema"],
        "calibration_variant": product["calibration_variant"],
        "rad_power_shape": list(power.shape),
        "rad_power_dtype": str(power.dtype),
        "ra_power_shape": list(np.asarray(arrays["ra_power"]).shape),
        "rd_power_shape": list(np.asarray(arrays["rd_power"]).shape),
        "range_min_m": float(arrays["range_axis_m"][0]),
        "range_max_m": float(arrays["range_axis_m"][-1]),
        "azimuth_min_deg": float(arrays["azimuth_axis_deg"][0]),
        "azimuth_max_deg": float(arrays["azimuth_axis_deg"][-1]),
        "velocity_min_mps": float(arrays["velocity_axis_mps"][0]),
        "velocity_max_mps": float(arrays["velocity_axis_mps"][-1]),
        "finite": bool(np.isfinite(power).all()),
        "power_sum": float(np.sum(power, dtype=np.float64)),
        "power_max": float(np.max(power)),
        "dynamic_valid": bool(np.asarray(arrays["dynamic_valid"]).item()),
        "dynamic_selected_doppler_indices_zero_based": [
            int(item) for item in arrays["dynamic_selected_doppler_indices_zero_based"]
        ],
    }
    if "rad_power_ti_useful" in arrays:
        result["ti_useful_rad_power_shape"] = list(arrays["rad_power_ti_useful"].shape)
        result["ti_useful_range_min_m"] = float(arrays["range_axis_ti_useful_m"][0])
        result["ti_useful_range_max_m"] = float(arrays["range_axis_ti_useful_m"][-1])
    return result
