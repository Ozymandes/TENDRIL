"""Synthetic APKs for tests: a real zip with a real APK Signing Block.

The block uses the v2 layout (signers -> signed data -> digests,
certificates -> first certificate) with an arbitrary byte string standing
in for the DER certificate, so tendril_apk reads a deterministic "signer"
= sha256(cert). Signatures are empty: tendril_apk identifies the signer,
Android's installer verifies the signature math.
"""
import hashlib
import io
import struct
import zipfile

V2_ID = 0x7109871A
MAGIC = b"APK Sig Block 42"


def _lp(b):
    return struct.pack("<I", len(b)) + b


def make_signed_apk(cert=b"TEST-CERT", payload=b"classes.dex bytes", v_id=V2_ID):
    """Bytes of an APK-shaped zip signed (structurally) by [cert]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("AndroidManifest.xml", b"<manifest/>")
        z.writestr("classes.dex", payload)
    data = buf.getvalue()
    signed_data = _lp(b"") + _lp(_lp(cert)) + _lp(b"")       # digests, certs, attrs
    signer = _lp(signed_data) + _lp(b"") + _lp(b"")          # + signatures, public key
    value = _lp(_lp(signer))
    pair = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", v_id) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + MAGIC
    eocd = data.rfind(b"PK\x05\x06")
    cd_offset = struct.unpack_from("<I", data, eocd + 16)[0]
    out = bytearray(data[:cd_offset] + block + data[cd_offset:])
    eocd += len(block)
    struct.pack_into("<I", out, eocd + 16, cd_offset + len(block))
    return bytes(out)


def signer_of(cert):
    return hashlib.sha256(cert).hexdigest()
