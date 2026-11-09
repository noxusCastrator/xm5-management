"""Hash-locked Windows portable assembly; uses stdlib only and never runs pip/installers.

Usage: bundled-python assemble.py [--offline] [--output PATH] [--source PATH]
The source must be a code-only tree. Existing output is refused, never deleted.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def fetch(item, offline):
    target = ROOT / "downloads" / item["filename"]
    if target.exists():
        if digest(target) != item["sha256"]:
            raise RuntimeError(f"Cached download checksum mismatch: {target}")
        return target
    if offline:
        raise RuntimeError(f"Offline build input missing: {target}")
    request = urllib.request.Request(item["url"], headers={"User-Agent": "XM5-portable-build/1"})
    temporary = target.with_name(target.name + ".part")
    with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    if digest(temporary) != item["sha256"]:
        raise RuntimeError(f"Downloaded input checksum mismatch: {item['url']}")
    temporary.replace(target)
    return target


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError(f"Unsafe archive entry: {member.filename}")
        package.extractall(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--source", type=Path, default=ROOT / "app")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "XM5Library-portable-win-x64")
    args = parser.parse_args()
    lock = json.loads((ROOT / "portable-lock.json").read_text(encoding="utf-8"))
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output already exists; choose a new directory: {output}")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise SystemExit("Source and output must be separate trees")
    prohibited = {".venv", ".git", "data", "cache", "runtime", "config.json", "connection.json", "credentials.json"}
    for path in source.rglob("*"):
        if path.name in prohibited or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise SystemExit(f"Source is not a code-only ordinary-file tree: {path}")
    asset_manifest = ROOT / "ASSET-BASELINE.json"
    if asset_manifest.exists():
        expected_assets = {row["path"]: row for row in json.loads(asset_manifest.read_text(encoding="utf-8"))}
        asset_root = source / "xm5lib/ui/assets"
        actual_assets = {p.relative_to(asset_root).as_posix(): p for p in asset_root.rglob("*") if p.is_file()}
        if actual_assets.keys() != expected_assets.keys():
            raise SystemExit("UI asset inventory differs from the explicit hash-verified asset source")
        for name, path in actual_assets.items():
            if path.stat().st_size != expected_assets[name]["size"] or digest(path) != expected_assets[name]["sha256"]:
                raise SystemExit(f"UI asset checksum mismatch: {name}")
    items = [lock["python"], lock["mpv"], lock["ffmpeg"], *lock["wheels"]]
    (ROOT / "downloads").mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        inputs = list(pool.map(lambda item: fetch(item, args.offline), items))
    shutil.copytree(source, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "VALIDATION.json"))
    vendor = output / "vendor"
    python = vendor / "python"
    python.mkdir(parents=True)
    unpack(inputs[0], python)
    packages = python / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    for wheel in inputs[3:]:
        unpack(wheel, packages)
    # No `import site`: the application does not execute arbitrary .pth files,
    # user-site code, registry Python paths or inherited PYTHONPATH.
    (python / "python314._pth").write_text("python314.zip\n.\nLib\\site-packages\n..\\..\n", encoding="utf-8", newline="\n")
    (vendor / "mpv").mkdir()
    with zipfile.ZipFile(inputs[1]) as archive:
        for name in ("mpv.exe", "vulkan-1.dll"):
            (vendor / "mpv" / name).write_bytes(archive.read(name))
    # A new Windows machine need not have the Visual C++ redistributable.
    # These unchanged redistributable DLLs are shipped by the pinned official
    # PySide6 wheel, including its licenses/third-party notices. A child process
    # does not search the Python executable's directory for its dependencies.
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        shutil.copy2(packages / "PySide6" / name, vendor / "mpv" / name)
    ffmpeg = vendor / "ffmpeg"
    ffmpeg.mkdir()
    with zipfile.ZipFile(inputs[2]) as archive:
        prefix = next(name.rsplit("bin/ffmpeg.exe", 1)[0] for name in archive.namelist() if name.endswith("bin/ffmpeg.exe"))
        (ffmpeg / "ffmpeg.exe").write_bytes(archive.read(prefix + "bin/ffmpeg.exe"))
        # Keep the publisher's build configuration, license and complete notices.
        for member in archive.infolist():
            relative = member.filename.removeprefix(prefix)
            if relative and not member.is_dir() and not relative.startswith("bin/"):
                target = ffmpeg / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    notices = ROOT / "notices"
    if not notices.is_dir():
        raise RuntimeError("Required third-party notices are missing")
    shutil.copytree(notices, vendor / "notices")
    shutil.copy2(ROOT / "portable-lock.json", vendor / "portable-lock.json")
    manifest_files = {}
    for path in sorted(vendor.rglob("*")):
        if path.is_file():
            manifest_files[path.relative_to(output).as_posix()] = {"size": path.stat().st_size, "sha256": digest(path)}
    write_json(vendor / "runtime-manifest.json", {"schema": "xm5.bundled-runtime.v1", "platform": lock["platform"],
                                                "files": manifest_files, "inputs": items})
    source_inventory = {p.relative_to(source).as_posix(): digest(p) for p in sorted(source.rglob("*"))
                        if p.is_file() and "__pycache__" not in p.parts and p.name != "VALIDATION.json"}
    write_json(output / "BUILD-MANIFEST.json", {"schema": "xm5.portable-build.v1",
                "source_inventory_sha256": hashlib.sha256(json.dumps(source_inventory, sort_keys=True).encode()).hexdigest(),
                "lock_sha256": digest(ROOT / "portable-lock.json"), "files": {
                    p.relative_to(output).as_posix(): {"size": p.stat().st_size, "sha256": digest(p)}
                    for p in sorted(output.rglob("*")) if p.is_file()}})
    print(json.dumps({"output": str(output), "files": len(manifest_files), "bytes": sum(x["size"] for x in manifest_files.values())}))


if __name__ == "__main__":
    main()
