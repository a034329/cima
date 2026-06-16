"""JWT HS256 con stdlib (`hmac`/`hashlib`/`base64`), ADR-003.

Emitimos y verificamos nuestros propios access tokens, así que no necesitamos
PyJWT: un JWT HS256 es base64url(header).base64url(payload).firma_hmac_sha256.
Lo crítico de seguridad se controla explícitamente:
  - se fija y se VALIDA `alg=HS256` (evita el ataque de confusión de algoritmo
    y los tokens `alg=none`);
  - se valida `exp`;
  - la firma se compara en tiempo constante con `hmac.compare_digest`.

El secreto sale de `settings.jwt_secret` (CIMA_JWT_SECRET en producción).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import settings


class TokenError(Exception):
    """Token ausente, mal formado, firma inválida o expirado."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(signing_input: bytes) -> bytes:
    return hmac.new(settings.jwt_secret.encode("utf-8"), signing_input,
                    hashlib.sha256).digest()


def create_access_token(sub: str, email: str,
                        ttl_min: int | None = None) -> str:
    """Crea un access token HS256 para el usuario `sub` (id interno)."""
    now = int(time.time())
    ttl = (ttl_min if ttl_min is not None else settings.access_token_ttl_min) * 60
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": sub, "email": email, "iat": now, "exp": now + ttl}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = _b64url_encode(_sign(signing_input))
    return f"{h}.{p}.{sig}"


def decode_token(token: str) -> dict:
    """Verifica firma + alg + exp y devuelve los claims. Lanza TokenError."""
    if not token or token.count(".") != 2:
        raise TokenError("formato inválido")
    h_b64, p_b64, sig_b64 = token.split(".")
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    try:
        header = json.loads(_b64url_decode(h_b64))
        payload = json.loads(_b64url_decode(p_b64))
        sig = _b64url_decode(sig_b64)
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError("no decodificable") from e
    # Alg fijado: rechaza 'none' y cualquier otro algoritmo (confusión de alg).
    if header.get("alg") != "HS256":
        raise TokenError("alg no permitido")
    if not hmac.compare_digest(sig, _sign(signing_input)):
        raise TokenError("firma inválida")
    exp = payload.get("exp")
    if not isinstance(exp, int) or time.time() >= exp:
        raise TokenError("expirado")
    return payload
