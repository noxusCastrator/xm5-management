"""Verify the official FFmpeg release signature in an isolated public keyring."""
import os
from pathlib import Path
import subprocess
import urllib.request

ROOT=Path("/var/tmp/xm5-runtime-build-20261108")
home=ROOT/"gnupg-public";home.mkdir(exist_ok=True);home.chmod(0o700)
for name,url in (("ffmpeg-devel.asc","https://ffmpeg.org/ffmpeg-devel.asc"),("ffmpeg-8.0.3.tar.xz.asc","https://ffmpeg.org/releases/ffmpeg-8.0.3.tar.xz.asc")):
 p=ROOT/"sources"/name
 if not p.exists():
  with urllib.request.urlopen(url,timeout=60) as r:p.write_bytes(r.read())
base=["gpg","--batch","--homedir",str(home)]
subprocess.run([*base,"--import",str(ROOT/"sources/ffmpeg-devel.asc")],check=True,capture_output=True)
p=subprocess.run([*base,"--status-fd=1","--verify",str(ROOT/"sources/ffmpeg-8.0.3.tar.xz.asc"),str(ROOT/"sources/ffmpeg-8.0.3.tar.xz")],check=True,capture_output=True,text=True)
known={"FCF986EA15E6E293A5644F10B4322F04D67658D8","DD1EC9E8DE085C629B3E1846B18E8928B3948D64"}
if not any(line.startswith("[GNUPG:] VALIDSIG ") and any(key in line for key in known) for line in p.stdout.splitlines()):raise ValueError("Unrecognized FFmpeg release signing key")
(ROOT/"SOURCE-SIGNATURE.txt").write_text(p.stdout+p.stderr)
print(p.stdout)
