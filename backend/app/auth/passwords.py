"""Hash y verificación de contraseñas con `hashlib.scrypt` (stdlib, ADR-003).

scrypt es un KDF vetado y resistente a hardware (memory-hard); está en la
librería estándar desde Python 3.6, así que cubrimos el hash de contraseñas
sin añadir dependencias (passlib/bcrypt/argon2). Formato almacenado:

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

La verificación es de tiempo constante (`hmac.compare_digest`) y lee los
parámetros del propio string, de modo que subir el coste en el futuro no
invalida los hashes antiguos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Parámetros scrypt. n debe ser potencia de 2; (n=2^14, r=8, p=1) es el preset
# "interactivo" recomendado — ~16 MB de memoria, rápido para login.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def hash_password(password: str) -> str:
    """Devuelve el hash autodescrito `scrypt$n$r$p$salt$hash`."""
    if not password:
        raise ValueError("password vacío")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """True si `password` coincide con el hash almacenado. False ante cualquier
    formato inválido o hash None (p. ej. usuario owner sin contraseña)."""
    if not password or not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=n, r=r, p=p, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)
