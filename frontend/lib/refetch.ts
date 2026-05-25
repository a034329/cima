'use client';

// Señal global para que las vistas que cargan datos del API (Dashboard,
// Posiciones…) se recarguen tras una mutación (import, alta de operación) hecha
// en un componente hermano. `router.refresh()` solo re-ejecuta los componentes
// de servidor; los cliente con useEffect([]) no vuelven a hacer fetch sin esto.
export const EVENTO_DATOS = 'cima:datos-actualizados';

export function notificarDatosActualizados(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(EVENTO_DATOS));
  }
}

export function onDatosActualizados(cb: () => void): () => void {
  if (typeof window === 'undefined') return () => {};
  window.addEventListener(EVENTO_DATOS, cb);
  return () => window.removeEventListener(EVENTO_DATOS, cb);
}
