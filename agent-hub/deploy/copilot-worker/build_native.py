"""Install only the audited native ELF; never run login, scripts, or inference."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

AGENT = "copilot"
VERSION = "1.0.86"
URL = "https://github.com/github/copilot-cli/releases/download/v1.0.86/copilot-linux-x64.tar.gz"
ARCHIVE_SIZE = 97598899
ARCHIVE_SHA256 = "ea4a519d7b2ff54e9c7d10ae9921cf4ab3f5041489ec340852f47f4f63fc535b"
BINARY_SIZE = 166792000
BINARY_SHA256 = "be0152ea29b06d54dd23e1fc5512e978b4e84097f6da5314df96b3731a2dd6aa"

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
        with tarfile.open(archive, "r:gz") as packed:
            members = packed.getmembers()
            if len(members) != 1 or members[0].name != AGENT or not members[0].isfile() or members[0].size != BINARY_SIZE:
                raise ValueError("Unexpected archive layout")
            with packed.extractfile(members[0]) as source, target.open("xb") as dest:
                while chunk := source.read(1024 * 1024):
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

