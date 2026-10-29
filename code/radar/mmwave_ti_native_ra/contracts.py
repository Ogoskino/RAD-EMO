"""Frozen physical constants and contracts for the TI cascade frontend."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping


FRAME_SCHEMA = "rad_emo_ti_native_ra_frame_v1"
LEGACY_FRAME_SCHEMA = "rad_emo_ti_spatialized_legacy_first64_rd_frame_v1"
RECEIPT_SCHEMA = "rad_emo_ti_native_ra_real_sanity_receipt_v1"
SPEED_OF_LIGHT_MPS = 300_000_000.0  # module_param.m convention

# HDF5 exposes the reverse of MATLAB [sample,loop,raw-RX,TX-slot].
ADC_SHAPE = (12, 16, 25, 80)
RD_SLOT_SHAPE = (128, 32, 12, 16)
ROBUST_APERTURE_SIZE = 86
RAD_POWER_SHAPE = (256, 32, 128)

# cascade_MIMO_signalProcessing.m sets 5 and 20, while the plot helper uses
# 5:(128-20), then indexes data at +1. Python FFT bins are exactly 5..108.
TI_USEFUL_RANGE_START = 5
TI_USEFUL_RANGE_STOP = 109
TI_USEFUL_RANGE_BINS = TI_USEFUL_RANGE_STOP - TI_USEFUL_RANGE_START
TI_USEFUL_RAD_POWER_SHAPE = (256, 32, TI_USEFUL_RANGE_BINS)

TX_GLOBAL_ID_BY_STORAGE_SLOT = tuple(range(12, 0, -1))
RX_RAW_ID_BY_PROCESSING_SLOT = (
    13,
    14,
    15,
    16,
    1,
    2,
    3,
    4,
    9,
    10,
    11,
    12,
    5,
    6,
    7,
    8,
)

# New-format TDA coordinates in half wavelengths at the 76.8 GHz design
# frequency. Tables are indexed by physical/global identity, never storage.
TX_AZIMUTH_HALF_LAMBDA_BY_GLOBAL_ID = (11, 10, 9, 32, 28, 24, 20, 16, 12, 8, 4, 0)
TX_ELEVATION_HALF_LAMBDA_BY_GLOBAL_ID = (6, 4, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0)
RX_AZIMUTH_HALF_LAMBDA_BY_RAW_ID = (11, 12, 13, 14, 50, 51, 52, 53, 46, 47, 48, 49, 0, 1, 2, 3)
RX_ELEVATION_HALF_LAMBDA_BY_RAW_ID = (0,) * 16


class FrontendError(ValueError):
    """Raised when a physical or tensor contract is violated."""


class CalibrationVariant(str, Enum):
    AS_SAVED = "as_saved"
    SAMPLE_COUNT_RESCALED = "sample_count_rescaled_180_to_80"


@dataclass(frozen=True)
class Waveform:
    samples: int = 80
    loops: int = 25
    tx_slots: int = 12
    rx_slots: int = 16
    sample_rate_hz: float = 8_000_000.0
    start_frequency_hz: float = 77_000_000_000.0
    slope_hz_per_s: float = 78_986_000_000_000.0
    adc_start_s: float = 6e-6
    ramp_end_s: float = 40e-6
    idle_s: float = 5e-6

    def __post_init__(self) -> None:
        if (self.tx_slots, self.rx_slots, self.loops, self.samples) != ADC_SHAPE:
            raise FrontendError("this stage is capture-bound to ADC [12,16,25,80]")
        for name in ("sample_rate_hz", "start_frequency_hz", "slope_hz_per_s"):
            if float(getattr(self, name)) <= 0:
                raise FrontendError(f"{name} must be positive")
        if self.ramp_end_s <= self.adc_start_s or self.idle_s < 0:
            raise FrontendError("invalid chirp timing")

    @property
    def chirp_interval_s(self) -> float:
        return self.ramp_end_s + self.idle_s

    @property
    def slow_time_period_s(self) -> float:
        return self.chirp_interval_s * self.tx_slots

    @property
    def capture_time_s(self) -> float:
        return self.samples / self.sample_rate_hz

    @property
    def center_frequency_hz(self) -> float:
        return self.start_frequency_hz + self.slope_hz_per_s * (
            self.adc_start_s + self.capture_time_s / 2.0
        )

    @property
    def wavelength_m(self) -> float:
        return SPEED_OF_LIGHT_MPS / self.center_frequency_hz

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CalibrationProfile:
    """Frequency-calibration authority embedded in the same-board MAT."""

    sampling_rate_hz: float
    slope_hz_per_s: float
    basis_samples: int = 180
    interpolation_factor: int = 5
    reference_tx_global_id: int = 12
    reference_rx_raw_id: int = 1

    def __post_init__(self) -> None:
        if self.sampling_rate_hz <= 0 or self.slope_hz_per_s <= 0:
            raise FrontendError("calibration sampling rate and slope must be positive")
        if (self.basis_samples, self.interpolation_factor) != (180, 5):
            raise FrontendError("calibration basis must be 180 samples with interpolation factor 5")
        if (self.reference_tx_global_id, self.reference_rx_raw_id) != (12, 1):
            raise FrontendError("TI calibration reference must be physical TX12/raw RX1")

    def frequency_scale_q(self, waveform: Waveform) -> float:
        return (self.sampling_rate_hz / waveform.sample_rate_hz) * (
            waveform.slope_hz_per_s / self.slope_hz_per_s
        )

    def to_dict(self, waveform: Waveform | None = None) -> dict[str, Any]:
        result = asdict(self)
        if waveform is not None:
            q = self.frequency_scale_q(waveform)
            result["frequency_scale_q"] = q
            result["frequency_scale_q_is_unity_abs_tolerance_1e_12"] = abs(q - 1.0) <= 1e-12
        return result

    @classmethod
    def from_value(cls, value: "CalibrationProfile | Mapping[str, Any]") -> "CalibrationProfile":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise FrontendError("calibration_profile must be CalibrationProfile or mapping")
        allowed = set(cls.__dataclass_fields__)
        missing = {"sampling_rate_hz", "slope_hz_per_s"} - set(value)
        unexpected = set(value) - allowed
        if missing or unexpected:
            raise FrontendError(
                f"calibration profile keys differ; missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        return cls(**dict(value))


@dataclass(frozen=True)
class FrontendSettings:
    """Exact active TI FFT/window settings for ``test1_param.m``."""

    range_fft_size: int = 128
    doppler_fft_size: int = 32
    angle_fft_size: int = 256
    fast_time_dc_removal: bool = True
    clutter_remove: bool = False
    range_window: str = "hanning_symmetric"
    doppler_window: str = "rectangular"
    ti_useful_range_start: int = TI_USEFUL_RANGE_START
    ti_useful_range_stop: int = TI_USEFUL_RANGE_STOP

    def __post_init__(self) -> None:
        if (self.range_fft_size, self.doppler_fft_size, self.angle_fft_size) != (128, 32, 256):
            raise FrontendError("capture-bound FFT sizes must be range=128, Doppler=32, angle=256")
        if self.range_window != "hanning_symmetric":
            raise FrontendError("exact TI primary requires symmetric hanning(80) range window")
        if self.doppler_window != "rectangular" or self.clutter_remove:
            raise FrontendError("exact TI primary requires rectangular Doppler and no clutter removal")
        if (self.ti_useful_range_start, self.ti_useful_range_stop) != (
            TI_USEFUL_RANGE_START,
            TI_USEFUL_RANGE_STOP,
        ):
            raise FrontendError("TI display-useful range slice must be zero-based [5:109]")

    @property
    def ti_useful_range_slice(self) -> slice:
        return slice(self.ti_useful_range_start, self.ti_useful_range_stop)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["ti_useful_range_bins_zero_based_inclusive"] = [
            self.ti_useful_range_start,
            self.ti_useful_range_stop - 1,
        ]
        return result
