"""Build a versioned, deterministic utility bundle on Windows."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from debfetch import extract_data_tar
from elfdeps import elf_arch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "out"

def build_bundle(output_dir: Path = OUT) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((ROOT / "utils").glob("*.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    version = "2.0.0"
    target = output_dir / f"utils_bundle_v{version}+{digest.hexdigest()[:8]}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, f"chunky_utils/{path.name}")
        manifests = {}
        for arch in ("arm64", "amd64"):
            tree = _download_poppler(arch)
            archive_root = "x86_64" if arch == "amd64" else "arm64"
            manifests[archive_root] = _add_tree(
                archive, tree, f"poppler_bundle/{archive_root}/poppler")
        _add_pdf2image(archive)
        archive.writestr("MANIFEST.json", json.dumps({"version": version,
                      "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "files": [p.name for p in files], "poppler": manifests}, indent=2))
    return target

def _packages(arch):
    url = f"https://deb.debian.org/debian/dists/bookworm/main/binary-{arch}/Packages.gz"
    raw = gzip.decompress(urllib.request.urlopen(url, timeout=60).read()).decode()
    result = {}
    for stanza in raw.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if fields.get("Package"):
            result[fields["Package"]] = fields
    return result

def _download_poppler(arch):
    """Resolve Debian package dependencies, then extract with debfetch."""
    packages = _packages(arch)
    wanted, queue = set(), ["poppler-utils"]
    while queue:
        name = queue.pop(0)
        if name in wanted or name not in packages:
            continue
        wanted.add(name)
        deps = packages[name].get("Depends", "")
        for dep in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]*", deps):
            if dep in packages and dep not in wanted:
                queue.append(dep)
    work = Path(tempfile.mkdtemp(prefix=f"chunky_poppler_{arch}_"))
    extract = work / "root"
    cache = work / "debs"
    cache.mkdir(parents=True)
    for name in sorted(wanted):
        entry = packages[name]
        deb = cache / f"{name}.deb"
        urllib.request.urlretrieve("https://deb.debian.org/debian/" + entry["Filename"], deb)
        extract_data_tar(deb.read_bytes(), extract)
    out = work / "poppler"
    (out / "bin").mkdir(parents=True)
    (out / "lib").mkdir(parents=True)
    for binary in ("pdftoppm", "pdfinfo", "pdftotext"):
        matches = list(extract.rglob(binary))
        if not matches:
            raise RuntimeError(f"Debian {arch} bundle did not contain {binary}")
        shutil.copy2(matches[0], out / "bin" / binary)
    for path in extract.rglob("*"):
        if path.is_file() and ("/lib/" in path.as_posix() or path.name.startswith("ld-linux")):
            shutil.copy2(path, out / "lib" / path.name)
    expected = "arm64" if arch == "arm64" else "x86_64"
    for binary in (out / "bin").iterdir():
        actual = elf_arch(binary)
        if actual != expected:
            raise RuntimeError(f"{binary} is {actual}, expected {expected}")
    return out

def _add_tree(archive, tree, prefix):
    names = []
    for path in sorted(tree.rglob("*")):
        if path.is_file():
            rel = path.relative_to(tree).as_posix()
            archive.write(path, f"{prefix}/{rel}")
            names.append(rel)
    return names

def _add_pdf2image(archive):
    work = Path(tempfile.mkdtemp(prefix="chunky_pdf2image_"))
    try:
        subprocess.run([os.fspath(__import__('sys').executable), "-m", "pip", "install",
                        "--quiet", "--no-deps", "--target", os.fspath(work), "pdf2image"], check=True)
        for path in sorted((work / "pdf2image").rglob("*.py")):
            archive.write(path, f"pdf2image/{path.name}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    if args.clean:
        for path in OUT.glob("utils_bundle_*.zip"):
            path.unlink()
    print(build_bundle())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
