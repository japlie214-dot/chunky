"""
procedure/build_arm_poppler.py
Cross-compile poppler binaries for ARM64 (aarch64) from an x86_64 host.

Snowflake ARM warehouses need ARM64 poppler binaries. This script
downloads pre-built ARM64 .deb packages from the Debian mirror and
extracts the binaries + shared libs into a directory suitable for
inclusion in utils_bundle.zip.

No root, no Docker, no qemu required — just `curl`/`wget`, `dpkg-deb`,
and `readelf` (all standard on Debian/Ubuntu hosts).

Usage:
    python3 procedure/build_arm_poppler.py --out /tmp/arm_poppler

Produces:
    /tmp/arm_poppler/
    ├── poppler/
    │   ├── bin/
    │   │   ├── pdftoppm
    │   │   ├── pdfinfo
    │   │   └── pdftotext
    │   └── lib/
    │       ├── ld-linux-aarch64.so.1
    │       ├── libc.so.6
    │       ├── libpoppler.so.134
    │       └── ... (all NEEDED shared libs)
    └── MANIFEST.txt
"""
from __future__ import annotations
import argparse
import gzip
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.request import urlopen, urlretrieve

# Debian Bookworm (stable) — has poppler-utils 22.x which matches the
# x86_64 build we shipped before. Mirror is fast and reliable.
MIRROR = "http://deb.debian.org/debian"
DIST = "bookworm"
COMPONENT = "main"
ARCH = "arm64"

# Packages we need to extract from. `poppler-utils` provides the
# binaries; everything else is a shared-lib dependency discovered
# automatically by walking the .deb control data + readelf.
SEED_PACKAGES = ["poppler-utils"]


# -----------------------------------------------------------------------------
# Packages index — download + parse
# -----------------------------------------------------------------------------
def download_packages_index() -> str:
    """Download and decompress the Debian arm64 Packages index."""
    url = f"{MIRROR}/dists/{DIST}/{COMPONENT}/binary-{ARCH}/Packages.gz"
    print(f"[index] Downloading {url} ...")
    with urlopen(url, timeout=60) as resp:
        data = resp.read()
    print(f"[index] {len(data)} bytes compressed")
    return gzip.decompress(data).decode("utf-8", errors="replace")


def parse_packages_index(text: str) -> Dict[str, Dict[str, str]]:
    """Parse a Packages index into a {package_name: {field: value}} map."""
    packages: Dict[str, Dict[str, str]] = {}
    for raw_entry in text.split("\n\n"):
        if not raw_entry.strip():
            continue
        entry: Dict[str, str] = {}
        current_key = None
        for line in raw_entry.split("\n"):
            if line.startswith(" ") or line.startswith("\t"):
                # Continuation of previous field
                if current_key:
                    entry[current_key] += "\n" + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                entry[key] = value.strip()
                current_key = key
        name = entry.get("Package")
        if name:
            # If multiple versions exist, the first one wins (newer is
            # usually listed first in the index).
            if name not in packages:
                packages[name] = entry
    return packages


# -----------------------------------------------------------------------------
# Dependency resolution
# -----------------------------------------------------------------------------
def parse_depends(depends_str: str) -> List[str]:
    """Parse a Depends field into a list of package names (no version constraints)."""
    if not depends_str:
        return []
    names: List[str] = []
    for clause in depends_str.split(","):
        clause = clause.strip()
        if not clause:
            continue
        # A clause may have alternatives separated by `|`
        # Take the first alternative.
        first = clause.split("|")[0].strip()
        # Strip version constraint: "libc6 (>= 2.34)" → "libc6"
        name = first.split(" ")[0].split("(")[0].strip()
        if name:
            names.append(name)
    return names


def resolve_dependencies(packages: Dict[str, Dict[str, str]],
                         seeds: List[str]) -> List[str]:
    """Breadth-first dependency resolution starting from `seeds`."""
    visited: Set[str] = set()
    queue: List[str] = list(seeds)
    order: List[str] = []
    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        if name not in packages:
            # Virtual package or unknown — skip silently
            continue
        order.append(name)
        deps = parse_depends(packages[name].get("Depends", ""))
        for d in deps:
            if d not in visited:
                queue.append(d)
    return order


# -----------------------------------------------------------------------------
# Download + extract .deb files
# -----------------------------------------------------------------------------
def download_deb(entry: Dict[str, str], dest: Path) -> Path:
    """Download a .deb file described by its Packages-index entry."""
    rel = entry["Filename"]
    url = f"{MIRROR}/{rel}"
    out = dest / Path(rel).name
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[deb]   {out.name}")
    urlretrieve(url, out)
    return out


def extract_deb(deb_path: Path, dest: Path) -> None:
    """Extract a .deb file's data.tar.* into `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["dpkg-deb", "-x", str(deb_path), str(dest)],
        check=True,
        capture_output=True,
    )


# -----------------------------------------------------------------------------
# Find binaries + resolve shared libs (cross-arch via readelf)
# -----------------------------------------------------------------------------
def find_binaries(extract_root: Path, names: List[str]) -> Dict[str, Path]:
    """Find each binary by name under the extracted tree."""
    found: Dict[str, Path] = {}
    for n in names:
        for cand in extract_root.rglob(n):
            # Skip non-files and non-executable files
            if not cand.is_file():
                continue
            if not os.access(cand, os.X_OK):
                continue
            # Verify it's an ELF
            try:
                with open(cand, "rb") as f:
                    if f.read(4) != b"\x7fELF":
                        continue
            except Exception:
                continue
            found[n] = cand
            break
    return found


def readelf_needed(elf_path: Path) -> List[str]:
    """Use `readelf -d` to extract NEEDED shared-library entries from an ELF file.

    readelf works on any ELF, regardless of host architecture — it just
    parses the file, it doesn't execute it. This is what makes cross-arch
    bundling possible without qemu.
    """
    try:
        out = subprocess.run(
            ["readelf", "-d", str(elf_path)],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return []
    needed: List[str] = []
    for line in out.splitlines():
        line = line.strip()
        if "(NEEDED)" not in line:
            continue
        # Line looks like: " 0x0000000000000001 (NEEDED) Shared library: [libc.so.6]"
        if "[" in line and "]" in line:
            lib = line.split("[", 1)[1].split("]", 1)[0].strip()
            if lib:
                needed.append(lib)
    return needed


def find_shared_lib(extract_root: Path, lib_name: str) -> Optional[Path]:
    """Find a shared library by name (or by soname prefix) under the extract root."""
    # Try exact match first
    for cand in extract_root.rglob(lib_name):
        if cand.is_file():
            try:
                with open(cand, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        return cand
            except Exception:
                continue
    # Try as a glob (e.g. libpoppler.so.134 might match libpoppler.so.134.0.0)
    if ".so." in lib_name:
        prefix = lib_name.split(".so.")[0] + ".so."
        for cand in extract_root.rglob(prefix + "*"):
            if cand.is_file():
                try:
                    with open(cand, "rb") as f:
                        if f.read(4) == b"\x7fELF":
                            return cand
                except Exception:
                    continue
    return None


def find_dynamic_linker(extract_root: Path) -> Optional[Path]:
    """Find the ARM64 dynamic linker (ld-linux-aarch64.so.1)."""
    # Common locations
    candidates = [
        "lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
        "usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
        "lib/ld-linux-aarch64.so.1",
    ]
    for c in candidates:
        p = extract_root / c
        if p.is_file():
            return p
    # Fallback: rglob
    for cand in extract_root.rglob("ld-linux-aarch64.so.1"):
        if cand.is_file():
            return cand
    return None


def resolve_lib_tree(extract_root: Path, seeds: List[Path]) -> Tuple[Dict[str, Path], List[str]]:
    """
    Walk NEEDED entries starting from `seeds` (binaries or libs) and
    collect every shared library needed at runtime.

    Returns (resolved_libs, missing_libs) where resolved_libs is a
    {soname: path} map.
    """
    resolved: Dict[str, Path] = {}
    missing: List[str] = []
    visited: Set[str] = set()
    queue: List[Path] = list(seeds)

    while queue:
        current = queue.pop(0)
        for needed in readelf_needed(current):
            if needed in visited:
                continue
            visited.add(needed)
            if needed in resolved:
                continue
            lib_path = find_shared_lib(extract_root, needed)
            if lib_path is None:
                missing.append(needed)
                continue
            resolved[needed] = lib_path
            queue.append(lib_path)

    return resolved, missing


# -----------------------------------------------------------------------------
# Bundle assembly
# -----------------------------------------------------------------------------
def assemble_bundle(extract_root: Path, bin_names: List[str],
                    out_dir: Path) -> Dict[str, List[str]]:
    """
    Copy the poppler binaries + all transitive shared libs + the ARM64
    dynamic linker into `out_dir` laid out as:
        out_dir/poppler/bin/<binary>
        out_dir/poppler/lib/<soname>
    """
    out_bin = out_dir / "poppler" / "bin"
    out_lib = out_dir / "poppler" / "lib"
    out_bin.mkdir(parents=True, exist_ok=True)
    out_lib.mkdir(parents=True, exist_ok=True)

    bin_paths = find_binaries(extract_root, bin_names)
    missing_bins = [b for b in bin_names if b not in bin_paths]
    if missing_bins:
        raise SystemExit(f"Missing binaries: {missing_bins}")

    # Copy binaries
    for name, src in bin_paths.items():
        dst = out_bin / name
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        print(f"[bin]   {dst.relative_to(out_dir)}")

    # Resolve shared lib tree starting from the binaries
    resolved, missing_libs = resolve_lib_tree(extract_root, list(bin_paths.values()))
    if missing_libs:
        print(f"[warn]  Missing shared libs (will fail at runtime): {missing_libs}",
              file=sys.stderr)

    # Copy libs
    for soname, src in sorted(resolved.items()):
        dst = out_lib / Path(src).name
        if dst.exists():
            continue
        shutil.copy2(src, dst)
        print(f"[lib]   {dst.relative_to(out_dir)}")

    # Copy the ARM64 dynamic linker
    ld = find_dynamic_linker(extract_root)
    if ld is None:
        raise SystemExit("Missing ARM64 dynamic linker (ld-linux-aarch64.so.1)")
    dst = out_lib / ld.name
    shutil.copy2(ld, dst)
    print(f"[ld]    {dst.relative_to(out_dir)}")

    return {
        "binaries": sorted(bin_paths.keys()),
        "libraries": sorted(resolved.keys()),
        "missing_libs": missing_libs,
        "dynamic_linker": ld.name,
    }


def write_manifest(out_dir: Path, manifest: Dict[str, List[str]]) -> None:
    """Write a MANIFEST.txt documenting what's in the bundle."""
    p = out_dir / "MANIFEST.txt"
    lines = [
        "ARM64 poppler bundle",
        "=====================",
        f"Source: Debian {DIST} {COMPONENT} {ARCH}",
        f"Mirror: {MIRROR}",
        "",
        "Binaries:",
    ]
    for b in manifest["binaries"]:
        lines.append(f"  - {b}")
    lines.extend(["", "Shared libraries:"])
    for l in manifest["libraries"]:
        lines.append(f"  - {l}")
    lines.extend(["", "Dynamic linker:", f"  - {manifest['dynamic_linker']}"])
    if manifest["missing_libs"]:
        lines.extend(["", "MISSING shared libraries (will fail at runtime):"])
        for l in manifest["missing_libs"]:
            lines.append(f"  - {l}")
    p.write_text("\n".join(lines) + "\n")
    print(f"[done]  Manifest written to {p}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True,
                        help="Output directory (will be created)")
    parser.add_argument("--deb-cache",
                        help="Directory to cache downloaded .deb files (default: temp)")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    deb_cache = Path(args.deb_cache) if args.deb_cache else \
        Path(tempfile.mkdtemp(prefix="arm_poppler_debs_"))

    # 1. Download + parse Packages index
    packages = parse_packages_index(download_packages_index())
    print(f"[index] {len(packages)} packages available for {ARCH}")

    # 2. Resolve dependency tree
    order = resolve_dependencies(packages, SEED_PACKAGES)
    print(f"[deps]  Resolved {len(order)} packages: {order}")

    # 3. Download + extract every .deb
    extract_root = Path(tempfile.mkdtemp(prefix="arm_poppler_extract_"))
    for name in order:
        entry = packages[name]
        deb_path = download_deb(entry, deb_cache)
        extract_deb(deb_path, extract_root)
        print(f"[xtr]   {name}")

    # 4. Assemble the bundle
    bin_names = ["pdftoppm", "pdfinfo", "pdftotext"]
    manifest = assemble_bundle(extract_root, bin_names, out_dir)

    # 5. Write manifest
    write_manifest(out_dir, manifest)

    # 6. Verify ELF architecture
    print()
    print("[verify] ELF header of pdftoppm:")
    subprocess.run(["file", str(out_dir / "poppler" / "bin" / "pdftoppm")])
    print("[verify] ELF header of libc.so.6:")
    subprocess.run(["file", str(out_dir / "poppler" / "lib" / "libc.so.6")])

    print()
    print(f"✅ ARM64 poppler bundle assembled at {out_dir}")
    print(f"   {sum(1 for _ in out_dir.rglob('*') if _.is_file())} files, "
          f"{sum(p.stat().st_size for p in out_dir.rglob('*') if p.is_file()) // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
