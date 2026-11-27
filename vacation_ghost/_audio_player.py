"""Run the Linux ALSA player with the same lifetime as its controller."""

import ctypes
import os
import signal
import sys


def main() -> int:
    expected_parent = int(sys.argv[1])
    player = sys.argv[2]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    # Linux retains the parent-death signal when exec'ing an ordinary program.
    pr_set_pdeathsig = 1
    if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # Our parent may have died before Python reached prctl.
    if os.getppid() != expected_parent:
        return 1
    os.execv(player, [player, "--quiet"])


if __name__ == "__main__":
    raise SystemExit(main())
