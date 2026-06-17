'use client';

import { useEffect } from 'react';
import { refreshToken } from '@/lib/api';
import { getToken, setToken, isSaasMode } from '@/lib/auth';

// Renueva el access token de forma proactiva mientras la app está abierta, para
// que la sesión no caduque a media navegación (y evitar el 401 en SSR por token
// expirado). Sólo en saas y si ya hay token. No pinta nada.
const INTERVALO_MS = 1000 * 60 * 60 * 12; // cada 12 h (TTL backend = 7 días)

export function AuthRefresher() {
  useEffect(() => {
    if (!isSaasMode) return;

    let cancelado = false;
    async function renovar() {
      if (cancelado || !(await getToken())) return;
      try {
        const r = await refreshToken();
        if (!cancelado) setToken(r.access_token);
      } catch {
        // 401 → fetchJson ya gestiona la redirección a /login. Otros errores
        // (red): se reintenta en el próximo tick.
      }
    }
    renovar(); // al montar
    const id = setInterval(renovar, INTERVALO_MS);
    return () => { cancelado = true; clearInterval(id); };
  }, []);

  return null;
}
