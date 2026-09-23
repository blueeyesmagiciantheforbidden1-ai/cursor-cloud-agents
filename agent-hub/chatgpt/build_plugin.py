"""Build an allowlisted plugin archive, optionally binding a registered app ID.

This script performs no account, network, marketplace, or installation operations.
It does not verify that the supplied app exists; confirm it in ChatGPT first.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / "runcrew-hub"
APP_ID = re.compile(r"plugin_asdk_app_[0-9a-f]{32}\Z")
FILES = (
    ".codex-plugin/plugin.json",
    "skills/runcrew-hub/SKILL.md",
)


def build(output: Path, app_id: str | None = None, *, skills_only: bool = False,
          plugin: Path = PLUGIN) -> list[str]:
    if app_id is not None and not APP_ID.fullmatch(app_id):
        raise ValueError("Use the registered plugin_asdk_app ID copied from ChatGPT, not a URL or token")
    if bool(app_id) == bool(skills_only):
        raise ValueError("Choose a real --app-id or explicitly request --skills-only")
    output = output.resolve()
    plugin = plugin.resolve()
    if output.is_relative_to(plugin):
        raise ValueError("Keep generated archives outside the plugin source")
    contents = {}
    for relative in FILES:
        path = plugin / relative
        if not path.resolve().is_relative_to(plugin) or path.is_symlink():
            raise ValueError("Plugin inputs must be regular files inside the plugin folder")
        contents[relative] = path.read_bytes()
    manifest = json.loads(contents[FILES[0]])
    if manifest.get("name") != "runcrew-hub" or manifest.get("skills") != "./skills/":
        raise ValueError("Unexpected plugin identity or skill path")
    # Do not inherit unreviewed extra connections from a source manifest.
    if "apps" in manifest or "mcpServers" in manifest:
        raise ValueError("Source manifest must remain unbound; this builder adds the one app connection")
    if app_id:
        manifest["apps"] = "./.app.json"
        contents[".app.json"] = (json.dumps({"apps": {"runcrew-hub": {"id": app_id}}}, indent=2) + "\n").encode()
    contents[FILES[0]] = (json.dumps(manifest, indent=2) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse replacement so interrupted/manual builds cannot silently replace a release.
    with output.open("xb") as target:
        with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
            for relative, data in contents.items():
                archive.writestr(relative, data)
    return list(contents)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", help="Actual registered ChatGPT app ID; not a secret")
    parser.add_argument("--skills-only", action="store_true", help="Build a preview without a live MCP connection")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        files = build(args.output, args.app_id, skills_only=args.skills_only)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Plugin build failed: {exc}\n")
    print(f"Built {args.output.resolve()} with {len(files)} allowlisted files")
    print("This archive is not installed or connected until the ChatGPT setup is completed.")


if __name__ == "__main__":
    main()
