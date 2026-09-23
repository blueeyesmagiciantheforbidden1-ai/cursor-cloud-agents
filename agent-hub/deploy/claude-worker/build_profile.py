"""Install one signed, pinned Claude release; never authenticate or run inference."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "2.1.275"
PLATFORM = "linux-x64"
SHA256 = "13586f3150a7ca1655f36e1dba759fb404e0f7cf7021d4a3dcd5e6f604e56156"
SIZE = 232059192
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
REPOSITORY = "https://downloads.claude.ai/claude-code-releases"
KEY_URL = "https://downloads.claude.ai/keys/claude-code.asc"


class BuildError(ValueError):
    pass


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname != "downloads.claude.ai"
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise BuildError("Official download redirected outside its approved origin")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(url: str, path: Path, limit: int) -> None:
    opener = build_opener(OfficialRedirects())
    request = Request(url, headers={"User-Agent": "RunCrew-release-verifier/1"})
    size = 0
    with opener.open(request, timeout=60) as response, path.open("xb") as destination:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise BuildError("Official artifact exceeded its expected size limit")
            destination.write(chunk)


def run_gpg(gpg: str, home: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [gpg, "--no-options", "--batch", "--no-tty", "--no-autostart",
             "--homedir", str(home), *arguments],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"Public-metadata GPG step {arguments[0]} timed out") from exc
    if result.returncode:
        # All inputs to this subprocess are public release metadata in an
        # isolated keyring, so its bounded error is safe diagnostic evidence.
        detail = " ".join(result.stderr.split())[:700]
        raise BuildError(f"Release-signature verification failed: {detail}")
    return result.stdout


def run_gpgv(gpg: str, home: Path, keyring: Path, signature: Path, manifest: Path) -> str:
    binary = str(Path(gpg).with_name("gpgv.exe" if Path(gpg).suffix.lower() == ".exe" else "gpgv"))
    try:
        # gpgv resolves relative keyrings against --homedir; this also avoids
        # MSYS treating a Windows drive-qualified keyring as a relative name.
        result = subprocess.run([binary, "--homedir", str(home), "--keyring", keyring.name,
                                 "--status-fd", "1", str(signature), str(manifest)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BuildError("Public manifest signature verification timed out") from exc
    if result.returncode:
        raise BuildError("Detached release signature is invalid: " + " ".join(result.stderr.split())[:700])
    return result.stdout


def verify_signature(gpg: str, home: Path, key: Path, signature: Path, manifest: Path) -> None:
    fingerprints = run_gpg(gpg, home, "--with-colons", "--import-options", "show-only",
                           "--import", str(key))
    primary = []
    awaiting_primary = False
    for line in fingerprints.splitlines():
        parts = line.split(":")
        if parts[0] == "pub":
            awaiting_primary = True
        elif parts[0] == "fpr" and awaiting_primary:
            primary.append(parts[9])
            awaiting_primary = False
    if primary != [FINGERPRINT]:
        raise BuildError("Release public-key fingerprint differs from the documented pin")
    keyring = home / "release-key.gpg"
    # A detached public signature needs no private-key agent or imported user
    # keyring. Use only the fingerprint-checked key for this verification.
    run_gpg(gpg, home, "--dearmor", "--output", str(keyring), str(key))
    status = run_gpgv(gpg, home, keyring, signature, manifest)
    valid = []
    for line in status.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            valid.append(fields)
    if len(valid) != 1 or FINGERPRINT not in (valid[0][2], valid[0][-1]):
        raise BuildError("Manifest signature does not belong to the pinned release key")


def verify_manifest(raw: bytes) -> dict:
    def unique(entries):
        output = {}
        for key, value in entries:
            if key in output:
                raise BuildError("Release manifest contains duplicate keys")
            output[key] = value
        return output
    body = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(body, dict) or body.get("version") != VERSION:
        raise BuildError("Release manifest version differs from the audited CLI")
    platforms = body.get("platforms")
    platform = platforms.get(PLATFORM) if isinstance(platforms, dict) else None
    if (not isinstance(platform, dict) or platform.get("binary") != "claude"
            or platform.get("checksum") != SHA256 or type(platform.get("size")) is not int
            or platform["size"] != SIZE):
        raise BuildError("Release manifest does not match the pinned Linux artifact")
    return body


def install(output: Path, *, gpg: str, metadata_only: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise BuildError("Output directory must be empty; never overwrite an existing release")
    with tempfile.TemporaryDirectory(prefix="runcrew-release-") as temporary:
        stage = Path(temporary)
        key, signature, manifest = (stage / name for name in
                                    ("claude-code.asc", "manifest.json.sig", "manifest.json"))
        keyring = stage / "keyring"
        keyring.mkdir(mode=0o700)
        download(KEY_URL, key, 128_000)
        download(f"{REPOSITORY}/{VERSION}/manifest.json", manifest, 128_000)
        download(f"{REPOSITORY}/{VERSION}/manifest.json.sig", signature, 32_000)
        verify_signature(gpg, keyring, key, signature, manifest)
        verify_manifest(manifest.read_bytes())
        receipt = {
            "version": VERSION, "platform": PLATFORM, "sha256": SHA256, "size": SIZE,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "signing_key_fingerprint": FINGERPRINT,
            "manifest_url": f"{REPOSITORY}/{VERSION}/manifest.json",
            "signature_verified": True, "binary_verified": False,
        }
        if not metadata_only:
            binary = stage / "claude"
            download(f"{REPOSITORY}/{VERSION}/{PLATFORM}/claude", binary, SIZE)
            with binary.open("rb") as handle:
                if handle.read(4) != b"\x7fELF":
                    raise BuildError("Downloaded release is not a native Linux ELF")
                handle.seek(0)
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if binary.stat().st_size != SIZE or digest != SHA256:
                raise BuildError("Native binary does not match the signed manifest")
            binary.chmod(0o555)
            probe_home = stage / "probe-home"
            probe_home.mkdir(mode=0o700)
            probe = subprocess.run([str(binary), "--version"], cwd=probe_home,
                                   env={"HOME": str(probe_home), "PATH": "/usr/bin:/bin",
                                        "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1",
                                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=30, check=False)
            if probe.returncode or probe.stdout.strip() != f"{VERSION} (Claude Code)":
                raise BuildError("Native binary did not report the audited version")
            shutil.copyfile(binary, output / "claude")
            (output / "claude").chmod(0o555)
            profile = {"schema": 1, "cli_version": VERSION, "sha256": SHA256}
            (output / "runtime-profile.json").write_text(json.dumps(profile) + "\n", encoding="utf-8")
            (output / "runtime-profile.json").chmod(0o444)
            receipt["binary_verified"] = True
        for item in (manifest, signature, key):
            (output / item.name).write_bytes(item.read_bytes())
        (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpg", default="gpg")
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.output, gpg=args.gpg, metadata_only=args.metadata_only)))
        return 0
    except BuildError as exc:
        print(f"Release verification failed: {exc}")
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Release verification failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
