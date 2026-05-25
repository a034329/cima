'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const ANO_ACTUAL = new Date().getFullYear();
const ANOS = [ANO_ACTUAL, ANO_ACTUAL - 1, ANO_ACTUAL - 2, ANO_ACTUAL - 3];

// Sub-secciones del hub fiscal. `seg` vacío = índice (Resumen del ejercicio).
const SECCIONES: { seg: string; label: string }[] = [
  { seg: '', label: 'Resumen' },
  { seg: 'acciones', label: 'Acciones' },
  { seg: 'opciones', label: 'Opciones' },
  { seg: 'dividendos', label: 'Dividendos' },
  { seg: 'intereses', label: 'Intereses' },
  { seg: 'forex', label: 'Forex' },
  { seg: 'letras', label: 'Letras' },
  { seg: 'complejos', label: 'Complejos' },
  { seg: 'optimizar', label: 'Optimizar' },
];

/**
 * Navegación del hub Fiscalidad: selector de ejercicio (cambia el año
 * conservando la sub-sección actual) + barra de sub-pestañas (conservan el
 * año). Todo derivado del pathname: /fiscal/<ano>/<seg>.
 */
export function FiscalNav() {
  const pathname = usePathname();
  const parts = pathname.split('/').filter(Boolean); // ['fiscal','2026','opciones']
  const ano = parts[1] ?? String(ANO_ACTUAL);
  const segActual = parts[2] ?? '';

  const yearHref = (y: string) => (segActual ? `/fiscal/${y}/${segActual}` : `/fiscal/${y}`);
  const segHref = (seg: string) => (seg ? `/fiscal/${ano}/${seg}` : `/fiscal/${ano}`);

  return (
    <div className="mb-6 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-2xl font-semibold tracking-tight">Fiscalidad</h2>
        <div className="flex flex-wrap gap-2">
          {ANOS.map((y) => (
            <Link
              key={y}
              href={yearHref(String(y))}
              className={`px-3 py-1.5 text-sm rounded border ${
                ano === String(y)
                  ? 'bg-brand-600 text-white border-brand-600'
                  : 'border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]'
              }`}
            >
              {y}
            </Link>
          ))}
          <Link
            href={yearHref('acumulado')}
            className={`px-3 py-1.5 text-sm rounded border ${
              ano === 'acumulado'
                ? 'bg-brand-600 text-white border-brand-600'
                : 'border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]'
            }`}
          >
            Acumulado
          </Link>
        </div>
      </div>

      <nav className="flex flex-wrap gap-1 border-b border-[rgb(var(--border))]">
        {SECCIONES.map((s) => {
          const activa = s.seg === segActual;
          return (
            <Link
              key={s.seg || 'resumen'}
              href={segHref(s.seg)}
              className={`px-3 py-2 text-sm -mb-px border-b-2 ${
                activa
                  ? 'border-brand-600 text-[rgb(var(--fg))] font-medium'
                  : 'border-transparent text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]'
              }`}
            >
              {s.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
