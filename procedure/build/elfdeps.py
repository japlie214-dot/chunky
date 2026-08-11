"""Small ELF parser used to reject incorrectly-architected bundle members."""
from __future__ import annotations
import struct

EM = {0x3E: "x86_64", 0xB7: "arm64"}

def elf_arch(path) -> str | None:
    with open(path, "rb") as stream:
        head = stream.read(20)
    if head[:4] != b"\x7fELF" or len(head) < 20:
        return None
    endian = "<" if head[5] == 1 else ">"
    return EM.get(struct.unpack_from(endian + "H", head, 18)[0])

def needed(path) -> list[str]:
    return []
