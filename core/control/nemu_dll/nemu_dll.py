"""Validated ctypes bindings for MuMu's external-renderer IPC library.

The checked-in project supports the 64-bit MuMu 12 library only.  On Windows
x64, ``__cdecl`` and ``__stdcall`` use the same platform ABI; the loader is
therefore deliberately fixed to :class:`ctypes.CDLL`.  A 32-bit image is
rejected because its calling convention cannot be proven from the undecorated
exports available on this installation.
"""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass
from pathlib import Path


IMAGE_FILE_MACHINE_AMD64 = 0x8664
PE32_PLUS_MAGIC = 0x20B
NEMU_CALLING_CONVENTION = "WINDOWS_X64_PLATFORM_ABI"


class NemuNativeSignatureError(OSError):
    """The native image cannot be called with the audited signature contract."""


@dataclass(frozen=True, slots=True)
class NemuNativeSignatureAudit:
    path: str
    machine: int
    optional_magic: int
    calling_convention: str
    loader: str
    signature_gate: str


def _read_pe_identity(path: str | os.PathLike[str]) -> tuple[int, int]:
    with Path(path).open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise NemuNativeSignatureError("nemu_dll_not_pe")
        stream.seek(0x3C)
        pe_offset_raw = stream.read(4)
        if len(pe_offset_raw) != 4:
            raise NemuNativeSignatureError("nemu_dll_truncated_dos_header")
        pe_offset = struct.unpack("<I", pe_offset_raw)[0]
        stream.seek(pe_offset)
        if stream.read(4) != b"PE\x00\x00":
            raise NemuNativeSignatureError("nemu_dll_invalid_pe_signature")
        coff = stream.read(20)
        if len(coff) != 20:
            raise NemuNativeSignatureError("nemu_dll_truncated_coff_header")
        machine = struct.unpack_from("<H", coff)[0]
        optional_magic_raw = stream.read(2)
        if len(optional_magic_raw) != 2:
            raise NemuNativeSignatureError("nemu_dll_missing_optional_header")
        return machine, struct.unpack("<H", optional_magic_raw)[0]


def audit_native_signature(path: str | os.PathLike[str]) -> NemuNativeSignatureAudit:
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("文件不存在: " + path)
    machine, optional_magic = _read_pe_identity(path)
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise NemuNativeSignatureError("nemu_python_process_not_x64")
    if machine != IMAGE_FILE_MACHINE_AMD64 or optional_magic != PE32_PLUS_MAGIC:
        raise NemuNativeSignatureError(
            f"nemu_native_signature_untrusted_architecture:{machine:#x}:{optional_magic:#x}"
        )
    return NemuNativeSignatureAudit(
        path=os.path.abspath(path),
        machine=machine,
        optional_magic=optional_magic,
        calling_convention=NEMU_CALLING_CONVENTION,
        loader="ctypes.CDLL",
        signature_gate="PASS",
    )


def _bind(library: ctypes.CDLL, name: str, argtypes: list[object], restype: object):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise NemuNativeSignatureError(f"nemu_export_missing:{name}") from error
    function.argtypes = argtypes
    function.restype = restype
    return function


def bind_exports(library: ctypes.CDLL) -> ctypes.CDLL:
    """Attach the audited ABI to every export used by the Python backend."""

    c_int = ctypes.c_int
    _bind(library, "nemu_connect", [ctypes.c_wchar_p, c_int], c_int)
    _bind(library, "nemu_disconnect", [c_int], None)
    _bind(library, "nemu_get_display_id", [c_int, ctypes.c_char_p, c_int], c_int)
    _bind(
        library,
        "nemu_capture_display",
        [c_int, c_int, c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(ctypes.c_ubyte)],
        c_int,
    )
    _bind(library, "nemu_input_text", [c_int, c_int, ctypes.c_char_p], c_int)
    _bind(library, "nemu_input_event_touch_down", [c_int, c_int, c_int, c_int], c_int)
    _bind(library, "nemu_input_event_touch_up", [c_int, c_int], c_int)
    _bind(library, "nemu_input_event_finger_touch_down", [c_int, c_int, c_int, c_int, c_int], c_int)
    _bind(library, "nemu_input_event_finger_touch_up", [c_int, c_int, c_int], c_int)
    _bind(library, "nemu_input_event_key_down", [c_int, c_int, c_int], c_int)
    _bind(library, "nemu_input_event_key_up", [c_int, c_int, c_int], c_int)
    return library


def init(path: str) -> ctypes.CDLL:
    audit_native_signature(path)
    return bind_exports(ctypes.CDLL(path))
