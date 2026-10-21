"""A seccomp-bpf deny list a process installs on ITSELF, needing no privileges: no new processes,
network, mounts, links, mode or owner changes, or signals to others. Paths are left to Landlock."""

from __future__ import annotations

import ctypes
import struct

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
EPERM = 1

BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_RET_K = 0x06

# offsetof(struct seccomp_data, nr) and .arch
_NR, _ARCH = 0, 4

# arch/x86/entry/syscalls/syscall_64.tbl
DENIED_X86_64 = {
    56: "clone", 435: "clone3", 57: "fork", 58: "vfork", 59: "execve", 322: "execveat",
    41: "socket", 42: "connect", 49: "bind", 50: "listen", 43: "accept", 288: "accept4",
    101: "ptrace", 165: "mount", 166: "umount2", 161: "chroot", 155: "pivot_root", 133: "mknod",
    259: "mknodat", 86: "link", 265: "linkat", 88: "symlink", 266: "symlinkat", 90: "chmod",
    268: "fchmodat", 92: "chown", 260: "fchownat", 105: "setuid", 106: "setgid", 308: "setns",
    272: "unshare", 313: "finit_module", 175: "init_module", 319: "memfd_create",
    447: "memfd_secret", 425: "io_uring_setup", 426: "io_uring_enter", 427: "io_uring_register",
    303: "name_to_handle_at", 304: "open_by_handle_at", 134: "uselib", 188: "setxattr",
    189: "lsetxattr", 190: "fsetxattr", 197: "removexattr", 198: "lremovexattr",
    199: "fremovexattr", 132: "utime", 235: "utimes", 261: "futimesat", 280: "utimensat",
    310: "process_vm_readv", 311: "process_vm_writev", 321: "bpf", 323: "userfaultfd",
    248: "add_key", 249: "request_key", 250: "keyctl", 62: "kill", 200: "tkill", 234: "tgkill",
    424: "pidfd_send_signal", 434: "pidfd_open", 438: "pidfd_getfd", 300: "fanotify_init",
    428: "open_tree", 429: "move_mount", 430: "fsopen", 431: "fsconfig", 432: "fsmount",
    433: "fspick", 442: "mount_setattr",
}
DENIED_AARCH64 = {
    220: "clone", 435: "clone3", 221: "execve", 281: "execveat", 198: "socket", 203: "connect",
    200: "bind", 201: "listen", 202: "accept", 242: "accept4", 117: "ptrace", 40: "mount",
    39: "umount2", 51: "chroot", 41: "pivot_root", 37: "linkat", 36: "symlinkat", 53: "fchmodat",
    54: "fchownat", 146: "setuid", 144: "setgid", 268: "setns", 97: "unshare", 273: "finit_module",
    105: "init_module", 279: "memfd_create", 447: "memfd_secret", 33: "mknodat",
    425: "io_uring_setup", 426: "io_uring_enter", 427: "io_uring_register",
    264: "name_to_handle_at", 265: "open_by_handle_at", 5: "setxattr", 6: "lsetxattr",
    7: "fsetxattr", 14: "removexattr", 15: "lremovexattr", 16: "fremovexattr", 88: "utimensat",
    270: "process_vm_readv", 271: "process_vm_writev", 280: "bpf", 282: "userfaultfd",
    217: "add_key", 218: "request_key", 219: "keyctl", 129: "kill", 130: "tkill", 131: "tgkill",
    424: "pidfd_send_signal", 434: "pidfd_open", 438: "pidfd_getfd", 262: "fanotify_init",
    428: "open_tree", 429: "move_mount", 430: "fsopen", 431: "fsconfig", 432: "fsmount",
    433: "fspick", 442: "mount_setattr",
}


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _program(arch: int, denied) -> bytes:
    """Refuse anything from another architecture outright — a 32-bit call enters with different
    numbers, and a filter that let it through would be checking the wrong table."""
    out = [_stmt(BPF_LD_W_ABS, _ARCH),
           _jump(BPF_JMP_JEQ_K, arch, 1, 0),
           _stmt(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM),
           _stmt(BPF_LD_W_ABS, _NR)]
    for nr in denied:
        out += [_jump(BPF_JMP_JEQ_K, nr, 0, 1), _stmt(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM)]
    out.append(_stmt(BPF_RET_K, SECCOMP_RET_ALLOW))
    return b"".join(out)


def install() -> None:
    """Narrow this process for good. Raises when the kernel refuses, which the caller must treat
    as a hard failure: a program that runs unconfined is not what was asked for."""
    import platform
    machine = platform.machine()
    if machine in ("x86_64", "AMD64"):
        blob = _program(AUDIT_ARCH_X86_64, DENIED_X86_64)
    elif machine in ("aarch64", "arm64"):
        blob = _program(AUDIT_ARCH_AARCH64, DENIED_AARCH64)
    else:
        raise RuntimeError(f"no seccomp filter for {machine}")

    buf = ctypes.create_string_buffer(blob, len(blob))
    fprog = struct.pack("HxxxxxxP", len(blob) // 8, ctypes.addressof(buf))
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.c_char_p(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP failed")
    # The buffer must outlive the call: the kernel copies it, but a GC before prctl returns would
    # hand it a freed address.
    del buf, fprog
