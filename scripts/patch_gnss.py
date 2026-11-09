#!/usr/bin/env python3
"""Build the pinned one-byte GNSS variant from a user-supplied AOSP original."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SIZE = 141248
OFFSET = 0x147CC
ORIGINAL_SHA256 = "ea1ec67a7e1d4e8407b7162d49929477e142cdaf815ce59d01cb34480a10b93c"
PATCHED_SHA256 = "4c77d8efbcab5ee685c3e40fa6553f93e0d1317698d2795c79b79465e98c2c41"
DESTINATION = ROOT / ".lab/gnss-analysis/gnss-used-in-fix-quality1-test"


def patch(source, destination=DESTINATION):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("The output must differ from the original file")
    if source.stat().st_size != SIZE:
        raise ValueError("Unknown GNSS original size")
    original = source.read_bytes()
    if len(original) != SIZE or hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("Unknown GNSS original. Expected the Android 34 AOSP arm64-v8a r04 binary")
    if original[OFFSET] != 0x0C:
        raise ValueError("The original instruction does not match the reviewed patch")
    modified = bytearray(original)
    modified[OFFSET] = 0x8C
    if hashlib.sha256(modified).hexdigest() != PATCHED_SHA256:
        raise ValueError("The patched binary checksum does not match")
    if destination.exists() and destination.read_bytes() == modified:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".gnss-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(modified)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="Binary pulled from your clean Android 34 AOSP r04 AVD")
    parser.add_argument("--output", type=Path, default=DESTINATION)
    args = parser.parse_args()
    try:
        result = patch(args.original, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(f"Verified {PATCHED_SHA256}  {result}")


if __name__ == "__main__":
    main()
