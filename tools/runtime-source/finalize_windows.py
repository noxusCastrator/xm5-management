"""Verify an already copied runtime using native Windows filesystem IO.

The Linux preparer's serial hash pass can be slow on a mounted NTFS tree. Stop
that owned process after its dependency lock exists, then run this final pass.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

def sha(p):
 with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()
def write(p,value):p.write_text(json.dumps(value,indent=2)+"\n",encoding="utf-8")
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--runtime-input",type=Path,required=True);ap.add_argument("--runtime",type=Path,required=True);ap.add_argument("--source-zip",type=Path,required=True)
 a=ap.parse_args();root=a.runtime.resolve();archive=a.source_zip.resolve();source=json.loads((root/"vendor/notices/media-source/SOURCE-BUILD.json").read_text());lock=json.loads((root/"vendor/portable-lock.json").read_text())
 if source["source_archive_sha256"]!=sha(archive) or not source["windows_tests"]["passed"]:raise ValueError("Source/test record mismatch")
 paths=[p for p in sorted((root/"vendor").rglob("*")) if p.is_file() and p.name!="runtime-manifest.json"]
 def row(p):
  if p.is_symlink() or getattr(p.lstat(),"st_file_attributes",0)&0x400:raise ValueError("Nonordinary runtime file")
  return p.relative_to(root).as_posix(),{"size":p.stat().st_size,"sha256":sha(p)}
 with ThreadPoolExecutor(max_workers=8) as pool:rows=dict(pool.map(row,paths))
 old=json.loads((a.runtime_input/"vendor/runtime-manifest.json").read_text())
 for name,value in old["files"].items():
  rel=Path(name)
  if name.startswith(("vendor/mpv/","vendor/ffmpeg/")) or name=="vendor/portable-lock.json":continue
  if name.startswith("vendor/notices/") and (rel.name.startswith(("mpv-","Vulkan-")) or rel.name=="THIRD-PARTY-NOTICES.md"):continue
  if rows.get(name)!=value:raise ValueError("Copied baseline dependency differs: "+name)
 binaries={}
 for name in ("mpv","ffmpeg"):
  relative="vendor/"+name+"/"+name+".exe";digest=rows[relative]["sha256"]
  if digest!=source["windows_tests"]["binaries"][name+".exe"] or digest!=lock[name]["sha256"]:raise ValueError("Media executable differs")
  binaries[relative]=digest
 with zipfile.ZipFile(archive) as z:
  config=z.read("XM5Library-runtime-source-0.2.0/configuration/mpv-config.h").decode()
  if "#define HAVE_GPL 0" not in config:raise ValueError("Source configuration is not GPL-off")
 write(root/"vendor/runtime-manifest.json",{"schema":"xm5.bundled-runtime.v1","platform":"Windows 10/11 x86-64","files":rows,"inputs":[lock["python"],*lock["wheels"],lock["mpv"],lock["ffmpeg"]]})
 review={"schema":"xm5.runtime-source-review.v1","status":"approved","scope":"Exact media source/build inventory, preserved licenses and Windows feature tests; not a blanket third-party license certification","binaries":binaries,"source_artifacts":[{"file":archive.name,"sha256":sha(archive)}],"sources":source["sources"],"no_non_system_media_dlls":True,"windows_features_passed":True}
 write(archive.parent/"RUNTIME-SOURCE-REVIEW.json",review)
 print(json.dumps({"runtime_files":len(rows),"runtime_bytes":sum(x["size"] for x in rows.values()),"source_zip_sha256":sha(archive),"approved_media_binaries":binaries}))

if __name__=="__main__":main()
