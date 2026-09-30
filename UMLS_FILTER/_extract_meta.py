"""Extract needed RRF files from the .nlm (zip) archives into 2026AA/META/."""
import gzip
import os
import shutil
import zipfile

BASE = r"C:\Inference\umls-2026AA-full\2026AA-full"
OUT = os.path.join(BASE, "2026AA", "META")
os.makedirs(OUT, exist_ok=True)

JOBS = [
    ("2026aa-1-meta.nlm", "2026AA/META/MRSTY.RRF.gz", "MRSTY.RRF"),
    ("2026aa-1-meta.nlm", "2026AA/META/MRSAB.RRF.gz", "MRSAB.RRF"),
]

for archive, member, dest in JOBS:
    target = os.path.join(OUT, dest)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        print(f"[skip] {dest} already present")
        continue
    z = zipfile.ZipFile(os.path.join(BASE, archive))
    with z.open(member) as src, open(target, "wb") as dst:
        shutil.copyfileobj(gzip.GzipFile(fileobj=src), dst)
    print(f"[ok] {dest}: {os.path.getsize(target):,} bytes")

print("META contents:", [(f, os.path.getsize(os.path.join(OUT, f))) for f in os.listdir(OUT)])
