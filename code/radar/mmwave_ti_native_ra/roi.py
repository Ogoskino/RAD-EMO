"""Metric radar-region extraction after camera--radar transform fitting."""

from __future__ import annotations

from typing import Any

import numpy as np

from .contracts import FrontendError


def _strict_axis(value: np.ndarray, name: str) -> np.ndarray:
    axis = np.asarray(value, dtype=np.float64)
    if axis.ndim != 1 or axis.size < 2 or not np.isfinite(axis).all():
        raise FrontendError(f"{name} must be a finite one-dimensional axis")
    if np.any(np.diff(axis) <= 0):
        raise FrontendError(f"{name} must be strictly increasing")
    return axis


def _bounds(value: tuple[float, float], name: str) -> tuple[float, float]:
    lower, upper = (float(item) for item in value)
    if not np.isfinite([lower, upper]).all() or upper <= lower:
        raise FrontendError(f"{name} must be finite and increasing")
    return lower, upper


def _interp_axis(values: np.ndarray, source: np.ndarray, target: np.ndarray, axis: int) -> np.ndarray:
    moved = np.moveaxis(values, axis, -1)
    flat = moved.reshape(-1, moved.shape[-1])
    output = np.empty((flat.shape[0], target.size), dtype=np.float64)
    for index, row in enumerate(flat):
        output[index] = np.interp(target, source, row, left=0.0, right=0.0)
    return np.moveaxis(output.reshape(*moved.shape[:-1], target.size), -1, axis)


def extract_metric_roi(
    rad_power: np.ndarray,
    azimuth_axis_deg: np.ndarray,
    range_axis_m: np.ndarray,
    *,
    azimuth_bounds_deg: tuple[float, float],
    range_bounds_m: tuple[float, float],
    output_azimuth_bins: int = 64,
    output_range_bins: int = 32,
) -> dict[str, Any]:
    """Resample transform-produced metric bounds to a fixed ``[A,D,R]`` crop."""

    power = np.asarray(rad_power)
    azimuth = _strict_axis(azimuth_axis_deg, "azimuth_axis_deg")
    ranges = _strict_axis(range_axis_m, "range_axis_m")
    if power.ndim != 3 or power.shape != (azimuth.size, 32, ranges.size):
        raise FrontendError(f"rad_power must be [azimuth,32,range], got {power.shape}")
    if not np.isfinite(power).all() or np.any(power < 0):
        raise FrontendError("rad_power must be finite and non-negative")
    if output_azimuth_bins <= 1 or output_range_bins <= 1:
        raise FrontendError("ROI output dimensions must exceed one")
    az_lower, az_upper = _bounds(azimuth_bounds_deg, "azimuth_bounds_deg")
    r_lower, r_upper = _bounds(range_bounds_m, "range_bounds_m")
    target_azimuth = np.linspace(az_lower, az_upper, output_azimuth_bins)
    target_range = np.linspace(r_lower, r_upper, output_range_bins)
    ranged = _interp_axis(power.astype(np.float64), ranges, target_range, axis=2)
    crop = _interp_axis(ranged, azimuth, target_azimuth, axis=0).astype(np.float32)
    center = power.shape[1] // 2
    dynamic_mask = np.ones(power.shape[1], dtype=np.bool_)
    dynamic_mask[max(0, center - 1) : min(power.shape[1], center + 2)] = False
    channels = np.stack(
        [
            np.log1p(crop.sum(axis=1)),
            np.log1p(crop[:, center, :]),
            np.log1p(crop[:, dynamic_mask, :].sum(axis=1)),
        ],
        axis=0,
    ).astype(np.float32)
    return {
        "rad_power": crop,
        "log_power_channels": channels,
        "azimuth_axis_deg": target_azimuth,
        "range_axis_m": target_range,
        "axes": {
            "rad_power": ["azimuth", "doppler", "range"],
            "log_power_channels": ["channel", "azimuth", "range"],
        },
        "channel_names": ["total", "zero_doppler", "nonzero_doppler_excluding_center_three"],
    }
