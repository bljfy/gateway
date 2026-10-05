"""Published vectors and real-library negative authentication tests."""

import hashlib
import hmac
from pathlib import Path

import pytest

from gateway.contracts import AuthenticationError, CryptoBackend, GatewayError
from gateway.crypto import GmSSLBackend


def test_sm3_known_answer(backend: GmSSLBackend) -> None:
    assert isinstance(backend, CryptoBackend)
    assert backend.sm3(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    )
    key = b"k" * 32
    assert backend.hmac_sm3(key, b"abc") == hmac.digest(key, b"abc", "sm3")


def test_rfc8998_gcm_vector(backend: GmSSLBackend) -> None:
    # https://www.rfc-editor.org/rfc/rfc8998.html#appendix-A.1
    key = bytes.fromhex("0123456789ABCDEFFEDCBA9876543210")
    nonce = bytes.fromhex("00001234567800000000ABCD")
    aad = bytes.fromhex("FEEDFACEDEADBEEFFEEDFACEDEADBEEFABADDAD2")
    plaintext = bytes.fromhex(
        "AAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBCCCCCCCCCCCCCCCCDDDDDDDDDDDDDDDD"
        "EEEEEEEEEEEEEEEEFFFFFFFFFFFFFFFFEEEEEEEEEEEEEEEEAAAAAAAAAAAAAAAA"
    )
    expected = bytes.fromhex(
        "17F399F08C67D5EE19D0DC9969C4BB7D5FD46FD3756489069157B282BB200735"
        "D82710CA5C22F0CCFA7CBF93D496AC15A56834CBCF98C397B4024A2691233B8D"
        "83DE3541E4C2B58177E065A9BF7B62EC"
    )
    assert backend.seal_sm4_gcm(key, nonce, plaintext, aad) == expected
    assert backend.open_sm4_gcm(key, nonce, expected, aad) == plaintext
    for index in (0, 63, 79):
        damaged = bytearray(expected)
        damaged[index] ^= 1
        with pytest.raises(AuthenticationError):
            backend.open_sm4_gcm(key, nonce, bytes(damaged), aad)
    with pytest.raises(AuthenticationError):
        backend.open_sm4_gcm(key, nonce, expected, aad + b"x")


@pytest.mark.parametrize("size", [0, 1, 1024, 16384, 65536])
def test_record_lengths(backend: GmSSLBackend, size: int) -> None:
    key, nonce = backend.random_bytes(16), backend.random_bytes(12)
    plaintext = b"x" * size
    encrypted = backend.seal_sm4_gcm(key, nonce, plaintext, b"header")
    assert len(encrypted) == size + 16
    assert backend.open_sm4_gcm(key, nonce, encrypted, b"header") == plaintext


def test_sm2_identity_and_72_byte_key_material(backend: GmSSLBackend) -> None:
    private, public = backend.generate_keypair()
    assert backend.public_key(private) == public
    signature = backend.sign_sm2(private, b"transcript", signer_id=b"client")
    assert signature[0] == 0x30  # ASN.1 DER sequence
    assert backend.verify_sm2(public, b"transcript", signature, signer_id=b"client")
    assert not backend.verify_sm2(public, b"transcript", signature, signer_id=b"other")
    assert not backend.verify_sm2(public, b"modified", signature, signer_id=b"client")
    _, other = backend.generate_keypair()
    assert not backend.verify_sm2(other, b"transcript", signature, signer_id=b"client")
    material = backend.random_bytes(72)
    encrypted = backend.encrypt_sm2(public, material)
    assert encrypted[0] == 0x30
    assert backend.decrypt_sm2(private, encrypted) == material
    damaged = bytearray(encrypted)
    damaged[-1] ^= 1
    with pytest.raises(AuthenticationError):
        backend.decrypt_sm2(private, bytes(damaged))


@pytest.mark.parametrize("length", [0, -1, 131073])
def test_random_lengths(backend: GmSSLBackend, length: int) -> None:
    with pytest.raises(ValueError):
        backend.random_bytes(length)


def test_random_failure_is_checked(backend: GmSSLBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend._binding.gmssl, "rand_bytes", lambda *_: -1)
    with pytest.raises(GatewayError, match="random"):
        backend.random_bytes(32)


def test_library_checksum_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.dll"
    path.write_bytes(b"not native code")
    with pytest.raises(GatewayError, match="checksum"):
        GmSSLBackend(path, sha256=hashlib.sha256(b"other").hexdigest())


def test_encrypted_private_key_storage(backend: GmSSLBackend, tmp_path: Path) -> None:
    private, public = backend.generate_keypair()
    protected = backend.export_private_key(private, b"synthetic-test-password")
    assert private not in protected
    path = tmp_path / "identity.der"
    path.write_bytes(protected)
    loaded = backend.import_private_key(path.read_bytes(), b"synthetic-test-password")
    assert loaded == private and backend.public_key(loaded) == public
    for data, password in (
        (protected, b"wrong-test-password"),
        (protected + b"x", b"synthetic-test-password"),
    ):
        with pytest.raises(AuthenticationError):
            backend.import_private_key(data, password)
