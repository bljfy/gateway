"""Checked adapter for gmssl-python 2.2.2 and GmSSL 3.1.1.

The official ctypes binding is untyped. Any is confined to native contexts in
this file; the rest of the security core uses the typed CryptoBackend contract.
"""

import ctypes
import ctypes.util
import hashlib
import importlib
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Any, cast

from gateway.contracts import MAX_RECORD_PLAINTEXT, AuthenticationError, GatewayError


class GmSSLBackend:
    """将固定版本 GmSSL 的原生操作适配到安全会话使用的密码接口。"""

    def __init__(self, library_path: Path, *, sha256: str) -> None:
        # 校验文件后还要核对实际加载的库，防止搜索路径选中另一份同名库。
        path = library_path.resolve(strict=True)
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise GatewayError("native library checksum mismatch")
        if importlib.metadata.version("gmssl-python") != "2.2.2":
            raise GatewayError("unsupported GmSSL binding")
        found = ctypes.util.find_library("gmssl")
        if found is None:
            raise GatewayError("native library must be selected by the configured loader path")
        if sys.platform == "win32" and Path(found).resolve() != path:
            raise GatewayError("native library loader path mismatch")
        if sys.platform == "linux":
            search = os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
            if not search or Path(search[0]).resolve() != path.parent:
                raise GatewayError("native library directory must lead LD_LIBRARY_PATH")
        elif sys.platform != "win32":
            raise GatewayError("unsupported native platform")
        self._binding: Any = importlib.import_module("gmssl")
        if sys.platform == "win32" and Path(self._binding.gmssl._name).resolve() != path:
            raise GatewayError("a different native library is already loaded")
        if sys.platform == "linux":
            mapped = {
                Path(line.split(maxsplit=5)[5].strip()).resolve()
                for line in Path("/proc/self/maps").read_text().splitlines()
                if len(line.split(maxsplit=5)) == 6 and "libgmssl.so" in line
            }
            if mapped != {path}:
                raise GatewayError("loaded native library provenance mismatch")
        if self._binding.gmssl_library_version_num() != 30101:
            raise GatewayError("unsupported GmSSL native ABI")

    def random_bytes(self, length: int) -> bytes:
        """调用原生安全随机源生成有界随机材料。"""
        if not 1 <= length <= 131_072:
            raise ValueError("invalid random buffer length")
        output = ctypes.create_string_buffer(length)
        if self._binding.gmssl.rand_bytes(output, ctypes.c_size_t(length)) != 1:
            raise GatewayError("secure random source failed")
        return output.raw

    def sm3(self, message: bytes) -> bytes:
        """计算握手消息等输入的 SM3 摘要。"""
        ctx = self._binding.Sm3()
        ctx.update(message)
        return cast(bytes, ctx.digest())

    def _key(self, material: bytes, private: bool) -> Any:
        """把私钥标量或未压缩公钥点装入原生上下文，并检查输入格式。"""
        ctx = self._binding.Sm2Key()
        if private:
            if len(material) != 32:
                raise ValueError("SM2 private key must contain 32 bytes")
            order = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
            # SM2 签名需要 (1 + d) 可逆，因此私钥不能取曲线阶减一。
            if not 1 <= int.from_bytes(material) < order - 1:
                raise AuthenticationError("invalid SM2 key")
            result = self._binding.gmssl.sm2_key_set_private_key(ctypes.byref(ctx), material)
        else:
            if len(material) != 65 or material[0] != 4:
                raise ValueError("SM2 public key must be an uncompressed point")
            result = self._binding.gmssl.sm2_point_from_octets(
                ctypes.byref(ctx.public_key), material, ctypes.c_size_t(len(material))
            )
        if result != 1:
            raise AuthenticationError("invalid SM2 key")
        ctx._has_public_key = True
        ctx._has_private_key = private
        return ctx

    def generate_keypair(self) -> tuple[bytes, bytes]:
        """返回 32 字节私钥和 0x04 前缀的 65 字节未压缩公钥。"""
        ctx = self._binding.Sm2Key()
        ctx.generate_key()
        return bytes(ctx.private_key), b"\x04" + bytes(ctx.public_key.x) + bytes(ctx.public_key.y)

    def public_key(self, private_key: bytes) -> bytes:
        """由私钥恢复对应的未压缩公钥，供本地身份配置使用。"""
        ctx = self._key(private_key, True)
        return b"\x04" + bytes(ctx.public_key.x) + bytes(ctx.public_key.y)

    def export_private_key(self, private_key: bytes, password: bytes) -> bytes:
        """Return native PKCS#8 EncryptedPrivateKeyInfo DER, never an unencrypted file."""
        if not 16 <= len(password) <= 1024 or b"\x00" in password:
            raise ValueError("invalid key password")
        ctx = self._key(private_key, True)
        buffer = ctypes.create_string_buffer(1024)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        length = ctypes.c_size_t()
        try:
            result = self._binding.gmssl.sm2_private_key_info_encrypt_to_der(
                ctypes.byref(ctx),
                ctypes.c_char_p(password),
                ctypes.byref(pointer),
                ctypes.byref(length),
            )
            if result != 1 or not 1 <= length.value <= len(buffer):
                raise GatewayError("private key protection failed")
            return buffer.raw[: length.value]
        finally:
            # 擦除此处持有的原生结构；返回值等 Python bytes 副本不受此操作影响。
            ctypes.memset(ctypes.byref(ctx), 0, ctypes.sizeof(ctx))

    def import_private_key(self, encrypted_der: bytes, password: bytes) -> bytes:
        """Unlock an ACL-protected local DER file supplied by the configuration layer."""
        if (
            not 1 <= len(encrypted_der) <= 512
            or not 16 <= len(password) <= 1024
            or b"\x00" in password
        ):
            raise AuthenticationError("invalid protected key")
        ctx = self._binding.Sm2Key()
        buffer = ctypes.create_string_buffer(encrypted_der)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        length = ctypes.c_size_t(len(encrypted_der))
        attrs = ctypes.POINTER(ctypes.c_ubyte)()
        attrs_length = ctypes.c_size_t()
        try:
            result = self._binding.gmssl.sm2_private_key_info_decrypt_from_der(
                ctypes.byref(ctx),
                ctypes.byref(attrs),
                ctypes.byref(attrs_length),
                ctypes.c_char_p(password),
                ctypes.byref(pointer),
                ctypes.byref(length),
            )
            # 必须消费完整 DER，且不能附带本接口不支持的属性或尾随数据。
            if result != 1 or length.value != 0 or attrs_length.value != 0:
                raise AuthenticationError("private key unlock failed")
            return bytes(ctx.private_key)
        finally:
            ctypes.memset(ctypes.byref(ctx), 0, ctypes.sizeof(ctx))

    def _signature(self, material: bytes, signer_id: bytes, sign: bool) -> Any:
        # signer_id 参与 SM2 的身份摘要，签名和验签必须使用同一身份。
        if not 1 <= len(signer_id) <= 64:
            raise ValueError("invalid signer identity")
        identity = signer_id.decode("utf-8", errors="strict")
        return self._binding.Sm2Signature(self._key(material, sign), identity, sign)

    def sign_sm2(self, private_key: bytes, message: bytes, *, signer_id: bytes) -> bytes:
        """使用签名专用私钥和身份标识生成 SM2 签名。"""
        ctx = self._signature(private_key, signer_id, True)
        ctx.update(message)
        return cast(bytes, ctx.sign())

    def verify_sm2(
        self, public_key: bytes, message: bytes, signature: bytes, *, signer_id: bytes
    ) -> bool:
        """使用本地信任表提供的公钥与对端身份标识验证签名。"""
        if not 8 <= len(signature) <= 72:
            return False
        ctx = self._signature(public_key, signer_id, False)
        ctx.update(message)
        return cast(bool, ctx.verify(signature))

    def encrypt_sm2(self, public_key: bytes, plaintext: bytes) -> bytes:
        """用对端加密公钥保护握手密钥材料；大块业务数据交给 SM4-GCM。"""
        if not 1 <= len(plaintext) <= 255:
            raise ValueError("SM2 plaintext length out of range")
        return cast(bytes, self._key(public_key, False).encrypt(plaintext))

    def decrypt_sm2(self, private_key: bytes, ciphertext: bytes) -> bytes:
        """解开 SM2 握手密文，统一隐藏原生解密失败的细节。"""
        if not 45 <= len(ciphertext) <= 366:
            raise AuthenticationError("invalid SM2 ciphertext")
        try:
            return cast(bytes, self._key(private_key, True).decrypt(ciphertext))
        except self._binding.NativeError:
            raise AuthenticationError("SM2 authentication failed") from None

    def _gcm(self, key: bytes, nonce: bytes, aad: bytes, encrypt: bool) -> Any:
        # 协议固定 128 位密钥、96 位 nonce 和 128 位认证标签。
        if len(key) != 16 or len(nonce) != 12 or len(aad) > 131_072:
            raise ValueError("invalid GCM parameters")
        return self._binding.Sm4Gcm(key, nonce, aad, 16, encrypt)

    def seal_sm4_gcm(self, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        """保护单条记录，返回密文及标签；AAD 只认证而不加密。"""
        if len(plaintext) > MAX_RECORD_PLAINTEXT:
            raise ValueError("record exceeds plaintext limit")
        ctx = self._gcm(key, nonce, aad, True)
        return cast(bytes, ctx.update(plaintext) + ctx.finish())

    def open_sm4_gcm(
        self, key: bytes, nonce: bytes, ciphertext_and_tag: bytes, aad: bytes
    ) -> bytes:
        """认证成功后才返回明文，失败统一映射为 AuthenticationError。"""
        if not 16 <= len(ciphertext_and_tag) <= MAX_RECORD_PLAINTEXT + 16:
            raise AuthenticationError("invalid GCM record length")
        try:
            ctx = self._gcm(key, nonce, aad, False)
            temporary = bytearray(ctx.update(ciphertext_and_tag))
            try:
                # update 的输出尚未通过认证；finish 校验标签后才能交给业务层。
                temporary.extend(ctx.finish())
                return bytes(temporary)
            finally:
                # 擦除暂存缓冲区，避免认证失败后继续保留这份未认证明文。
                temporary[:] = b"\x00" * len(temporary)
        except self._binding.NativeError:
            raise AuthenticationError("record authentication failed") from None

    def hmac_sm3(self, key: bytes, message: bytes) -> bytes:
        """生成用于握手 Finished 的带密钥 SM3 消息认证码。"""
        ctx = self._binding.Sm3Hmac(key)
        ctx.update(message)
        return cast(bytes, ctx.generate_mac())
