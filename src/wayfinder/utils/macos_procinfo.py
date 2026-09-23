"""Kernel-backed socket ownership checks on macOS (libproc).

Linux proves "this listening socket belongs to the child we spawned" by
matching /proc/net/tcp inodes against /proc/<pid>/fd. macOS has no /proc, so
without this the check could never succeed and the resident llama-server was
refused on every start. libproc answers the same question straight from the
kernel: enumerate the child's descriptors and look for a TCP socket in LISTEN
state on the port. ~0.1 ms per call, so it is cheap enough to re-prove per
request like the Linux path does.

Struct offsets come from <sys/proc_info.h> (stable since 10.5; the same
layout lsof reads). ``tests/test_macos_procinfo.py`` checks them against a
live listening socket in the test process, so a layout change fails loudly.
"""

from __future__ import annotations

import ctypes
import socket
import sys

_PROC_PIDLISTFDS = 1
_PROC_PIDFDSOCKETINFO = 3
_PROX_FDTYPE_SOCKET = 2
_SOCKINFO_TCP = 2
_TSI_S_LISTEN = 1

_PROC_FDINFO_SIZE = 8           # struct proc_fdinfo {int32 fd; uint32 type}
_SOCKET_FDINFO_SIZE = 792       # struct socket_fdinfo
_OFF_SOI_KIND = 256             # psi.soi_kind
_OFF_LOCAL_PORT = 268           # psi.soi_proto.pri_tcp.tcpsi_ini.insi_lport
_OFF_TCP_STATE = 344            # psi.soi_proto.pri_tcp.tcpsi_state
_MAX_FDS = 4096

_libproc = None


def _lib():
    global _libproc
    if _libproc is None:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidfdinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ]
        lib.proc_pidfdinfo.restype = ctypes.c_int
        _libproc = lib
    return _libproc


def _socket_fds(lib, pid: int) -> list[int] | None:
    buf = ctypes.create_string_buffer(_PROC_FDINFO_SIZE * _MAX_FDS)
    used = lib.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, len(buf))
    if used <= 0:
        return None
    fds = []
    raw = buf.raw[:used]
    for offset in range(0, used - _PROC_FDINFO_SIZE + 1, _PROC_FDINFO_SIZE):
        fd = int.from_bytes(raw[offset:offset + 4], sys.byteorder, signed=True)
        kind = int.from_bytes(raw[offset + 4:offset + 8], sys.byteorder)
        if kind == _PROX_FDTYPE_SOCKET:
            fds.append(fd)
    return fds


def pid_listens_on_tcp(pid: int, port: int) -> bool | None:
    """True if ``pid`` holds a TCP socket LISTENing on ``port``.

    False when the process is readable and has no such socket; None when the
    kernel could not be asked (not macOS, process gone, libproc unavailable) —
    callers treat None as "not proven", exactly like the Linux /proc path.
    """
    if sys.platform != "darwin" or not pid or not port:
        return None
    try:
        lib = _lib()
        fds = _socket_fds(lib, int(pid))
        if fds is None:
            return None
        info = ctypes.create_string_buffer(_SOCKET_FDINFO_SIZE)
        for fd in fds:
            got = lib.proc_pidfdinfo(int(pid), fd, _PROC_PIDFDSOCKETINFO, info, _SOCKET_FDINFO_SIZE)
            if got < _SOCKET_FDINFO_SIZE:
                continue
            raw = info.raw
            kind = int.from_bytes(raw[_OFF_SOI_KIND:_OFF_SOI_KIND + 4], sys.byteorder, signed=True)
            if kind != _SOCKINFO_TCP:
                continue
            state = int.from_bytes(raw[_OFF_TCP_STATE:_OFF_TCP_STATE + 4], sys.byteorder, signed=True)
            if state != _TSI_S_LISTEN:
                continue
            lport = int.from_bytes(raw[_OFF_LOCAL_PORT:_OFF_LOCAL_PORT + 4], sys.byteorder, signed=True)
            if socket.ntohs(lport & 0xFFFF) == int(port):
                return True
        return False
    except Exception:
        return None
