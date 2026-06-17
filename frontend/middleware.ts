import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// Gate de UX (ADR-003, Fase C). Sólo actúa en modo SaaS: en owner el backend
// puentea la auth y no hay login. La validez del token la comprueba el backend
// (401); aquí sólo miramos PRESENCIA de la cookie para redirigir a /login.
const SAAS = (process.env.NEXT_PUBLIC_CIMA_MODE || 'owner').toLowerCase() === 'saas';
const TOKEN_COOKIE = 'cima_token';
const PUBLICAS = ['/login', '/signup'];

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const esPublica = PUBLICAS.some((p) => pathname === p || pathname.startsWith(p + '/'));

  // En modo owner no hay login: /login y /signup no aplican → a la home.
  if (!SAAS) {
    if (esPublica) {
      const url = req.nextUrl.clone();
      url.pathname = '/';
      url.search = '';
      return NextResponse.redirect(url);
    }
    return NextResponse.next();
  }

  const tieneToken = Boolean(req.cookies.get(TOKEN_COOKIE)?.value);

  // Sin token y ruta protegida → a login con ?next= para volver después.
  if (!tieneToken && !esPublica) {
    const url = req.nextUrl.clone();
    url.pathname = '/login';
    url.search = `?next=${encodeURIComponent(pathname + search)}`;
    return NextResponse.redirect(url);
  }
  // Con token y en login/signup → a la home.
  if (tieneToken && esPublica) {
    const url = req.nextUrl.clone();
    url.pathname = '/';
    url.search = '';
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

// Excluir assets estáticos y la API interna de Next.
export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)'],
};
