"""
Simple robust invisible signature embedding and detection using block-wise DCT on the luminance channel.

Approach (lightweight, no external deps beyond numpy/opencv):
- Convert RGB -> YCrCb; operate on Y (luma)
- Split into 8x8 blocks; compute DCT per block
- Embed bits by enforcing an inequality between two mid-frequency coefficients (c1 vs c2)
  - If bit=1: make c1 > c2 + delta
  - If bit=0: make c2 > c1 + delta
- Repeat payload across many blocks; on detection, do majority vote per bit index
- This is reasonably robust to moderate JPEG compression, light scaling/cropping

NOTE: This is a pragmatic baseline. It is not state-of-the-art, but sufficient for MVP.
"""
from __future__ import annotations
from typing import Iterable, List, Optional
import os
import struct
import hashlib
import numpy as np
import cv2
from PIL import Image

# Indices of mid-frequency DCT coefficients inside 8x8 (excluding DC at (0,0))
# Choose positions that are relatively stable under JPEG but not too low-freq
C1 = (2, 3)
C2 = (3, 2)
BLOCK = 8
MAGIC = b"PMK1"  # 4 bytes header for Photomark v1
PAYLOAD_LEN = 32  # fixed-size payload we embed/detect


def _to_y(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("RGB"))  # HxWx3, RGB
    ycrcb = cv2.cvtColor(arr, cv2.COLOR_RGB2YCrCb)
    y = ycrcb[:, :, 0].astype(np.float32)
    return y


def _from_y(y: np.ndarray, base: Image.Image) -> Image.Image:
    arr = np.array(base.convert("RGB"))
    ycrcb = cv2.cvtColor(arr, cv2.COLOR_RGB2YCrCb)
    y8 = np.clip(y, 0, 255).astype(np.uint8)
    ycrcb[:, :, 0] = y8
    out_rgb = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB)
    return Image.fromarray(out_rgb, mode="RGB")


def _block_view(a: np.ndarray, block: int = BLOCK) -> np.ndarray:
    H, W = a.shape
    Hc = (H // block) * block
    Wc = (W // block) * block
    a = a[:Hc, :Wc]
    new_shape = (Hc // block, Wc // block, block, block)
    new_strides = (
        a.strides[0] * block,
        a.strides[1] * block,
        a.strides[0],
        a.strides[1],
    )
    return np.lib.stride_tricks.as_strided(a, shape=new_shape, strides=new_strides)


def _idct_2d(block: np.ndarray) -> np.ndarray:
    return cv2.idct(block)


def _dct_2d(block: np.ndarray) -> np.ndarray:
    return cv2.dct(block)


def _payload_to_bits(payload: bytes) -> List[int]:
    bits: List[int] = []
    for b in payload:
        for i in range(8):
            bits.append((b >> (7 - i)) & 1)
    return bits


def _bits_to_bytes(bits: Iterable[int]) -> bytes:
    b = 0
    out = bytearray()
    for i, bit in enumerate(bits):
        b = (b << 1) | (bit & 1)
        if (i % 8) == 7:
            out.append(b)
            b = 0
    if len(bits) % 8 != 0:
        out.append(b << (8 - (len(bits) % 8)))
    return bytes(out)


def build_payload_for_uid(uid: str) -> bytes:
    """Construct a 32-byte payload with header and uid hash.
    Layout:
    - 4 bytes: MAGIC 'PMK1'
    - 8 bytes: truncated SHA256(uid)
    - 8 bytes: unix timestamp (uint64, big-endian)
    - 12 bytes: random
    """
    ts = int.from_bytes(os.urandom(8), 'big')  # random 64-bit as privacy-preserving surrogate for timestamp
    uid_hash = hashlib.sha256(uid.encode('utf-8')).digest()[:8]
    rnd = os.urandom(12)
    payload = MAGIC + uid_hash + struct.pack('>Q', ts) + rnd
    assert len(payload) == PAYLOAD_LEN
    return payload


def payload_matches_uid(payload: bytes, uid: str) -> bool:
    if len(payload) < 12:
        return False
    if payload[:4] != MAGIC:
        return False
    uid_hash = hashlib.sha256(uid.encode('utf-8')).digest()[:8]
    return payload[4:12] == uid_hash


def embed_signature(img: Image.Image, payload: bytes, strength: float = 6.0, repeat: int | None = None) -> Image.Image:
    """
    Embed payload bits into image. Returns a new PIL Image (RGB).
    - strength: delta added to enforce inequalities (higher = more robust, more visible risk)
    - repeat: number of times to repeat payload; default auto from image size
    """
    y = _to_y(img).astype(np.float32)
    blocks = _block_view(y, BLOCK)  # [bh, bw, 8, 8]
    bh, bw = blocks.shape[:2]

    bits = _payload_to_bits(payload)
    if not bits:
        return img.copy().convert("RGB")

    total_blocks = bh * bw
    # Default repetition: aim ~1/2 of blocks used, at least 8x repetition
    if repeat is None:
        repeat = max(8, total_blocks // max(1, len(bits) * 2))
    sequence = bits * repeat

    idx = 0
    for i in range(bh):
        for j in range(bw):
            if idx >= len(sequence):
                break
            b = sequence[idx]
            idx += 1
            block = blocks[i, j].astype(np.float32) - 128.0
            d = _dct_2d(block)
            c1 = d[C1]
            c2 = d[C2]
            # Enforce inequality with margin
            delta = float(strength)
            if b == 1:
                if c1 <= c2 + delta:
                    adjust = (c2 + delta) - c1
                    d[C1] = c1 + adjust
            else:
                if c2 <= c1 + delta:
                    adjust = (c1 + delta) - c2
                    d[C2] = c2 + adjust
            rec = _idct_2d(d) + 128.0
            blocks[i, j, :, :] = rec
        if idx >= len(sequence):
            break

    # Reconstruct Y plane from modified blocks
    y_mod = y.copy()
    y_mod[:bh * BLOCK, :bw * BLOCK] = blocks.reshape(bh * BLOCK, bw * BLOCK)

    out = _from_y(y_mod, img)
    return out


def detect_signature(img: Image.Image, payload_len_bytes: int = PAYLOAD_LEN, repeat_hint: Optional[int] = None) -> Optional[bytes]:
    """
    Attempt to detect and reconstruct payload of given byte length.
    Returns bytes if majority-vote per bit succeeds beyond a threshold; else None.
    """
    y = _to_y(img).astype(np.float32)
    blocks = _block_view(y, BLOCK)
    bh, bw = blocks.shape[:2]
    total_blocks = bh * bw

    bits_len = max(1, payload_len_bytes * 8)
    # Estimate how many repetitions are available
    reps = total_blocks // bits_len
    if reps <= 0:
        return None

    # For each bit position, count votes over blocks assigned to that position
    votes = np.zeros(bits_len, dtype=np.int32)

    idx = 0
    for i in range(bh):
        for j in range(bw):
            pos = idx % bits_len
            blk = blocks[i, j].astype(np.float32) - 128.0
            d = _dct_2d(blk)
            c1 = d[C1]
            c2 = d[C2]
            bit = 1 if (c1 - c2) > 0 else 0
            votes[pos] += 1 if bit == 1 else -1
            idx += 1

    # Decide bits by sign of votes. Also compute confidence.
    out_bits = (votes > 0).astype(np.uint8).tolist()
    conf = float(np.mean(np.clip(np.abs(votes) / max(1, reps), 0, 1)))

    if conf < 0.15:
        return None

    return _bits_to_bytes(out_bits)[:payload_len_bytes]