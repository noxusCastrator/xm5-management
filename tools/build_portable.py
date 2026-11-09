"""Assemble a clean portable application without copying user data.

An existing hash-locked runtime can be reused for local validation. Public media
runtime distribution requires an explicit corresponding-source review manifest.
No installer, global Python, installed media application or device is used.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

REPO=Path(__file__).resolve().parents[1]
BAD={".git","__pycache__","vendor",".venv","data","cache","runtime","logs","build","out","tests"}
BAD_NAMES={"config.json","connection.json","credentials.json","library.sqlite","library.db","BASELINE.json","CHANGES.json"}

def sha(p):
    with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()

def regular(p):
    if p.is_symlink() or not p.is_file() or getattr(p.lstat(),"st_file_attributes",0)&0x400:
        raise ValueError(f"Refuse nonordinary input: {p}")

def runtime_files(root):
    mf=root/"vendor/runtime-manifest.json";regular(mf)
    data=json.loads(mf.read_text(encoding="utf-8-sig"));rows={}
    if data.get("schema")!="xm5.bundled-runtime.v1":raise ValueError("Unknown runtime manifest")
    for name,record in data["files"].items():
        rel=PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0]!="vendor":raise ValueError("Unsafe runtime manifest path")
        p=root/rel;regular(p)
        if p.stat().st_size!=record["size"] or sha(p)!=record["sha256"]:raise ValueError(f"Runtime mismatch: {name}")
        rows[rel.as_posix()]=p
    rows["vendor/runtime-manifest.json"]=mf
    return rows

def public_review(path,files):
    if not path:raise ValueError("Public builds require --source-review; existing GPL runtime review is incomplete.")
    review=json.loads(path.read_text(encoding="utf-8-sig"))
    if review.get("schema")!="xm5.runtime-source-review.v1" or review.get("status")!="approved" or not review.get("source_artifacts"):
        raise ValueError("Corresponding-source review is not approved")
    for name in ("vendor/mpv/mpv.exe","vendor/ffmpeg/ffmpeg.exe"):
        if review.get("binaries",{}).get(name)!=sha(files[name]):raise ValueError(f"Review does not cover exact binary: {name}")
    for row in review["source_artifacts"]:
        p=path.parent/row["file"];regular(p)
        if sha(p)!=row["sha256"]:raise ValueError("Corresponding-source archive mismatch")
    return review

def zip_tree(source,destination):
    if destination.exists():raise ValueError(f"ZIP already exists: {destination}")
    with zipfile.ZipFile(destination,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=8) as z:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                info=zipfile.ZipInfo(source.name+"/"+p.relative_to(source).as_posix(),date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
                with p.open("rb") as inp,z.open(info,"w",force_zip64=True) as out:shutil.copyfileobj(inp,out)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,default=REPO/"desktop")
    ap.add_argument("--runtime-dir",type=Path,help="Existing application root with vendor/runtime-manifest.json")
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--zip",type=Path)
    ap.add_argument("--public",action="store_true")
    ap.add_argument("--source-review",type=Path)
    ap.add_argument("--offline",action="store_true")
    a=ap.parse_args();source=a.source.resolve();output=a.output.resolve()
    if output.exists():ap.error("Output exists; choose a new directory.")
    if source.is_relative_to(output) or output.is_relative_to(source):ap.error("Source and output must be separate.")
    for name in ("bootstrap.py","XM5Library.cmd","xm5lib/ui/assets/NOTICE.md","xm5lib/core/bundled_runtime.py"):
        if not (source/name).is_file():ap.error(f"Complete source missing {name}")
    source_files={}
    for p in sorted(source.rglob("*")):
        rel=p.relative_to(source)
        if not p.is_file() or any(x in BAD for x in rel.parts) or p.name in BAD_NAMES or "BASELINE" in p.name or p.suffix in {".pyc",".sqlite",".sqlite3",".db",".log",".key",".pem",".peace",".dll",".exe"}:continue
        regular(p);source_files[rel.as_posix()]=p
    runtime=runtime_files(a.runtime_dir.resolve()) if a.runtime_dir else None
    review=None
    if a.public:
        if runtime is None:ap.error("Public build first requires an approved existing runtime.")
        review=public_review(a.source_review,runtime)
    if runtime is None:
        # A public download lock retains exact archive hashes and publisher URLs.
        cmd=[sys.executable,str(REPO/"tools/portable/assemble.py"),"--source",str(source),"--output",str(output)]
        if a.offline:cmd.append("--offline")
        subprocess.run(cmd,check=True)
    else:
        output.mkdir(parents=True)
        for name,p in {**source_files,**runtime}.items():
            dest=output/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
    if not review:
        (output/"LOCAL-RUNTIME-REVIEW.txt").write_text("Local validation package. The existing bundled media runtime has not completed corresponding-source review. Do not upload this ZIP as a public binary distribution.\n",encoding="utf-8")
    manifest={"schema":"xm5.portable-build.v2","version":"0.2.0","public_runtime_source_review":"approved" if review else "pending","source_review_sha256":sha(a.source_review) if review else None,"files":{}}
    for p in sorted(output.rglob("*")):
        if p.is_file() and p.name!="BUILD-MANIFEST.json":manifest["files"][p.relative_to(output).as_posix()]={"size":p.stat().st_size,"sha256":sha(p)}
    (output/"BUILD-MANIFEST.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    if a.zip:zip_tree(output,a.zip.resolve())
    print(json.dumps({"files":len(manifest["files"]),"public_runtime_source_review":manifest["public_runtime_source_review"],"zip_sha256":sha(a.zip.resolve()) if a.zip else None}))

if __name__=="__main__":main()
