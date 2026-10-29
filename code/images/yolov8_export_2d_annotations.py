"""Export YOLO person boxes with RAD-EMO recording-level carriage classes.

The output schema is {"classes": [...], "images": [{"image": filename,
"annotations": [{"class": label, "boundingBox": {"x", "y", "width", "height"}}]}]}.
Coordinates are pixels. Classes describe the recording protocol: PO (neither
target object), PWP (phone), PWK (knife), PWPK (phone and knife). Boxes locate the
detected person; they are not manually reviewed object-localisation annotations.

Examples (run from the repository root):
  python code/images/yolov8_export_2d_annotations.py --root ../rgb_data \
      --output-root ../person_annotations --sessions-csv ../metadata/sessions.csv
  python code/images/yolov8_export_2d_annotations.py --root ../rgb_data/P_2/P_2_S_1 \
      --output-root ../person_annotations --label PWP --include-empty

--label-map accepts CSV columns session,label. --sessions-csv accepts the
canonical source_session_id (or session_id) and condition_code columns. No
participant tables or images are bundled with this code. Use --model with the
intended local weight file to fix model provenance; the Ultralytics model-name
default may obtain weights if they are not already available locally.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

CLASSES = [
    {"name": "PO", "features": [], "color": "#2b10a8"},
    {"name": "PWP", "features": [], "color": "#160c24"},
    {"name": "PWPK", "features": [], "color": "#b58c69"},
    {"name": "PWK", "features": [], "color": "#cf7bba"},
]
VALID_LABELS = {item["name"] for item in CLASSES}
OUTPUT_NAME = "2D-on-2D_annotations_export.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SESSION = re.compile(r"P_?(\d+)_?S_?(\d+)\Z", re.IGNORECASE)


def session_name(value: str) -> str:
    match = SESSION.fullmatch(value.strip())
    if not match or min(int(number) for number in match.groups()) < 1:
        raise ValueError(f"Invalid recording identifier: {value!r}")
    person, recording = map(int, match.groups())
    return f"P_{person}_S_{recording}"


def load_label_map(csv_path: Optional[str], canonical: bool = False) -> Dict[str, str]:
    """Read explicit classes; reject conflicting duplicates or unknown labels."""
    if not csv_path:
        return {}
    mapping = {}
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        if canonical:
            session_column = "source_session_id" if "source_session_id" in fields else "session_id"
            label_column = "condition_code"
        else:
            session_column, label_column = "session", "label"
        if not {session_column, label_column}.issubset(fields):
            raise ValueError(f"Label CSV must contain columns: {session_column},{label_column}")
        for row in reader:
            key = session_name(row[session_column])
            label = row[label_column].strip().upper()
            if label not in VALID_LABELS:
                raise ValueError(f"Invalid class {label!r} for {key}; expected {sorted(VALID_LABELS)}")
            if key in mapping and mapping[key] != label:
                raise ValueError(f"Conflicting classes for recording {key}")
            mapping[key] = label
    return mapping


def find_session_dirs(root: Path) -> List[Path]:
    root = root.resolve()
    if (root / "color_frames").is_dir():
        return [root]
    return sorted({path.parent for path in root.rglob("color_frames") if path.is_dir()})


def get_label_for_session(session_dir: Path, global_label: Optional[str], label_map: Dict[str, str]) -> str:
    if global_label:
        label = global_label.upper()
        if label not in VALID_LABELS:
            raise ValueError(f"Invalid class {label!r}; expected {sorted(VALID_LABELS)}")
        return label
    key = session_name(session_dir.name)
    if key not in label_map:
        raise ValueError(f"No explicit protocol class for recording {key}")
    return label_map[key]


def select_person_box(result, min_conf: float, select: str) -> Optional[Tuple[float, float, float, float, float]]:
    """Original selector: COCO class 0, confidence filter, area/confidence ties."""
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return None
    candidates = []
    for box in boxes:
        cls_id = int(box.cls.item())
        conf = float(box.conf.item())
        if cls_id != 0 or conf < min_conf:
            continue
        x1, y1, x2, y2 = [float(value) for value in box.xyxy[0].tolist()]
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        candidates.append((x1, y1, x2, y2, conf, area))
    if not candidates:
        return None
    if select == "largest":
        chosen = max(candidates, key=lambda box: (box[5], box[4]))
    else:
        chosen = max(candidates, key=lambda box: (box[4], box[5]))
    return chosen[:5]


def clamp_box(x1: float, y1: float, x2: float, y2: float, img_w: int, img_h: int) -> Dict[str, float]:
    """Original conversion from xyxy to clipped pixel x/y/width/height."""
    x1 = max(0.0, min(float(img_w), x1))
    y1 = max(0.0, min(float(img_h), y1))
    x2 = max(0.0, min(float(img_w), x2))
    y2 = max(0.0, min(float(img_h), y2))
    return {"x": x1, "y": y1, "width": max(0.0, x2 - x1), "height": max(0.0, y2 - y1)}


def output_path_for_session(session_dir: Path, output_root: Path) -> Path:
    name = session_name(session_dir.name)
    person = name.split("_S_", 1)[0]
    target = Path(output_root).resolve() / person / name / OUTPUT_NAME
    if target.resolve().is_relative_to(session_dir.resolve()):
        raise ValueError("Output must be separate from the original recording directory")
    return target


def validate_roots(root: Path, output_root: Path) -> None:
    source, output = root.resolve(), output_root.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use separate, non-overlapping input and output roots")


def export_session(model, session_dir: Path, label: str, min_conf: float, imgsz: int,
                   select: str, overwrite: bool, include_empty: bool,
                   output_root: Path, progress=None) -> Tuple[int, int, Path]:
    """Run the retained detector/export logic and write only under output_root."""
    if label not in VALID_LABELS:
        raise ValueError(f"Invalid protocol class: {label!r}")
    output_path = output_path_for_session(session_dir, output_root)
    if output_path.exists() and not overwrite:
        print(f"Skipping existing export: {output_path}")
        return 0, 0, output_path
    color_dir = session_dir / "color_frames"
    # Preserve the original filename sort and supported image extensions.
    image_paths = sorted(path for path in color_dir.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS)
    export = {"classes": CLASSES, "images": []}
    detected = 0
    iterator = image_paths if progress is None else progress(image_paths, desc=f"{session_dir.name} [{label}]", unit="img")
    for image_path in iterator:
        results = model.predict(str(image_path), classes=[0], conf=min_conf, imgsz=imgsz, verbose=False)
        result = results[0]
        annotations = []
        selected = select_person_box(result, min_conf=min_conf, select=select)
        if selected is not None:
            img_h, img_w = result.orig_shape[:2]
            x1, y1, x2, y2, _conf = selected
            annotations.append({"class": label,
                                "boundingBox": clamp_box(x1, y1, x2, y2, img_w=img_w, img_h=img_h)})
            detected += 1
        if annotations or include_empty:
            export["images"].append({"image": image_path.name, "annotations": annotations})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with output_path.open(mode, encoding="utf-8") as stream:
        json.dump(export, stream, indent=None, separators=(",", ":"))
    return len(image_paths), detected, output_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="Recording directory or dataset root containing color_frames folders")
    parser.add_argument("--output-root", required=True, type=Path, help="Separate destination: P_N/P_N_S_M/2D-on-2D_annotations_export.json")
    labels = parser.add_mutually_exclusive_group(required=True)
    labels.add_argument("--label", choices=sorted(VALID_LABELS), help="One explicit protocol class for every selected recording")
    labels.add_argument("--label-map", help="CSV with session,label columns")
    labels.add_argument("--sessions-csv", help="Canonical CSV with source_session_id (or session_id),condition_code")
    parser.add_argument("--model", default="yolov8x.pt", help="YOLO model path/name; default yolov8x.pt")
    parser.add_argument("--conf", type=float, default=0.25, help="Minimum person confidence (default 0.25)")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference image size (default 640)")
    parser.add_argument("--select", choices=["largest", "highest_conf"], default="largest")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing exports in the output directory only")
    parser.add_argument("--include-empty", action="store_true", help="Retain image records with no selected person detection")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error("Input root is not a directory")
    if not 0 <= args.conf <= 1 or args.imgsz < 1:
        parser.error("Confidence must be between 0 and 1 and image size must be positive")
    validate_roots(args.root, args.output_root)
    mapping = load_label_map(args.sessions_csv or args.label_map, canonical=bool(args.sessions_csv))
    sessions = find_session_dirs(args.root)
    if not sessions:
        parser.error("No color_frames directories found under the input root")
    plan = [(session, get_label_for_session(session, args.label, mapping),
             output_path_for_session(session, args.output_root)) for session in sessions]
    if len({str(target) for _, _, target in plan}) != len(plan):
        parser.error("Multiple source directories map to the same output recording")
    # Imports happen after CLI/input validation; --help and fixture tests never
    # load a model, download weights or require the detector dependencies.
    try:
        from tqdm import tqdm
        from ultralytics import YOLO
    except ImportError:
        parser.error("Install the dependencies from code/images/requirements.txt before running detection")
    model = YOLO(args.model)
    total_images = total_detected = 0
    for session, label, _ in plan:
        images, detected, path = export_session(model, session, label, args.conf, args.imgsz,
            args.select, args.overwrite, args.include_empty, args.output_root, progress=tqdm)
        total_images += images
        total_detected += detected
        if images:
            print(f"Saved: {path} | detected {detected}/{images} frames")
    print(f"Done. Total detections: {total_detected}/{total_images} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
