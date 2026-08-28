"""Install the pinned Alpaca CLI release after verifying its SHA-256 digest."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import tarfile
import urllib.request


VERSION = "0.0.14"
RELEASE_COMMIT = "53606273aa230a40c64b783425dcb3f4423ede30"
ASSETS = {
    "amd64": (
        "cli_0.0.14_linux_amd64.tar.gz",
        "6c82ef31f94dd61aae1c90e40fc41fdfaf8111bd50e9a2780b9d8d304eb2ba66",
    ),
    "arm64": (
        "cli_0.0.14_linux_arm64.tar.gz",
        "621270e2b935dbae587e6ae05fe04a10bc178b4c9c638961a3d0214568ff2617",
    ),
}


def verified_binary(archive: bytes, expected_sha256: str) -> bytes:
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(f"Alpaca CLI checksum mismatch: expected {expected_sha256}, got {actual}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        candidates = [member for member in bundle.getmembers() if member.isfile() and Path(member.name).name == "alpaca"]
        if len(candidates) != 1:
            raise RuntimeError("Alpaca CLI archive must contain exactly one alpaca binary")
        extracted = bundle.extractfile(candidates[0])
        if extracted is None:
            raise RuntimeError("Alpaca CLI binary could not be extracted")
        return extracted.read()


def install(arch: str, output: Path) -> None:
    try:
        asset, expected_sha256 = ASSETS[arch]
    except KeyError as exc:
        raise RuntimeError(f"unsupported Alpaca CLI architecture: {arch}") from exc
    url = f"https://github.com/alpacahq/cli/releases/download/v{VERSION}/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": "alpaca-trading-bot-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read()
    binary = verified_binary(archive, expected_sha256)
    output.write_bytes(binary)
    os.chmod(output, 0o755)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", required=True, choices=sorted(ASSETS))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    install(args.arch, args.output)


if __name__ == "__main__":
    main()
