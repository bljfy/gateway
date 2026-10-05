"""Build pinned GmSSL from verified source into a checkout-local tool directory."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

SOURCE_URL = "https://github.com/guanzhi/GmSSL/archive/refs/tags/v3.1.1.zip"
SOURCE_SHA256 = "edc33efd90cedddf061aee8295dc247dadbb3df7f5f96154ed699831c87d3416"
SOURCE_COMMIT = "d655c06b3a6b0fe8cff900f293bf0e5aac6eb0a2"


def build(root: Path, cmake: str, ninja: str | None, compiler: str | None) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "source.zip"
    if not archive.exists():
        with (
            urllib.request.urlopen(SOURCE_URL, timeout=60) as response,
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != SOURCE_SHA256:
            raise RuntimeError("GmSSL source checksum mismatch")
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (root / member.filename).resolve().is_relative_to(root):
                raise RuntimeError("unsafe source archive path")
        package.extractall(root)
    source_dir, build_dir = root / "GmSSL-3.1.1", root / "build"
    version = subprocess.run([cmake, "--version"], check=True, capture_output=True, text=True)
    match = re.search(r"cmake version (\d+)\.", version.stdout)
    command = [
        cmake,
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=ON",
    ]
    if match is not None and int(match[1]) >= 4:
        command.append("-DCMAKE_POLICY_VERSION_MINIMUM=3.10")
    if ninja:
        command.extend(["-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={Path(ninja).resolve()}"])
    if compiler:
        command.append(f"-DCMAKE_C_COMPILER={Path(compiler).resolve()}")
    if sys.platform == "win32" and compiler and "gcc" in compiler.lower():
        command.append("-DCMAKE_SHARED_LINKER_FLAGS=-Wl,--export-all-symbols")
    subprocess.run(command, check=True)
    subprocess.run(
        [
            cmake,
            "--build",
            str(build_dir),
            "--config",
            "Release",
            "--target",
            "gmssl",
            "sm2test",
            "sm3test",
            "sm4test",
            "--parallel",
            "4",
        ],
        check=True,
    )
    ctest = str(Path(cmake).with_name("ctest.exe" if os.name == "nt" else "ctest"))
    subprocess.run(
        [
            ctest,
            "--test-dir",
            str(build_dir),
            "-C",
            "Release",
            "-R",
            "^(sm2|sm3|sm4)$",
            "--output-on-failure",
        ],
        check=True,
    )
    install = root / "lib"
    install.mkdir(exist_ok=True)
    if sys.platform == "win32":
        candidates = list((build_dir / "bin").rglob("*gmssl.dll"))
        if len(candidates) != 1:
            raise RuntimeError("ambiguous GmSSL build output")
        library = install / "gmssl.dll"
        shutil.copyfile(candidates[0], library)
    elif sys.platform == "linux":
        library = install / "libgmssl.so.3.1"
        shutil.copyfile(build_dir / "bin/libgmssl.so.3.1", library)
        for name in ("libgmssl.so.3", "libgmssl.so"):
            alias = install / name
            if not alias.exists():
                alias.symlink_to(library.name)
    else:
        raise RuntimeError("unsupported build platform")
    with library.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    manifest = {
        "source_url": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "source_sha256": SOURCE_SHA256,
        "binding": "gmssl-python==2.2.2",
        "native_version": "3.1.1",
        "platform": sys.platform,
        "library": str(library),
        "sha256": digest,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return library


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".tools/gmssl"))
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--ninja")
    parser.add_argument("--compiler")
    args = parser.parse_args()
    build(args.root, args.cmake, args.ninja, args.compiler)


if __name__ == "__main__":
    main()
