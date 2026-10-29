"""Strict read-only loaders for retained ADC and same-board calibration."""

from __future__ import annotations

import hashlib
import io
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import ADC_SHAPE, CalibrationProfile, FrontendError


@dataclass(frozen=True)
class ExactFileSnapshot:
    """Immutable bytes whose digest and parser input are necessarily identical."""

    path: str
    payload: bytes = field(repr=False)
    bytes: int
    sha256: str
    stat_identity: tuple[int, int, int, int] = field(repr=False)

    def receipt(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "stable_open_handle_and_path_identity_verified": True,
            "hash_computed_from_exact_parser_snapshot_bytes": True,
            "symlink": False,
            "all_existing_parent_components_symlink_and_reparse_free": True,
        }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _reject_reparse_or_symlink_components(path: Path) -> Path:
    """Return an absolute lexical path only when no existing component redirects."""
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FrontendError(f"cannot inspect snapshot path component: {current}: {exc}") from exc
        is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & 0x400)
        if stat.S_ISLNK(metadata.st_mode) or is_reparse:
            raise FrontendError(
                f"snapshot path cannot traverse a symlink or reparse point: {current}"
            )
    return absolute


def read_exact_file_snapshot(path: str | Path) -> ExactFileSnapshot:
    """Read once and reject symlink, replacement, truncation, or mid-read writes."""
    unresolved = Path(path).expanduser()
    lexical = _reject_reparse_or_symlink_components(unresolved)
    source = lexical.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise FrontendError(f"snapshot source must be a regular file: {source}")
    path_before = source.stat()
    with source.open("rb") as handle:
        handle_before = os.fstat(handle.fileno())
        if not stat.S_ISREG(handle_before.st_mode):
            raise FrontendError(f"snapshot open handle is not a regular file: {source}")
        payload = handle.read()
        handle_after = os.fstat(handle.fileno())
    path_after = source.stat()
    identities = {
        _stat_identity(path_before),
        _stat_identity(handle_before),
        _stat_identity(handle_after),
        _stat_identity(path_after),
    }
    if len(identities) != 1 or len(payload) != int(path_after.st_size):
        raise FrontendError(f"snapshot source changed during exact-byte read: {source}")
    digest = hashlib.sha256(payload).hexdigest().upper()
    return ExactFileSnapshot(
        path=str(source),
        payload=payload,
        bytes=len(payload),
        sha256=digest,
        stat_identity=_stat_identity(path_after),
    )


def revalidate_exact_file_snapshot(snapshot: ExactFileSnapshot) -> dict[str, Any]:
    """Fail closed if a pathname no longer names the exact snapshotted bytes."""
    current = read_exact_file_snapshot(snapshot.path)
    if current.bytes != snapshot.bytes or current.sha256 != snapshot.sha256:
        raise FrontendError(f"snapshot path binding changed after read: {snapshot.path}")
    return {
        **snapshot.receipt(),
        "post_parse_live_path_binding_match": True,
    }


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_source(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise FileNotFoundError(source)
    return source


def load_adc_frame_from_snapshot(
    snapshot: ExactFileSnapshot,
    *,
    dataset_key: str = "adcData",
    revalidate_path: bool = True,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Parse HDF5 from the exact immutable bytes hashed in the receipt."""

    try:
        import h5py
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("h5py is required to load retained ADC MAT files") from exc
    if not isinstance(snapshot, ExactFileSnapshot):
        raise TypeError("snapshot must be ExactFileSnapshot")
    try:
        with h5py.File(io.BytesIO(snapshot.payload), "r") as handle:
            if dataset_key not in handle:
                raise FrontendError(
                    f"missing HDF5 dataset {dataset_key!r}: {snapshot.path}"
                )
            dataset = handle[dataset_key]
            stored_shape = tuple(int(v) for v in dataset.shape)
            stored_dtype = str(dataset.dtype)
            value = np.asarray(dataset)
    except OSError as exc:
        raise FrontendError(f"cannot parse exact ADC snapshot: {snapshot.path}: {exc}") from exc
    if value.dtype.names is not None:
        if tuple(value.dtype.names) != ("real", "imag"):
            raise FrontendError(f"unexpected compound ADC fields: {value.dtype.names}")
        value = value["real"] + 1j * value["imag"]
    elif not np.iscomplexobj(value):
        raise FrontendError(f"ADC dataset is not complex: {stored_dtype}")
    if value.shape != ADC_SHAPE:
        raise FrontendError(f"ADC shape differs: {value.shape} != {ADC_SHAPE}")
    if not np.isfinite(value.real).all() or not np.isfinite(value.imag).all():
        raise FrontendError("ADC contains NaN or infinity")
    output = np.ascontiguousarray(value, dtype=np.complex64)
    live_binding = (
        revalidate_exact_file_snapshot(snapshot)
        if revalidate_path
        else {**snapshot.receipt(), "post_parse_live_path_binding_match": None}
    )
    return output, {
        **live_binding,
        "dataset_key": dataset_key,
        "stored_shape": list(stored_shape),
        "stored_dtype": stored_dtype,
        "output_shape": list(output.shape),
        "output_axes": ["tx_storage_slot", "raw_rx_storage_slot", "slow_time_loop", "fast_time_sample"],
        "upstream_state": "already_frequency_and_phase_calibrated_by_calibrationCascade_datapath",
        "hdf5_parser_consumed_exact_snapshot_bytes": True,
    }


def load_adc_frame(
    path: str | Path,
    *,
    dataset_key: str = "adcData",
) -> tuple[np.ndarray, dict[str, Any]]:
    """Snapshot, hash, parse and post-parse revalidate one retained ADC MAT."""
    return load_adc_frame_from_snapshot(
        read_exact_file_snapshot(path), dataset_key=dataset_key, revalidate_path=True
    )


def load_calibration_mat_from_snapshot(
    snapshot: ExactFileSnapshot,
    *,
    interpolation_factor: int = 5,
    revalidate_path: bool = True,
) -> tuple[np.ndarray, CalibrationProfile, dict[str, Any]]:
    """Parse MAT calibration authorities from the exact bytes already hashed."""

    try:
        import scipy.io
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("scipy is required to read the TI calibration MAT") from exc
    if not isinstance(snapshot, ExactFileSnapshot):
        raise TypeError("snapshot must be ExactFileSnapshot")
    try:
        payload = scipy.io.loadmat(io.BytesIO(snapshot.payload), simplify_cells=True)
        result = payload["calibResult"]
        params = payload["params"]
        value = np.asarray(result["RangeMat"])
        rx_fft_shape = tuple(int(item) for item in np.asarray(result["Rx_fft"]).shape)
        sampling_rate_hz = float(params["Sampling_Rate_sps"])
        slope_hz_per_s = float(params["Slope_MHzperus"]) * 1e12
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise FrontendError(
            f"cannot read calibration authorities from exact snapshot {snapshot.path}: {exc}"
        ) from exc
    if value.shape != (12, 16) or value.dtype.kind not in "iuf" or not np.isfinite(value).all():
        raise FrontendError(f"RangeMat differs: dtype={value.dtype}, shape={value.shape}")
    if len(rx_fft_shape) != 3 or rx_fft_shape[1:] != (16, 12):
        raise FrontendError(f"calibration Rx_fft shape differs: {rx_fft_shape}")
    if rx_fft_shape[0] % interpolation_factor:
        raise FrontendError("calibration Rx_fft length is not divisible by interpolation factor")
    basis_samples = rx_fft_shape[0] // interpolation_factor
    profile = CalibrationProfile(
        sampling_rate_hz=sampling_rate_hz,
        slope_hz_per_s=slope_hz_per_s,
        basis_samples=basis_samples,
        interpolation_factor=interpolation_factor,
    )
    matrix = np.ascontiguousarray(value, dtype=np.float64)
    live_binding = (
        revalidate_exact_file_snapshot(snapshot)
        if revalidate_path
        else {**snapshot.receipt(), "post_parse_live_path_binding_match": None}
    )
    return matrix, profile, {
        **live_binding,
        "range_mat_field": "calibResult.RangeMat",
        "range_mat_shape": [12, 16],
        "range_mat_indexing": "row=physical_TX_global_ID_minus_1,column=raw_RX_ID_minus_1",
        "reference_convention": "physical_TX12_raw_RX1",
        "reference_range_mat_value": float(matrix[11, 0]),
        "embedded_sampling_rate_field": "params.Sampling_Rate_sps",
        "embedded_slope_field": "params.Slope_MHzperus_converted_by_1e12_to_Hz_per_s",
        "rx_fft_shape": list(rx_fft_shape),
        "basis_samples_inferred_as_rx_fft_length_divided_by_interpolation": basis_samples,
        "profile": profile.to_dict(),
        "scipy_mat_parser_consumed_exact_snapshot_bytes": True,
    }


def load_calibration_mat(
    path: str | Path,
    *,
    interpolation_factor: int = 5,
) -> tuple[np.ndarray, CalibrationProfile, dict[str, Any]]:
    """Snapshot once, hash, parse and post-parse revalidate the calibration MAT."""
    return load_calibration_mat_from_snapshot(
        read_exact_file_snapshot(path),
        interpolation_factor=interpolation_factor,
        revalidate_path=True,
    )


def load_range_mat(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Compatibility loader; metadata includes the required profile mapping."""

    matrix, profile, info = load_calibration_mat(path)
    result = dict(info)
    result["profile"] = profile.to_dict()
    return matrix, result
