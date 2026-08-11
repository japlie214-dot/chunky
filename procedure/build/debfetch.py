"""Stdlib-only Debian .deb ar/tar extraction."""
from __future__ import annotations
import io
import tarfile

AR_MAGIC = b"!<arch>\n"

def ar_members(blob: bytes):
    if blob[:8] != AR_MAGIC:
        raise ValueError("not an ar archive")
    offset = 8
    while offset + 60 <= len(blob):
        header = blob[offset:offset + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        yield name, blob[start:start + size]
        offset = start + size + size % 2

def extract_data_tar(deb_bytes: bytes, dest) -> None:
    for name, payload in ar_members(deb_bytes):
        if not name.startswith("data.tar"):
            continue
        mode = {"data.tar.xz": "r:xz", "data.tar.gz": "r:gz", "data.tar": "r:"}.get(name)
        if mode is None:
            raise RuntimeError(f"unsupported compression: {name}")
        with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as archive:
            # Debian packages contain harmless absolute font/config symlinks.
            # Skip those links while retaining regular files and safe links.
            def safe_member(member, root):
                if member.issym() or member.islnk():
                    if member.linkname.startswith("/") or ".." in member.linkname.split("/"):
                        return None
                return member
            try:
                archive.extractall(dest, filter=safe_member)
            except TypeError:  # Python 3.11 compatibility
                for member in archive.getmembers():
                    if safe_member(member, dest) is not None:
                        archive.extract(member, dest)
        return
    raise RuntimeError("no data.tar.* member in .deb")
