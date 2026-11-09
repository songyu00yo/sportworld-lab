#!/usr/bin/env python3
"""Install fixed, checksum-verified official Google SDK archives locally."""
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / ".lab/android"
SDK = ANDROID / "sdk"
TOOLS_METADATA = "https://dl.google.com/android/repository/repository2-3.xml"
# Size and SHA1 are pinned from the official repository metadata, 2026-11-09.
# Each tuple is (archive URL, byte count, SHA1, SDK-relative destination, ZIP root).
PACKAGES = (
    ("https://dl.google.com/android/repository/commandlinetools-mac_arm64-16111833_latest.zip",
     155384151, "ad03dc49bfacfd52c110b14104ea548b8a07e830", "cmdline-tools/latest", "cmdline-tools"),
    ("https://dl.google.com/android/repository/platform-tools_r37.0.1-darwin.zip",
     16110554, "6ae73f4de6452dc57e62ec02b68eed92a4c21661", "platform-tools", "platform-tools"),
    ("https://dl.google.com/android/repository/emulator-darwin_aarch64-16740729.zip",
     416113219, "6c5e61b873e11c7d1d08683bea089f762baee070", "emulator", "emulator"),
    ("https://dl.google.com/android/repository/sys-img/android/arm64-v8a-34_r04.zip",
     705115396, "d9f2011131919abe952814e041f16f317242c3fa", "system-images/android-34/default/arm64-v8a", "arm64-v8a"),
)


def verified(path, size, checksum):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha1").hexdigest() == checksum


def download(url, size, checksum, downloads):
    destination = downloads / url.rsplit("/", 1)[1]
    if verified(destination, size, checksum):
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=downloads, prefix=".download-", delete=False) as stream:
            temporary = Path(stream.name)
            print(f"Downloading {destination.name} ({size} bytes)", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response:
                shutil.copyfileobj(response, stream)
        if not verified(temporary, size, checksum):
            raise ValueError(f"Official package size or checksum mismatch: {destination.name}")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def extract(archive_path, target, zip_root, checksum, metadata=None):
    marker = target / ".sportworld-package-sha1"
    if target.exists():
        if marker.is_file() and marker.read_text().strip() == checksum:
            return
        raise ValueError(f"Existing SDK package is not managed by this installer: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".extract-") as directory:
        staging = Path(directory) / "package"
        staging.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                relative = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (relative.is_absolute() or ".." in relative.parts or "\\" in info.filename
                        or not relative.parts or relative.parts[0] != zip_root
                        or (mode & 0o170000) == 0o120000):
                    raise ValueError(f"Unsafe or unexpected SDK archive entry: {info.filename}")
                if len(relative.parts) == 1:
                    continue
                output = staging.joinpath(*relative.parts[1:])
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, output.open("wb") as stream:
                        shutil.copyfileobj(source, stream)
                    if mode & 0o777:
                        output.chmod(mode & 0o777)
        if metadata is not None:
            (staging / "package.xml").write_text(metadata, encoding="utf-8")
        (staging / marker.name).write_text(checksum + "\n", encoding="utf-8")
        os.replace(staging, target)


def emulator_metadata():
    # Keep Google's namespaces and license text for avdmanager registration.
    with urllib.request.urlopen(TOOLS_METADATA, timeout=60) as response:
        metadata = response.read().decode("utf-8")
    pinned = PACKAGES[2]
    blocks = re.findall(r'<remotePackage path="emulator">.*?</remotePackage>', metadata, re.S)
    package = next((block for block in blocks if pinned[0].rsplit("/", 1)[1] in block), None)
    if package is None or pinned[2] not in package or f"<size>{pinned[1]}</size>" not in package:
        raise ValueError("Official metadata no longer contains the pinned emulator. Review the source versions")
    package = re.sub(r"<archives>.*?</archives>", "", package, flags=re.S)
    package = re.sub(r"<channelRef[^>]*/>", "", package).replace("remotePackage", "localPackage")
    license_text = re.search(r'<license id="android-sdk-license".*?</license>', metadata, re.S)
    root_open = re.search(r"<sdk:sdk-repository[^>]*>", metadata)
    if license_text is None or root_open is None:
        raise ValueError("Official SDK metadata format changed")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            + root_open.group().replace("sdk:sdk-repository", "common:repository")
            + license_text.group() + package + "</common:repository>\n")


def install():
    downloads = ANDROID / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    SDK.mkdir(parents=True, exist_ok=True)
    with (ANDROID / "install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for url, size, checksum, relative, zip_root in PACKAGES:
            target = SDK / relative
            marker = target / ".sportworld-package-sha1"
            if marker.is_file() and marker.read_text().strip() == checksum:
                print(f"Already installed {relative}", flush=True)
                continue
            metadata = emulator_metadata() if relative == "emulator" else None
            archive = download(url, size, checksum, downloads)
            extract(archive, target, zip_root, checksum, metadata)
            print(f"Verified and installed {relative}", flush=True)


if __name__ == "__main__":
    raise SystemExit("Run python3 scripts/bootstrap.py --android to initialize and install")
