"""TI 4-chip cascade range--azimuth frontend for retained calibrated ADC."""

from .contracts import (
    ADC_SHAPE,
    FRAME_SCHEMA,
    LEGACY_FRAME_SCHEMA,
    RAD_POWER_SHAPE,
    TI_USEFUL_RAD_POWER_SHAPE,
    CalibrationProfile,
    CalibrationVariant,
    FrontendSettings,
    Waveform,
)
from .diagnostics import (
    DECISION_RULE,
    aggregate_paired_diagnostics,
    diagnose_variant_pair,
)
from .frontend import (
    apply_sample_count_calibration_variant,
    build_ti_frame,
    build_ti_frame_from_legacy_rd_slots,
    product_summary,
    robust_azimuth_aperture,
)
from .io import (
    ExactFileSnapshot,
    load_adc_frame,
    load_adc_frame_from_snapshot,
    load_calibration_mat,
    load_calibration_mat_from_snapshot,
    load_range_mat,
    read_exact_file_snapshot,
    revalidate_exact_file_snapshot,
)
from .roi import extract_metric_roi
from .streaming import build_compact_frame, build_paired_compact_frame

__all__ = [
    "ADC_SHAPE",
    "FRAME_SCHEMA",
    "LEGACY_FRAME_SCHEMA",
    "RAD_POWER_SHAPE",
    "TI_USEFUL_RAD_POWER_SHAPE",
    "CalibrationProfile",
    "CalibrationVariant",
    "FrontendSettings",
    "ExactFileSnapshot",
    "Waveform",
    "DECISION_RULE",
    "aggregate_paired_diagnostics",
    "apply_sample_count_calibration_variant",
    "build_compact_frame",
    "build_paired_compact_frame",
    "build_ti_frame",
    "build_ti_frame_from_legacy_rd_slots",
    "diagnose_variant_pair",
    "extract_metric_roi",
    "load_adc_frame",
    "load_adc_frame_from_snapshot",
    "load_calibration_mat",
    "load_calibration_mat_from_snapshot",
    "load_range_mat",
    "read_exact_file_snapshot",
    "revalidate_exact_file_snapshot",
    "product_summary",
    "robust_azimuth_aperture",
]
