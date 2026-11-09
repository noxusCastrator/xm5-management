"""Create a reviewed media runtime and its complete corresponding-source ZIP.

Run in the Linux build environment. Reuses only checksum-listed Python/Qt
dependency files; discards the old media executables and their runtime trees.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import urllib.request
import zipfile

HERE=Path(__file__).resolve().parent
DEFAULT_BUILD=Path("/var/tmp/xm5-runtime-build-20261108")

def sha(p):
 with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()

def write(p,data):
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")

def ordinary(p):
 if p.is_symlink() or not p.is_file():raise ValueError(f"Not an ordinary file: {p}")

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--runtime-input",type=Path,required=True)
 ap.add_argument("--build-dir",type=Path,default=DEFAULT_BUILD)
 ap.add_argument("--windows-result",type=Path,required=True)
 ap.add_argument("--output",type=Path,required=True)
 ap.add_argument("--source-zip",type=Path,required=True)
 a=ap.parse_args();root=a.build_dir.resolve();sourcezip=a.source_zip.resolve();output=a.output.resolve();base=a.runtime_input.resolve()
 if output.exists() or sourcezip.exists():ap.error("Output/source ZIP exists; choose new paths.")
 lock=json.loads((root/"SOURCES.json").read_text())
 for name,row in lock.items():
  p=root/"sources"/row["file"];ordinary(p)
  if sha(p)!=row["sha256"]:raise ValueError("Source input mismatch: "+name)
 config=(root/"build/mpv/config.h").read_text()
 if not re.search(r"^#define HAVE_GPL 0$",config,re.M):raise ValueError("mpv is not the reviewed GPL-off build")
 result=json.loads(a.windows_result.read_text())
 if not result.get("passed"):raise ValueError("Windows feature verification has not passed")
 executables={"mpv":root/"prefix/bin/mpv.exe","ffmpeg":root/"prefix/bin/ffmpeg.exe"}
 for name,p in executables.items():
  if result["binaries"].get(name+".exe")!=sha(p):raise ValueError("Windows verification does not cover current "+name)
 imports={}
 allowed={"ADVAPI32.DLL","AVRT.DLL","BCRYPT.DLL","DWMAPI.DLL","GDI32.DLL","IMM32.DLL","KERNEL32.DLL","MSVCRT.DLL","NTDLL.DLL","OLE32.DLL","OLEAUT32.DLL","SHCORE.DLL","SHELL32.DLL","SHLWAPI.DLL","USER32.DLL","UXTHEME.DLL","VERSION.DLL","WS2_32.DLL","WINMM.DLL","CRYPT32.DLL","PSAPI.DLL"}
 for name,p in executables.items():
  text=subprocess.check_output(["x86_64-w64-mingw32-objdump","-p",str(p)],text=True)
  imports[name]=re.findall(r"DLL Name:\s*(\S+)",text)
  if any(x.upper() not in allowed and not x.lower().startswith("api-ms-win-") for x in imports[name]):raise ValueError("Unexpected non-system DLL dependency: "+name)
 versions={}
 for name,cmd in {"gcc":["x86_64-w64-mingw32-gcc","--version"],"g++":["x86_64-w64-mingw32-g++","--version"],"meson":["meson","--version"],"ninja":["ninja","--version"],"nasm":["nasm","--version"],"packages":["dpkg-query","-W","gcc-mingw-w64-x86-64-posix","g++-mingw-w64-x86-64-posix","mingw-w64-x86-64-dev","binutils-mingw-w64-x86-64","meson","nasm"]}.items():
  versions[name]=subprocess.check_output(cmd,text=True)
 write(root/"COMPILER-VERSIONS.json",versions);write(root/"DLL-IMPORTS.json",imports)
 exception=root/"GCC-RUNTIME-EXCEPTION.txt"
 if not exception.exists():
  url="https://raw.githubusercontent.com/gcc-mirror/gcc/releases/gcc-13.2.0/COPYING.RUNTIME"
  with urllib.request.urlopen(url,timeout=60) as r:exception.write_bytes(r.read())
 files={}
 for row in lock.values():files["sources/"+row["file"]]=root/"sources"/row["file"]
 for name in ("ffmpeg-devel.asc","ffmpeg-8.0.3.tar.xz.asc"):
  files["sources/"+name]=root/"sources"/name
 for name in ("SOURCES.json","COMPILER-VERSIONS.json","DLL-IMPORTS.json","FFMPEG-CONFIGURE.json","SOURCE-SIGNATURE.txt","mpv-minimal-compatibility.patch","GCC-RUNTIME-EXCEPTION.txt","cross.ini"):
  files[name]=root/name
 for name in ("download_sources.py","build_runtime.py","verify_source_release.py","validate_windows.py","prepare_runtime.py","README.md"):
  files[name]=HERE/name
 files["WINDOWS-RESULT.json"]=a.windows_result
 files["configuration/ffmpeg-config.h"]=root/"build/ffmpeg/config.h"
 files["configuration/ffmpeg-config.mak"]=root/"build/ffmpeg/ffbuild/config.mak"
 files["configuration/mpv-config.h"]=root/"build/mpv/config.h"
 files["configuration/mpv-meson-info.json"]=root/"build/mpv/meson-info/intro-buildoptions.json"
 files["configuration/libplacebo-config.h"]=root/"build/libplacebo/src/include/libplacebo/config.h"
 for component in lock:
  folder=root/"src"/component
  candidates=[*folder.glob("LICENSE*"),*folder.glob("COPYING*"),*folder.glob("Copyright"),*folder.glob("license*")]
  if component=="freetype":candidates += [folder/"docs/FTL.TXT",folder/"docs/LICENSE.TXT"]
  for p in candidates:
   if p.is_file():files["licenses/"+component+"/"+p.name]=p
 for p in (HERE/"test-inputs").rglob("*"):
  if p.is_file():files["test-inputs/"+p.relative_to(HERE/"test-inputs").as_posix()]=p
 inventory={name:{"size":p.stat().st_size,"sha256":sha(p)} for name,p in sorted(files.items())}
 sourcezip.parent.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(sourcezip,"x",compression=zipfile.ZIP_STORED) as z:
  for name,p in sorted(files.items()):z.write(p,"XM5Library-runtime-source-0.2.0/"+name)
  z.writestr("XM5Library-runtime-source-0.2.0/SOURCE-INVENTORY.json",json.dumps(inventory,indent=2)+"\n")
 # Only now admit the clean dependency output. No application/profile files.
 output.mkdir(parents=True)
 old=json.loads((base/"vendor/runtime-manifest.json").read_text(encoding="utf-8-sig"))
 for name,row in old["files"].items():
  rel=PurePosixPath(name)
  if rel.is_absolute() or ".." in rel.parts or rel.parts[0]!="vendor":raise ValueError("Unsafe old runtime manifest")
  if name.startswith(("vendor/mpv/","vendor/ffmpeg/")) or name=="vendor/portable-lock.json":continue
  if name.startswith("vendor/notices/") and (rel.name.startswith(("mpv-","Vulkan-")) or rel.name=="THIRD-PARTY-NOTICES.md"):continue
  p=base/rel;ordinary(p)
  if p.stat().st_size!=row["size"] or sha(p)!=row["sha256"]:raise ValueError("Baseline Python/Qt dependency changed: "+name)
  target=output/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
 for name,p in executables.items():
  dest=output/"vendor"/name/(name+".exe");dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
 notices=output/"vendor/notices/media-source";notices.mkdir(parents=True)
 for name,p in files.items():
  if name.startswith("licenses/"):
   dest=notices/name.removeprefix("licenses/");dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
 shutil.copyfile(exception,notices/exception.name)
 shutil.copyfile(root/"src/mpv/LICENSE.LGPL",output/"vendor/mpv/LICENSE.LGPL")
 shutil.copyfile(root/"src/mpv/Copyright",output/"vendor/mpv/Copyright")
 shutil.copyfile(root/"src/ffmpeg/COPYING.LGPLv2.1",output/"vendor/ffmpeg/LICENSE")
 source_record={"schema":"xm5.media-source-build.v1","source_archive":sourcezip.name,"source_archive_sha256":sha(sourcezip),"sources":lock,"windows_tests":result,"imports":imports}
 write(notices/"SOURCE-BUILD.json",source_record)
 (output/"vendor/notices/THIRD-PARTY-NOTICES.md").write_text("# Bundled runtime notices\n\nPython, PySide6/Qt and Microsoft runtime files retain their original wheel licenses and the Qt/Microsoft notices alongside this file. Qt libraries are dynamically loaded and may be replaced by compatible user-built libraries. Official Qt/PySide source locations remain in Qt-for-Python-licenses.html.\n\nThe mpv and FFmpeg media processes are custom minimal LGPL builds, separate from the companion application. FFmpeg code is LGPLv2.1 or later; mpv was configured with GPL code disabled. Their upstream copyrights and component license texts are retained in media-source/. The exact upstream archives, changes, build controls and tested executable hashes accompany the release as "+sourcezip.name+". Download that archive from the same release page as this portable package: https://github.com/noxusCastrator/XM5_PEQ/releases/tag/v0.2.0 . It contains all non-system media library sources, including libass, libplacebo, Freetype, FriBidi, HarfBuzz, zlib, fast_float and the required Vulkan header declarations. Compiler runtime code retains its original runtime exception and MinGW notices.\n\nThis software uses libraries from the FFmpeg project under LGPLv2.1. FFmpeg source, build configuration and any changes are included in that source archive. You may rebuild, modify and replace either media executable; runtime-manifest.json must then be updated to match the replacement. The original verification manifest is an integrity check, not a restriction on modification or reverse engineering. No blanket license for stock XM5 material is granted by these notices.\n",encoding="utf-8")
 dependency_lock=json.loads((base/"vendor/portable-lock.json").read_text(encoding="utf-8-sig"))
 for name,p in executables.items():dependency_lock[name]={"version":lock[name]["version"],"artifact_kind":"source-built Windows x64 executable","sha256":sha(p),"source_url":lock[name]["url"],"source_archive":sourcezip.name,"source_archive_sha256":sha(sourcezip),"license":"LGPL-2.1-or-later build; see component notices"}
 write(output/"vendor/portable-lock.json",dependency_lock)
 rows={p.relative_to(output).as_posix():{"size":p.stat().st_size,"sha256":sha(p)} for p in sorted((output/"vendor").rglob("*")) if p.is_file()}
 write(output/"vendor/runtime-manifest.json",{"schema":"xm5.bundled-runtime.v1","platform":"Windows 10/11 x86-64","files":rows,"inputs":[dependency_lock["python"],*dependency_lock["wheels"],dependency_lock["mpv"],dependency_lock["ffmpeg"]]})
 review={"schema":"xm5.runtime-source-review.v1","status":"approved","scope":"Technical exact-source inventory/build controls/license preservation and Windows feature checks for the two media executables; no blanket third-party license certification","binaries":{"vendor/"+name+"/"+name+".exe":sha(p) for name,p in executables.items()},"source_artifacts":[{"file":sourcezip.name,"sha256":sha(sourcezip)}],"sources":lock,"no_non_system_media_dlls":True,"windows_features_passed":True}
 write(sourcezip.parent/"RUNTIME-SOURCE-REVIEW.json",review)
 print(json.dumps({"runtime_files":len(rows),"source_files":len(files),"source_zip_bytes":sourcezip.stat().st_size,"source_zip_sha256":sha(sourcezip),"runtime":str(output)}))

if __name__=="__main__":main()
