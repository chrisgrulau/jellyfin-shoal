#!/usr/bin/env python3
"""Builds manifest.json, the Jellyfin plugin repository for the Shoal plugins, from their GitHub releases.

Only releases that are published, immutable, carry SHA256SUMS and have a build-provenance attestation are listed. Each zip is downloaded and checked
against SHA256SUMS before its MD5 (what Jellyfin checks on install) goes into the manifest. Plugin details and the
changelog come from build.yaml at the release's tag, and the version there must match the tag.

    tools/update_manifest.py          write manifest.json
    tools/update_manifest.py --check  exit 1 if manifest.json is out of date

Standard library only. Set GITHUB_TOKEN to avoid the API's anonymous rate limit.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"
PLUGINS = ROOT / "plugins.json"
MAX_ZIP_BYTES = 50 * 1024 * 1024
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?$")


def fetch(url, api=False):
    headers = {"User-Agent": "jellyfin-shoal-manifest"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        data = response.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"{url} is larger than {MAX_ZIP_BYTES} bytes")
    return data


def has_build_provenance(repo, digest):
    """Whether GitHub holds a SLSA build-provenance attestation for this file in the repository."""
    try:
        listed = json.loads(fetch(f"https://api.github.com/repos/{repo}/attestations/sha256:{digest}", api=True))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    for item in listed.get("attestations", []):
        envelope = (item.get("bundle") or {}).get("dsseEnvelope") or {}
        try:
            statement = json.loads(base64.b64decode(envelope.get("payload", "")))
        except ValueError:
            continue
        if str(statement.get("predicateType", "")).startswith("https://slsa.dev/provenance/"):
            return True
    return False


def raw(repo, ref, path):
    try:
        return fetch(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def parse_build_yaml(text):
    """Reads the flat subset of YAML that build.yaml uses: quoted scalars and folded (>) blocks."""
    values, key, block = {}, None, []
    for line in text.splitlines():
        if key is not None:
            if line.startswith((" ", "\t")) or not line.strip():
                block.append(line.strip())
                continue
            values[key] = " ".join(p for p in block if p)
            key, block = None, []
        m = re.match(r'^([A-Za-z]+):\s*(.*)$', line)
        if not m:
            continue
        name, rest = m.groups()
        if rest in (">", ">-", "|", "|-"):
            key = name
        elif rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            values[name] = rest[1:-1]
        elif rest and not rest.startswith("#"):
            values[name] = rest
    if key is not None:
        values[key] = " ".join(p for p in block if p)
    return values


def versions_of(plugin):
    repo, asset = plugin["repo"], plugin["asset"]
    releases = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases?per_page=100", api=True))
    found = []
    for release in releases:
        tag = release["tag_name"]
        names = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        if release.get("draft") or not release.get("immutable") or asset not in names or "SHA256SUMS" not in names:
            continue
        m = TAG.match(tag)
        if not m:
            continue
        version = ".".join(m.groups()) + ".0"

        build = raw(repo, tag, "build.yaml")
        if build is None:
            raise ValueError(f"{repo} {tag}: no build.yaml")
        meta = parse_build_yaml(build.decode("utf-8"))
        if meta.get("version") != version:
            raise ValueError(f"{repo} {tag}: build.yaml says {meta.get('version')}, the tag means {version}")

        sums = {}
        for line in fetch(names["SHA256SUMS"]).decode("utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0].lower()
        data = fetch(names[asset])
        digest = hashlib.sha256(data).hexdigest()
        if sums.get(asset) != digest:
            raise ValueError(f"{repo} {tag}: {asset} doesn't match SHA256SUMS")

        # Only zips built by the plugin's CI (a build-provenance attestation, not just the release's own) are listed
        if not has_build_provenance(repo, digest):
            continue

        found.append((tuple(int(x) for x in m.groups()), tag, meta, {
            "version": version,
            "changelog": meta.get("changelog", ""),
            "targetAbi": meta["targetAbi"],
            "sourceUrl": names[asset],
            "checksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),  # what Jellyfin verifies on install
            "timestamp": release["published_at"],
        }))
    found.sort(key=lambda v: v[0], reverse=True)
    return found


def build():
    manifest = []
    for plugin in json.loads(PLUGINS.read_text(encoding="utf-8")):
        found = versions_of(plugin)
        if not found:
            print(f"{plugin['repo']}: no verified releases yet, left out", file=sys.stderr)
            continue
        _, newest_tag, meta, _ = found[0]
        # The icon from the newest release's tag, so it only changes with a release
        image_ref = newest_tag if raw(plugin["repo"], newest_tag, plugin["image"]) is not None else "main"
        manifest.append({
            "guid": meta["guid"],
            "name": meta["name"],
            "description": meta.get("description", ""),
            "overview": meta.get("overview", ""),
            "owner": meta.get("owner", ""),
            "category": meta.get("category", "General"),
            "imageUrl": f"https://raw.githubusercontent.com/{plugin['repo']}/{image_ref}/{plugin['image']}",
            "versions": [v for _, _, _, v in found],
        })
        print(f"{plugin['repo']}: {', '.join(v['version'] for _, _, _, v in found)}", file=sys.stderr)
    manifest.sort(key=lambda p: p["name"])
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if manifest.json is out of date")
    args = parser.parse_args()
    text = build()
    if args.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if current != text:
            print("manifest.json is out of date: run tools/update_manifest.py", file=sys.stderr)
            return 1
        print("manifest.json is up to date", file=sys.stderr)
        return 0
    MANIFEST.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
