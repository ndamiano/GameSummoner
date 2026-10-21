"""A Landlock ruleset a process puts on ITSELF, needing no privileges: read and change the game
folder, only read Python's own library, nothing else. A refusal is a hard failure."""

from __future__ import annotations

import ctypes
import os
import struct
import sysconfig

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

WRITE_FILE, READ_FILE, READ_DIR, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5
MAKE_DIR, MAKE_REG, REFER, TRUNCATE, IOCTL_DEV = 1 << 7, 1 << 8, 1 << 13, 1 << 14, 1 << 15
EVERY_FS_V1 = (1 << 13) - 1
GAME = WRITE_FILE | READ_FILE | READ_DIR | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG
LIBRARY = READ_FILE | READ_DIR

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def library_paths() -> set:
    paths = sysconfig.get_paths()
    return {paths[k] for k in ("stdlib", "platstdlib", "purelib", "platlib") if os.path.isdir(paths[k])}


def _ruleset(abi: int) -> tuple:
    """The rights this kernel's ABI can refuse, network and scope included, and the game folder's share."""
    handled, game = EVERY_FS_V1, GAME
    if abi >= 2:
        handled, game = handled | REFER, game | REFER
    if abi >= 3:
        handled, game = handled | TRUNCATE, game | TRUNCATE
    if abi >= 5:
        handled |= IOCTL_DEV
    attr = struct.pack("QQQ", handled, 0b11 if abi >= 4 else 0, 0b11 if abi >= 6 else 0)
    size = 8 if abi < 4 else (16 if abi < 6 else 24)
    return attr[:size], game


def confine(root: str) -> None:
    abi = _libc.syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0),
                        ctypes.c_uint32(CREATE_RULESET_VERSION))
    if abi < 1:
        raise OSError(ctypes.get_errno(), "landlock unavailable")
    attr, game = _ruleset(abi)
    buf = ctypes.create_string_buffer(attr, len(attr))
    fd = _libc.syscall(SYS_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for path, access in [(root, game)] + [(p, LIBRARY) for p in library_paths()]:
            dirfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = ctypes.create_string_buffer(struct.pack("=Qi", access, dirfd), 12)
                if _libc.syscall(SYS_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(RULE_PATH_BENEATH),
                                 rule, ctypes.c_uint32(0)) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(dirfd)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
        if _libc.syscall(SYS_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(fd)
