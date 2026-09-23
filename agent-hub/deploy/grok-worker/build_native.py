"""Install only the audited native ELF; never run login, scripts, or inference."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

AGENT = "grok"
VERSION = "1.0.40"
URL = "https://x.ai/cli/grok-1.0.40-linux-x86_64.gz"
ARCHIVE_SIZE = 67786972
ARCHIVE_SHA256 = "de95a1d1a17eaa3eaa4218212c7e1c2e80a36e3ed6a5033b7dd117be02a128e9"
BINARY_SIZE = 165587968
BINARY_SHA256 = "92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc"

def check_binary(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00"
            or path.stat().st_size != BINARY_SIZE or digest != BINARY_SHA256):
        raise ValueError("Native ELF does not match audited release")

def install(output):
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="runcrew-release-") as temporary:
        archive = Path(temporary) / "download"
        with urllib.request.urlopen(URL, timeout=45) as response, archive.open("wb") as dest:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > ARCHIVE_SIZE:
                    raise ValueError("Download exceeds published bounds")
                dest.write(chunk)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if count != ARCHIVE_SIZE or digest != ARCHIVE_SHA256:
            raise ValueError("Download digest mismatch")
        target = output / AGENT
        with gzip.open(archive, "rb") as source, target.open("xb") as dest:
            length = 0
            while chunk := source.read(1024 * 1024):
                length += len(chunk)
                if length > BINARY_SIZE:
                    raise ValueError("Expanded binary exceeds bound")
                dest.write(chunk)
        check_binary(target)
        target.chmod(0o555)
        profile = {"schema": 1, "cli_version": VERSION, "sha256": BINARY_SHA256,
                   "archive_sha256": ARCHIVE_SHA256, "native_check": "pending"}
        (output / "runtime-profile.json").write_text(json.dumps(profile, sort_keys=True), encoding="utf-8")
        (output / "runtime-profile.json").chmod(0o444)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    install(args.output)

if __name__ == "__main__":
    main()

