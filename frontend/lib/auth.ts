// Auth de Cima (ADR-003, Fase C) — token JWT en cookie legible.
//
// El token vive en una cookie `cima_token` (NO httpOnly: el header
// `Authorization: Bearer` lo añade JS tanto en cliente como en SSR, y el backend
// sólo lee ese header). La cookie — frente a localStorage — la pueden leer los
// server components y el middleware, que es lo que necesita el App Router.
//
// En modo OWNER el backend puentea la auth (sin token): todo funciona sin login.
// En modo SAAS el middleware exige la cookie. El modo se expone al front vía
// NEXT_PUBLIC_CIMA_MODE (default 'owner' para no romper el uso local).

export const TOKEN_COOKIE = 'cima_token';

export const CIMA_MODE = (process.env.NEXT_PUBLIC_CIMA_MODE || 'owner').toLowerCase();
export const isSaasMode = CIMA_MODE === 'saas';

/** Lee el token de forma isomórfica: cookies() en servidor, document.cookie en cliente. */
export async function getToken(): Promise<string | null> {
  if (typeof window === 'undefined') {
    // Server component / Route handler: leer de la request.
    try {
      const { cookies } = await import('next/headers');
      return cookies().get(TOKEN_COOKIE)?.value ?? null;
    } catch {
      return null; // fuera de un contexto de request
    }
  }
  // Cliente.
  const m = document.cookie.match(new RegExp(`(?:^|; )${TOKEN_COOKIE}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

/** Guarda el token en cookie (sólo cliente). 7 días, SameSite=Lax. */
export function setToken(token: string): void {
  if (typeof document === 'undefined') return;
  const maxAge = 60 * 60 * 24 * 7;
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = `${TOKEN_COOKIE}=${encodeURIComponent(token)}; path=/; max-age=${maxAge}; SameSite=Lax${secure}`;
}

export function clearToken(): void {
  if (typeof document === 'undefined') return;
  document.cookie = `${TOKEN_COOKIE}=; path=/; max-age=0; SameSite=Lax`;
}
