'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { AccionCard } from '@/components/AccionCard';
import { fetchHojaRuta, fmtEUR, fmtPct, generarHojaRuta } from '@/lib/api';
import type { AccionPropuesta, HojaRuta, PasoPropuesto } from '@/lib/types';

function toAccion(p: PasoPropuesto): AccionPropuesta {
  const cap = p.capital_objetivo_eur != null ? ` · ${fmtEUR(p.capital_objetivo_eur, { maximumFractionDigits: 0 })}` : '';
  const wl = p.en_cartera ? '' : ' · watchlist';
  const razon = p.razon ? ` — ${p.razon}` : '';
  return {
    tipo: 'crear_paso',
    isin: p.isin,
    descripcion: `${p.decision} ${p.nombre}${cap} (${p.prioridad})${wl}${razon}`,
    params: {
      decision: p.decision,
      prioridad: p.prioridad,
      capital_objetivo_eur: p.capital_objetivo_eur,
      razon: p.razon,
    },
  };
}

/** Pantalla final del onboarding: tras firmar, se genera (en segundo plano) la
 *  hoja de ruta para cerrar el déficit y el usuario aprueba cada paso. */
export function HojaRutaReview() {
  const [hr, setHr] = useState<HojaRuta | null>(null);
  const [estado, setEstado] = useState<'en_curso' | 'ok' | 'error'>('en_curso');
  const [error, setError] = useState<string | null>(null);
  const lanzado = useRef(false);

  useEffect(() => {
    if (lanzado.current) return;           // evita doble disparo (strict mode dev)
    lanzado.current = true;
    generarHojaRuta()
      .then(() => setEstado('en_curso'))
      .catch((e) => { setEstado('error'); setError(e instanceof Error ? e.message : String(e)); });
  }, []);

  useEffect(() => {
    if (estado !== 'en_curso') return;
    const id = setInterval(async () => {
      try {
        const r = await fetchHojaRuta();
        if (r.resultado) setHr(r.resultado);
        if (r.estado === 'ok') { setEstado('ok'); clearInterval(id); }
        else if (r.estado === 'error') { setEstado('error'); setError(r.error); clearInterval(id); }
      } catch { /* reintenta en el siguiente tick */ }
    }, 4000);
    return () => clearInterval(id);
  }, [estado]);

  return (
    <div className="space-y-4">
      <div className="text-center space-y-1">
        <div className="text-2xl">✓</div>
        <p className="font-medium">Plan firmado</p>
        <p className="text-sm text-[rgb(var(--muted))]">
          Tus objetivos por bloque están fijados. Esta es tu <strong>hoja de ruta</strong> para
          cerrar el déficit — revísala y añade al plan los pasos que apruebes.
        </p>
      </div>

      {estado === 'en_curso' && (
        <p className="text-sm text-[rgb(var(--muted))] text-center animate-pulse py-4">
          La IA está trazando tu hoja de ruta… (puede tardar un par de minutos)
        </p>
      )}
      {estado === 'error' && (
        <p className="text-sm text-rose-600 dark:text-rose-400 text-center">
          No se pudo generar la hoja de ruta{error ? `: ${error}` : ''}. Puedes intentarlo más tarde
          desde el chat del asesor.
        </p>
      )}

      {hr && (
        <>
          {/* Déficit por bloque (determinista) */}
          <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-3">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-[rgb(var(--muted))] mb-2">
              Déficit por bloque
            </div>
            <ul className="space-y-1 text-sm">
              {hr.deficit.map((g) => (
                <li key={g.categoria_base} className="flex items-center gap-2">
                  <span className="truncate">{g.nombre}</span>
                  <span className="text-xs text-[rgb(var(--muted))]">
                    {fmtPct(g.peso_actual, 0)} / {fmtPct(g.peso_objetivo, 0)}
                  </span>
                  <span className={`ml-auto font-mono text-xs ${g.deficit_eur > 0 ? 'text-amber-600 dark:text-amber-400' : 'text-[rgb(var(--muted))]'}`}>
                    {g.deficit_eur > 0 ? `faltan ${fmtEUR(g.deficit_eur, { maximumFractionDigits: 0 })}` : 'en objetivo'}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {hr.resumen && (
            <p className="text-sm text-[rgb(var(--fg))] leading-snug bg-[rgb(var(--bg))] border border-[rgb(var(--border))] rounded-lg p-3">
              {hr.resumen}
            </p>
          )}

          {/* Pasos propuestos → tarjetas aprobables */}
          {hr.pasos.length > 0 ? (
            <div className="space-y-2">
              <div className="text-[11px] uppercase tracking-wider text-[rgb(var(--muted))]">
                Pasos propuestos (aprueba los que quieras añadir al plan)
              </div>
              {hr.pasos.map((p, i) => <AccionCard key={`${p.isin}-${i}`} a={toAccion(p)} onError={setError} />)}
            </div>
          ) : estado === 'ok' ? (
            <p className="text-sm text-[rgb(var(--muted))]">
              No hay pasos sobre tus posiciones actuales. Revisa los huecos de abajo.
            </p>
          ) : null}

          {hr.huecos.length > 0 && (
            <div className="rounded-lg border border-dashed border-[rgb(var(--border))] p-3 text-sm">
              <span className="text-[rgb(var(--muted))]">
                Bloques con déficit pero sin un valor que reforzar:{' '}
                <strong>{hr.huecos.join(', ')}</strong>. Elige candidatos en{' '}
              </span>
              <Link href="/estrategia/analisis" className="text-brand-600 dark:text-brand-400 hover:underline">
                Análisis
              </Link>
              <span className="text-[rgb(var(--muted))]"> y añádelos a tu watchlist.</span>
            </div>
          )}

          {hr.disclaimer && (
            <p className="text-[11px] text-[rgb(var(--muted))] italic">{hr.disclaimer}</p>
          )}
        </>
      )}

      <div className="text-center pt-2">
        <Link href="/estrategia" className="px-4 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 inline-block">
          Ir a mi estrategia →
        </Link>
      </div>
    </div>
  );
}
