"""Validate a download manifest without opening participant files (Python 3.9+)."""

import argparse
import csv
from pathlib import PurePosixPath, PureWindowsPath
import re

FIELDS = {"relative_path", "bytes", "sha256", "modality", "data_level", "access_tier"}
TIERS = {"public_documentation", "anonymised_embargoed", "restricted"}


def relative_path(value):
    parts = value.split("/")
    if (not value or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).drive
            or any(p in {"", ".", ".."} or ":" in p for p in parts)):
        raise ValueError("unsafe relative path: " + repr(value))
    return value


def validate(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if len(reader.fieldnames or []) != len(FIELDS) or set(reader.fieldnames or []) != FIELDS:
            raise ValueError("manifest must have exactly these columns: " + ", ".join(sorted(FIELDS)))
        seen = set()
        count = 0
        for row in reader:
            name = relative_path(row["relative_path"])
            # Portable manifests must not collide on case-insensitive systems.
            if name.casefold() in seen:
                raise ValueError("duplicate/case-colliding path: " + name)
            seen.add(name.casefold())
            if not re.fullmatch(r"0|[1-9][0-9]*", row["bytes"] or ""):
                raise ValueError("invalid byte count: " + name)
            if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"] or ""):
                raise ValueError("invalid SHA256: " + name)
            if not row["modality"] or not row["data_level"] or row["access_tier"] not in TIERS:
                raise ValueError("invalid data classification: " + name)
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed CSV row: " + name)
            count += 1
    if not count:
        raise ValueError("empty manifest")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    try:
        print(f"Valid manifest: {validate(args.manifest)} files")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, str(error) + "\n")
