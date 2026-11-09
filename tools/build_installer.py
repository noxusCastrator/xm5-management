#!/usr/bin/env python3
"""Wrap one manifest-verified XM5 Studio payload in an offline current-user setup.

The compiler is an explicit local input; this script downloads or installs nothing.
All installer staging stays in a new directory, separate from the reviewed payload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "XM5Studio:167851CF-0701-4B99-8FE4-AC2EB066C7D8"
REQUIRED = {"bootstrap.py", "XM5Studio.exe", "vendor/python/pythonw.exe", "vendor/mpv/mpv.exe",
            "vendor/ffmpeg/ffmpeg.exe", "xm5lib/ui/assets/studio/app.ico"}
PRIVATE = {"data", "cache", "runtime", ".local", "__pycache__"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Unsafe payload path")
    for part in name.split("/"):
        if (not part or part in {".", ".."} or ".." in part or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.split(".", 1)[0].upper() in RESERVED):
            raise ValueError(f"Unsafe payload path: {name}")
    return PurePosixPath(name)


def ordinary(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError(f"Payload contains a link: {item}")
    if not path.is_file():
        raise ValueError(f"Missing regular payload file: {path}")


def verified_payload(payload, allow_unreviewed=False):
    ordinary(payload / "BUILD-MANIFEST.json")
    manifest = json.loads((payload / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != "xm5.portable-build.v2" or manifest.get("version") != "0.2.2"
            or not isinstance(manifest.get("files"), dict)):
        raise ValueError("Expected an XM5 Studio 0.2.2 portable-build.v2 manifest")
    if not allow_unreviewed and manifest.get("public_runtime_source_review") != "approved":
        raise ValueError("Installer input needs the approved corresponding-source runtime review")
    names, folded = {}, set()
    for name, row in manifest["files"].items():
        rel = relative(name)
        if name.casefold() in folded:
            raise ValueError("Case-colliding payload paths")
        folded.add(name.casefold())
        if (any(p.casefold() in PRIVATE for p in rel.parts) or rel.name.casefold().startswith("config.json")
                or rel.name.casefold().startswith(".env") or rel.suffix.casefold() in {".sqlite", ".sqlite3", ".db", ".log", ".pyc", ".pyo", ".key", ".pem"}):
            raise ValueError(f"Private or generated payload file: {name}")
        file = payload / rel
        ordinary(file)
        if file.stat().st_size != row["size"] or sha(file) != row["sha256"]:
            raise ValueError(f"Payload mismatch: {name}")
        names[name] = file
    missing = REQUIRED - names.keys()
    if missing:
        raise ValueError(f"Incomplete application payload: {sorted(missing)}")
    actual = set()
    for file in payload.rglob("*"):
        if file.is_symlink() or (getattr(file.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Unexpected linked payload input")
        if file.is_file():
            actual.add(file.relative_to(payload).as_posix())
    if actual != set(names) | {"BUILD-MANIFEST.json"}:
        raise ValueError("Payload contains files outside its reviewed manifest")
    return manifest, names


def prepare(payload, stage, *, allow_unreviewed=False):
    payload, stage = Path(payload).resolve(), Path(stage).resolve()
    if stage.exists() or payload.is_relative_to(stage) or stage.is_relative_to(payload):
        raise ValueError("Choose a new staging directory separate from the payload")
    manifest, files = verified_payload(payload, allow_unreviewed)
    stage.mkdir(parents=True)
    app = stage / "payload"
    app.mkdir()
    for name, file in files.items():
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    (app / "distribution.json").write_text('{"schema":"xm5.distribution.v1","mode":"installed"}\n', encoding="utf-8", newline="\n")
    (app / ".xm5-studio-installation").write_text(IDENTITY + "\n", encoding="utf-8", newline="\n")
    (app / "docs").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "docs/INSTALLATION.md", app / "docs/INSTALLATION.md")
    installed = dict(manifest)
    installed["schema"] = "xm5.installed-build.v1"
    installed["portable_manifest_sha256"] = sha(payload / "BUILD-MANIFEST.json")
    installed["files"] = {p.relative_to(app).as_posix(): {"size": p.stat().st_size, "sha256": sha(p)}
                          for p in sorted(app.rglob("*")) if p.is_file()}
    (app / "BUILD-MANIFEST.json").write_text(json.dumps(installed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    paths = [p for p in sorted(app.rglob("*")) if p.is_file()]
    (app / "OWNED-FILES.sha256").write_text("".join(sha(p) + "\t" + p.relative_to(app).as_posix() + "\n" for p in paths), encoding="utf-8", newline="\n")
    paths.append(app / "OWNED-FILES.sha256")
    include = stage / "payload-files.iss"
    lines = []
    for file in paths:
        name = file.relative_to(app)
        dest = "{app}" if name.parent == Path(".") else "{app}\\" + str(name.parent).replace("/", "\\")
        # No wildcards: uninstall owns exactly these names and no other files.
        source = str(file).replace("{", "{{")
        lines.append(f'Source: "{source}"; DestDir: "{dest}"; Flags: ignoreversion\n')
    include.write_text("".join(lines), encoding="utf-8", newline="\n")
    return app, include, installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--stage-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--iscc", required=True, type=Path)
    parser.add_argument("--chinese-messages", required=True, type=Path)
    parser.add_argument("--local-allow-unreviewed", action="store_true", help="local fixtures only; output is not publishable")
    args = parser.parse_args()
    output, compiler, language = args.output.resolve(), args.iscc.resolve(), args.chinese_messages.resolve()
    if output.exists() or output.suffix.casefold() != ".exe":
        parser.error("Choose a new .exe output")
    ordinary(compiler)
    ordinary(language)
    app, include, manifest = prepare(args.payload, args.stage_dir, allow_unreviewed=args.local_allow_unreviewed)
    output.parent.mkdir(parents=True, exist_ok=True)
    script = ROOT / "tools/installer/XM5Studio.iss"
    command = [str(compiler), "/Qp", "/DPayloadDir=" + str(app), "/DPayloadFiles=" + str(include),
               "/DChineseMessages=" + str(language), "/DOutputPath=" + str(output.parent),
               "/DOutputName=" + output.stem, str(script)]
    subprocess.run(command, check=True)
    receipt = {"schema": "xm5.installer-build.v1", "version": "0.2.2", "app_id": IDENTITY,
               "compiler_sha256": sha(compiler), "language_sha256": sha(language),
               "script_sha256": sha(script), "portable_manifest_sha256": manifest["portable_manifest_sha256"],
               "installed_manifest_sha256": sha(app / "BUILD-MANIFEST.json"),
               "owned_manifest_sha256": sha(app / "OWNED-FILES.sha256"), "setup_sha256": sha(output),
               "public_runtime_source_review": manifest.get("public_runtime_source_review"), "signed": False}
    output.with_suffix(".build.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
