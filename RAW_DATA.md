# Images and recording-level sensor data

The main dataset target contains images and low-level sensor measurements,
following the recording-level structure documented in
[Gao et al.'s author repository](https://github.com/Xiangyu-Gao/Raw_2D_MIMO_radar_dataset_for_carry_object_detection).
The existing feature and prediction package is a companion to that resource.

| Material | Inventoried recording coverage | Logical size |
| --- | --- | --- |
| Original RGB images | 114 recordings | 101.11 GB |
| Thermal images | 114 recordings | 26.69 GB |
| Native radar ADC binaries and indexes | 90 recordings, 12 participants | 60.91 GB |
| Pulse/camera acquisition tables | 114 recordings | 5.19 MB |
| Person annotation exports | 114 recordings | 33.66 MB |
| Optional calibrated ADC MAT exports | 76 recordings, 10 participants | 183.29 GB |

The RGB inventory combines the configured `rgb+mmw_exp2` source with the original
P1S1-P1S3 images retained under `realsense_data`, the same source used for those
recordings in the experiments. The combined inventory contains 189,835 images.
Each recording/frame is selected once across the two locations.

RGB images have a resolution of 640 × 480 pixels and retain their acquisition
timestamps. The capture code requested 60 fps; the saved sequences have variable
cadence, with per-recording whole-span rates of about 29.82–36.09 images per
second (median 33.50). Frame timestamps provide the sampling times for processing.

The base directory inventory dates from 4 October, with the additional RGB
entries enumerated on 29 October. Native radar bytes were
separately read and hashed on 29 October; other sizes include cloud-recall files
and do not establish current payload readability. Missing inventory coverage is
not proof that an acquisition was lost. The available modalities are not complete
for every participant/recording.

Native binaries retain the original capture. MAT exports contain complex ADC-level
samples, generally following upstream frequency/phase calibration, before FFT.
Their logical axes are sample, loop, receiver, transmitter (`80 × 25 × 16 × 12`).
They are an optional measurement-level variant, not an independent sensor cohort.
Features, cubes and model scores are later analysis products.

The release needs recording-level image sequences, ADC readers/configuration,
person boxes, protocol and affect labels, relative timing with reconstruction
uncertainty, missing-file/support tables and fixed paper splits. Person boxes are
not concealed-object boxes. Sensor pairing must retain its estimated timing
provenance; hardware synchronisation is not claimed.

No raw sensor package is uploaded or publicly downloadable. The raw inventory and
proposed transfer units have been prepared; selected files still need content
checks, packaging and the documented image/external sharing assessment. The DMP's
conditions govern access to originals and any anonymised image versions. Blurred
images must be labelled as transformed versions because they may change face or
waveform analysis inputs.

The [aggregate inventory](metadata/raw_scope_summary.json) describes the current
planning evidence. Per-recording crosswalks remain restricted preparation files.
