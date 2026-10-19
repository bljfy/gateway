"""RFC 5869 HKDF using the backend's HMAC-SM3, with explicit domain separation."""

from gateway.contracts import CryptoBackend


def hkdf(backend: CryptoBackend, secret: bytes, salt: bytes, info: bytes, size: int) -> bytes:
    """Extract then expand; callers encode the protocol and purpose in info."""
    if not 1 <= size <= 255 * 32:
        raise ValueError("invalid HKDF output length")
    if len(salt) > 64:
        raise ValueError("HKDF salt exceeds backend HMAC key limit")
    # HMAC zero-pads its key to the hash block size; pre-padding short salts
    # is equivalent and satisfies gmssl-python's 16-byte minimum key length.
    prk = backend.hmac_sm3((salt or bytes(32)).ljust(16, b"\x00"), secret)
    previous = b""
    output = bytearray()
    try:
        for counter in range(1, (size + 31) // 32 + 1):
            previous = backend.hmac_sm3(prk, previous + info + bytes([counter]))
            output.extend(previous)
        return bytes(output[:size])
    finally:
        output[:] = bytes(len(output))
