"""Python front-end for the PIR hot loops.

Loads pir_kernels.c (compiled on first import into ./_build, never shipped as a
binary because -march=native output is machine specific). If no C compiler is
available, or PIR_NO_C=1 is set, pure-numpy fallbacks with identical results are
used; they are 5-50x slower but correct.
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "pir_kernels.c")
_lib = None
BUILD_NOTE = "numpy fallback"


def _build_dir():
    for d in (os.path.join(_HERE, "_build"), os.path.join(tempfile.gettempdir(), "pir_build")):
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            pass
    return None


def _load():
    global _lib, BUILD_NOTE
    if os.environ.get("PIR_NO_C") == "1":
        BUILD_NOTE = "numpy fallback (PIR_NO_C=1)"
        return
    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    out_dir = _build_dir()
    if not cc or not out_dir:
        BUILD_NOTE = "numpy fallback (no C compiler / build dir)"
        return
    src = open(_SRC, "rb").read()
    tag = hashlib.sha256(src + sys.platform.encode()).hexdigest()[:12]
    so = os.path.join(out_dir, f"libpirkernels-{tag}.so")
    if not os.path.exists(so):
        built = False
        for flags in (["-O3", "-march=native"], ["-O3"]):
            tmp = so + f".{os.getpid()}.tmp"
            try:
                subprocess.run([cc, *flags, "-shared", "-fPIC", "-o", tmp, _SRC],
                               check=True, capture_output=True)
                os.replace(tmp, so)
                built = True
                break
            except (subprocess.CalledProcessError, OSError):
                continue
        if not built:
            BUILD_NOTE = "numpy fallback (compile failed)"
            return
    lib = ctypes.CDLL(so)
    u8 = np.ctypeslib.ndpointer(np.uint8, flags="C_CONTIGUOUS")
    u32 = np.ctypeslib.ndpointer(np.uint32, flags="C_CONTIGUOUS")
    u64 = np.ctypeslib.ndpointer(np.uint64, flags="C_CONTIGUOUS")
    sz = ctypes.c_size_t
    lib.xor_select_batch.argtypes = [u64, sz, sz, u8, sz, sz, u64]
    lib.lwe_answer_batch.argtypes = [u8, sz, sz, u32, sz, u32]
    lib.u32_matvec_batch.argtypes = [u32, sz, sz, u32, sz, u32]
    for f in (lib.xor_select_batch, lib.lwe_answer_batch, lib.u32_matvec_batch):
        f.restype = None
    _lib = lib
    BUILD_NOTE = f"C kernels ({os.path.basename(cc)})"


_load()
HAVE_C = _lib is not None


def xor_select_batch(db_words, masks):
    """db_words: (N, W) uint64; masks: (B, ceil(N/8)) uint8 -> (B, W) uint64."""
    n, w = db_words.shape
    b, mb = masks.shape
    assert mb >= (n + 7) // 8
    db_words = np.ascontiguousarray(db_words)
    masks = np.ascontiguousarray(masks)
    out = np.empty((b, w), dtype=np.uint64)
    if HAVE_C:
        _lib.xor_select_batch(db_words, n, w, masks, mb, b, out)
        return out
    out[:] = 0
    for q in range(b):
        sel = np.unpackbits(masks[q], bitorder="little")[:n].astype(bool)
        idx = np.flatnonzero(sel)
        for s in range(0, len(idx), 256):  # bounded temporary
            out[q] ^= np.bitwise_xor.reduce(db_words[idx[s:s + 256]], axis=0)
    return out


def lwe_answer_batch(D, Q):
    """D: (l, m) uint8; Q: (B, m) uint32 -> (B, l) uint32 = (D @ Q^T)^T mod 2^32."""
    l, m = D.shape
    b = Q.shape[0]
    D = np.ascontiguousarray(D)
    Q = np.ascontiguousarray(Q)
    out = np.empty((b, l), dtype=np.uint32)
    if HAVE_C:
        _lib.lwe_answer_batch(D, l, m, Q, b, out)
        return out
    QT = np.ascontiguousarray(Q.T)
    for r in range(0, l, 128):
        out[:, r:r + 128] = (D[r:r + 128].astype(np.uint32) @ QT).T
    return out


def u32_matvec_batch(M, V):
    """M: (rows, n) uint32; V: (B, n) uint32 -> (B, rows) uint32, all mod 2^32."""
    rows, n = M.shape
    b = V.shape[0]
    M = np.ascontiguousarray(M)
    V = np.ascontiguousarray(V)
    out = np.empty((b, rows), dtype=np.uint32)
    if HAVE_C:
        _lib.u32_matvec_batch(M, rows, n, V, b, out)
        return out
    VT = np.ascontiguousarray(V.T)
    for r in range(0, rows, 512):
        out[:, r:r + 512] = (M[r:r + 512] @ VT).T
    return out


if __name__ == "__main__":
    print("kernels:", BUILD_NOTE)
