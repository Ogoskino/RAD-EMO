"""Verify SHA256SUMS inside a download directory (Python 3.9+)."""

import argparse
import hashlib
from pathlib import Path
import re

from validate_manifest import relative_path


def verify(root, checksum_file):
    root = Path(root).resolve(strict=True)
    seen = set()
    count = 0
    for line in Path(checksum_file).read_text(encoding="utf-8-sig").splitlines():
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError("invalid checksum line")
        expected, name = match.groups()
        relative_path(name)
        if name.casefold() in seen:
            raise ValueError("duplicate/case-colliding path: " + name)
        seen.add(name.casefold())
        path = (root / name).resolve(strict=True)
        if root not in path.parents or not path.is_file():
            raise ValueError("file outside download directory: " + name)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("checksum mismatch: " + name)
        count += 1
    if not count:
        raise ValueError("empty checksum file")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("checksums")
    args = parser.parse_args()
    try:
        print(f"Verified: {verify(args.directory, args.checksums)} files")
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
