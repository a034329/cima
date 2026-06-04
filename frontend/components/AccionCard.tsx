'use client';

import Link from 'next/link';
import { useState } from 'react';
import { crearPaso, editarEstimacion } from '@/lib/api';
import { notificarDatosActualizados } from '@/lib/refetch';
import type { AccionPropuesta } from '@/lib/types';

const TIPO_META: Record<string, { label: string; accent: string; icon: JSX.Element }> = {
  crear_paso: {
    label: 'Nuevo paso del plan',
    accent: 'border-l-sky-400 dark:border-l-sky-500',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
      </svg>
    ),
  },
  ajustar_estimacion: {
    label: 'Ajuste de estimación',
    accent: 'border-l-violet-400 dark:border-l-violet-500',
    icon: (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" />
        <line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" />
        <line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" />
        <line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" />
      </svg>
    ),
  },
};

/** Tarjeta de acción propuesta por la IA (asesor o hoja de ruta): el humano la
 *  aprueba (Aplicar) o la descarta. Aplicar llama al endpoint conocido. */
export function AccionCard({ a, onError }: { a: AccionPropuesta; onError: (e: string) => void }) {
  const [estado, setEstado] = useState<'idle' | 'aplicando' | 'aplicado' | 'descartado'>('idle');
  const meta = TIPO_META[a.tipo] ?? TIPO_META.crear_paso;

  const aplicar = async () => {
    try {
      if (a.tipo === 'crear_paso') {
        const p = a.params;
        setEstado('aplicando');
        await crearPaso({
          isin: a.isin,
          decision: p.decision as never,
          prioridad: (p.prioridad as never) ?? undefined,
          razon: (p.razon as string) ?? null,
          capital_objetivo_eur: p.capital_objetivo_eur != null ? String(p.capital_objetivo_eur) : null,
          // `nombre`/`ticker` opcionales: si la IA los propuso, el backend los
          // usa para auto-añadir el valor al watchlist (caso real: la IA propuso
          // un paso COMPRAR sobre un ISIN que no estaba ni en cartera ni en
          // seguimiento → 404. Ahora se añade al watchlist automáticamente).
          nombre: (p.nombre as string) ?? null,
          ticker: (p.ticker as string) ?? null,
        });
      } else {
        if (!window.confirm('Esto actualizará tu estimación con los valores propuestos. ¿Continuar?')) return;
        const p = a.params;
        const campos: Parameters<typeof editarEstimacion>[1] = { notas: 'Ajuste propuesto por el asesor IA' };
        if (p.tipo_val != null) campos.tipo_val = p.tipo_val as string;
        if (p.multiplo_objetivo != null) campos.multiplo_objetivo = p.multiplo_objetivo as number;
        if (p.metrica_base_4y != null) campos.metrica_base_4y = p.metrica_base_4y as number;
        if (p.dividendo_share != null) campos.dividendo_share = p.dividendo_share as number;
        setEstado('aplicando');
        await editarEstimacion(a.isin, campos);
      }
      setEstado('aplicado');
      notificarDatosActualizados();
    } catch (e) {
      setEstado('idle');
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  if (estado === 'descartado') {
    return (
      <div className="rounded-lg border border-dashed border-[rgb(var(--border))] px-3 py-2 text-xs text-[rgb(var(--muted))] flex items-center gap-2">
        <span className="line-through">{a.descripcion}</span>
        <button onClick={() => setEstado('idle')} className="ml-auto hover:text-[rgb(var(--fg))]">deshacer</button>
      </div>
    );
  }

  const aplicado = estado === 'aplicado';
  return (
    <div className={`rounded-lg border border-[rgb(var(--border))] border-l-[3px] ${meta.accent}
      bg-[rgb(var(--card))] p-3 text-sm shadow-sm transition-colors ${aplicado ? 'opacity-90' : ''}`}>
      <div className="flex items-center gap-1.5 mb-1.5">
        <span className="text-[rgb(var(--muted))]">{meta.icon}</span>
        <span className="text-[10px] font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
          {meta.label}
        </span>
        <span className="ml-auto text-[10px] font-mono text-[rgb(var(--muted))]">{a.isin}</span>
      </div>

      <p className="leading-snug text-[rgb(var(--fg))]">{a.descripcion}</p>

      <div className="flex items-center gap-3 mt-2.5 pt-2.5 border-t border-[rgb(var(--border))]">
        {a.tipo === 'ajustar_estimacion' && !aplicado && (
          <Link href={`/estrategia/analisis?isin=${encodeURIComponent(a.isin)}`}
            className="text-xs text-brand-600 dark:text-brand-400 hover:underline">
            Investigar →
          </Link>
        )}
        {aplicado ? (
          <span className="ml-auto inline-flex items-center gap-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <polyline points="20 6 9 17 4 12" />
            </svg>
            Aplicado
          </span>
        ) : (
          <div className="ml-auto flex items-center gap-1">
            <button onClick={() => setEstado('descartado')} disabled={estado === 'aplicando'}
              className="px-2.5 py-1 text-xs rounded-md text-[rgb(var(--muted))] hover:bg-[rgb(var(--bg))] hover:text-[rgb(var(--fg))] disabled:opacity-50">
              Descartar
            </button>
            <button onClick={aplicar} disabled={estado === 'aplicando'}
              className="px-3 py-1 text-xs font-medium rounded-md bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-60">
              {estado === 'aplicando' ? 'Aplicando…' : 'Aplicar'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
