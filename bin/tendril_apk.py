#!/usr/bin/env python3
"""tendril_apk — read the signer of an APK, stdlib only (v0.2.1).

The host must verify the ACTUAL signing certificate inside a TENDRIL Link
APK — not just trust the manifest's `signer_sha256` field — before staging
it for phones. This reads the APK Signature Scheme v3 (else v2) block:

  EOCD -> central directory offset -> the 24 bytes before it end in
  [uint64 block size]["APK Sig Block 42"] -> id/value pairs ->
  0xf05368c0 (v3) or 0x7109871a (v2) -> signers -> signed data ->
  certificates -> the first (signing) certificate, DER.

Returns the lowercase SHA-256 hex of each signer's certificate — the same
value `apksigner verify --print-certs` prints as "certificate SHA-256
digest". This identifies the signer; it does NOT re-verify the signature
math (Android's installer does that, and refuses tampered APKs). Combined
with the pinned production fingerprint and the release sha256, a staged
APK is exactly the release the key holder signed.
"""
import hashlib
import struct
import sys

EOCD_SIG = b"PK\x05\x06"
MAGIC = b"APK Sig Block 42"
V2_ID = 0x7109871A
V3_ID = 0xF05368C0


class ApkError(ValueError):
    """Not an APK with a v2/v3 signing block we can read."""


def _lp(buf, off):
    """Length-prefixed (uint32 LE) slice at off -> (bytes, next offset)."""
    if off + 4 > len(buf):
        raise ApkError("truncated length prefix")
    n = struct.unpack_from("<I", buf, off)[0]
    end = off + 4 + n
    if end > len(buf):
        raise ApkError("truncated length-prefixed value")
    return buf[off + 4:end], end


def _signing_block(data):
    tail = data[-(22 + 0xFFFF):]
    i = tail.rfind(EOCD_SIG)
    if i < 0 or i + 22 > len(tail):
        raise ApkError("no end-of-central-directory record")
    cd_offset = struct.unpack_from("<I", tail, i + 16)[0]
    if cd_offset < 24 or cd_offset > len(data):
        raise ApkError("bad central directory offset")
    footer = data[cd_offset - 24:cd_offset]
    if footer[8:] != MAGIC:
        raise ApkError("no APK Signing Block (v1-only or not an APK)")
    size = struct.unpack_from("<Q", footer, 0)[0]
    start = cd_offset - size - 8
    if start < 0 or struct.unpack_from("<Q", data, start)[0] != size:
        raise ApkError("inconsistent APK Signing Block size")
    pairs = data[start + 8:cd_offset - 24]
    out, off = {}, 0
    while off + 12 <= len(pairs):
        plen = struct.unpack_from("<Q", pairs, off)[0]
        if plen < 4 or off + 8 + plen > len(pairs):
            raise ApkError("bad id-value pair")
        pid = struct.unpack_from("<I", pairs, off + 8)[0]
        out[pid] = pairs[off + 12:off + 8 + plen]
        off += 8 + plen
    return out


def signer_sha256s(path):
    """Lowercase SHA-256 hex of the first certificate of every signer, from
    the v3 block when present, else v2. Raises ApkError."""
    with open(path, "rb") as f:
        data = f.read()
    blocks = _signing_block(data)
    value = blocks.get(V3_ID) or blocks.get(V2_ID)
    if value is None:
        raise ApkError("no v2/v3 signature scheme block")
    signers, _ = _lp(value, 0)
    out, off = [], 0
    while off < len(signers):
        signer, off = _lp(signers, off)
        signed_data, _ = _lp(signer, 0)
        _digests, nxt = _lp(signed_data, 0)
        certs, _ = _lp(signed_data, nxt)
        first, _ = _lp(certs, 0)
        out.append(hashlib.sha256(first).hexdigest())
    if not out:
        raise ApkError("signing block lists no signers")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: tendril_apk.py <file.apk>", file=sys.stderr)
        sys.exit(2)
    try:
        for digest in signer_sha256s(sys.argv[1]):
            print(digest)
    except (OSError, ApkError) as exc:
        print("tendril_apk: %s" % exc, file=sys.stderr)
        sys.exit(1)
