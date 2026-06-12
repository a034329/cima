'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { fetchPosiciones } from '@/lib/api';

interface Entrada {
  id: string;
  label: string;
  detalle?: string;
  href: string;
  grupo: 'Secciones' | 'Posiciones';
}

const ANIO = new Date().getFullYear();
const MES = new Date().getMonth() + 1;

const SECCIONES: Entrada[] = [
  { id: 'cartera', label: 'Cartera', href: '/', grupo: 'Secciones' },
  { id: 'estrategia', label: 'Estrategia', href: '/estrategia', grupo: 'Secciones' },
  { id: 'estimaciones', label: 'Estimaciones', href: '/estrategia/estimaciones', grupo: 'Secciones' },
  { id: 'plan', label: 'Plan de acción', href: '/estrategia/plan', grupo: 'Secciones' },
  { id: 'rotacion', label: 'Rotación fiscal', href: '/estrategia/rotacion', grupo: 'Secciones' },
  { id: 'seguimiento', label: 'Watchlist / Seguimiento', href: '/estrategia/seguimiento', grupo: 'Secciones' },
  { id: 'fiscal', label: 'Fiscalidad — Resumen', href: `/fiscal/${ANIO}`, grupo: 'Secciones' },
  { id: 'dividendos', label: 'Fiscalidad — Dividendos', href: `/fiscal/${ANIO}/dividendos`, grupo: 'Secciones' },
  { id: 'optimizar', label: 'Fiscalidad — Optimizar', href: `/fiscal/${ANIO}/optimizar`, grupo: 'Secciones' },
  { id: 'fugas', label: 'Fiscalidad — Fugas CDI', href: `/fiscal/${ANIO}/fugas`, grupo: 'Secciones' },
  { id: 'movimientos', label: 'Movimientos', href: '/transacciones', grupo: 'Secciones' },
  { id: 'informe', label: 'Cierre de mes', href: `/informe/${ANIO}/${MES}`, grupo: 'Secciones' },
  { id: 'config', label: 'Configuración', href: '/config', grupo: 'Secciones' },
];

function normaliza(s: string): string {
  return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

/** Paleta de comandos (cmd+K / ctrl+K): salto rápido a secciones y búsqueda
 *  de posiciones por nombre o ISIN (→ análisis del valor). */
export function CommandPalette() {
  const router = useRouter();
  const [abierta, setAbierta] = useState(false);
  const [query, setQuery] = useState('');
  const [sel, setSel] = useState(0);
  const [posiciones, setPosiciones] = useState<Entrada[]>([]);
  const cargadas = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Atajo global. cmd+K en macOS, ctrl+K en el resto.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setAbierta((v) => !v);
      } else if (e.key === 'Escape') {
        setAbierta(false);
      }
    };
    const onAbrir = () => setAbierta(true);
    window.addEventListener('keydown', onKey);
    window.addEventListener('cima:cmdk', onAbrir);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('cima:cmdk', onAbrir);
    };
  }, []);

  // Al abrir: foco + carga (una vez) de las posiciones para la búsqueda.
  useEffect(() => {
    if (!abierta) return;
    setQuery('');
    setSel(0);
    setTimeout(() => inputRef.current?.focus(), 0);
    if (!cargadas.current) {
      cargadas.current = true;
      fetchPosiciones()
        .then((r) => setPosiciones(r.posiciones.map((p) => ({
          id: p.isin,
          label: p.nombre,
          detalle: p.isin,
          href: `/estrategia/analisis?isin=${encodeURIComponent(p.isin)}`,
          grupo: 'Posiciones' as const,
        }))))
        .catch(() => { cargadas.current = false; });
    }
  }, [abierta]);

  const resultados = useMemo(() => {
    const q = normaliza(query.trim());
    const todas = [...SECCIONES, ...posiciones];
    if (!q) return SECCIONES;
    return todas.filter((e) =>
      normaliza(e.label).includes(q) || (e.detalle && normaliza(e.detalle).includes(q)),
    ).slice(0, 12);
  }, [query, posiciones]);

  const ir = useCallback((e: Entrada) => {
    setAbierta(false);
    router.push(e.href);
  }, [router]);

  if (!abierta) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 pt-[12vh] px-4"
      onClick={() => setAbierta(false)}
    >
      <div
        className="w-full max-w-lg rounded-xl border border-[rgb(var(--border))] bg-[rgb(var(--card))] shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => { setQuery(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, resultados.length - 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
            else if (e.key === 'Enter' && resultados[sel]) { e.preventDefault(); ir(resultados[sel]); }
          }}
          placeholder="Ir a sección o buscar posición (nombre / ISIN)…"
          className="w-full px-4 py-3 text-sm bg-transparent outline-none border-b border-[rgb(var(--border))]"
        />
        <ul className="max-h-[50vh] overflow-y-auto py-1">
          {resultados.length === 0 && (
            <li className="px-4 py-3 text-sm text-[rgb(var(--muted))]">Sin resultados.</li>
          )}
          {resultados.map((e, i) => (
            <li key={`${e.grupo}-${e.id}`}>
              <button
                onClick={() => ir(e)}
                onMouseEnter={() => setSel(i)}
                className={`w-full text-left px-4 py-2 text-sm flex items-baseline justify-between gap-3 ${
                  i === sel ? 'bg-brand-600/10 text-[rgb(var(--fg))]' : 'text-[rgb(var(--fg))]'
                }`}
              >
                <span className="truncate">{e.label}</span>
                <span className="text-[10px] text-[rgb(var(--muted))] font-mono shrink-0">
                  {e.detalle ?? e.grupo}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="px-4 py-2 border-t border-[rgb(var(--border))] text-[10px] text-[rgb(var(--muted))] flex gap-3">
          <span>↑↓ navegar</span><span>⏎ ir</span><span>esc cerrar</span>
        </div>
      </div>
    </div>
  );
}
