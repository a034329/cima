'use client';

import { useState } from 'react';
import type { FriccionResultado } from '@/lib/types';

// Diálogo de fricción: avisa, rebate 2 veces (datos → tú), te deja, captura el override.
// Nunca bloquea. Reutilizable desde la creación de pasos del plan y desde el alta
// real de transacciones (vender/comprar). `etiquetaProceder` personaliza el CTA final.
export function FriccionDialog({
  friccion,
  etiquetaProceder = 'Continuar de todos modos',
  onReconsiderar,
  onProceder,
}: {
  friccion: FriccionResultado;
  etiquetaProceder?: string;
  onReconsiderar: () => void;
  onProceder: (motivo: string) => Promise<void> | void;
}) {
  const [nivel, setNivel] = useState<1 | 2>(1);
  const [motivo, setMotivo] = useState('');
  const alta = friccion.severidad === 'ALTA';
  const caja = alta
    ? 'border-rose-200 dark:border-rose-800 bg-rose-50 dark:bg-rose-900/20'
    : 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-xl border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 shadow-xl">
        <div className="flex items-center gap-2 mb-3">
          <span className={`text-lg ${alta ? 'text-rose-500' : 'text-amber-500'}`}>⚠️</span>
          <h3 className="text-base font-semibold">{friccion.titulo}</h3>
        </div>
        {friccion.etiquetas.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {friccion.etiquetas.map((e) => (
              <span key={e} className="text-[11px] px-1.5 py-0.5 rounded bg-[rgb(var(--border))]/50 text-[rgb(var(--muted))]">
                {e}
              </span>
            ))}
          </div>
        )}
        <div className={`rounded-lg border ${caja} p-3 text-sm`}>{friccion.rebate1}</div>
        {nivel === 2 && (
          <>
            <div className={`mt-2 rounded-lg border ${caja} p-3 text-sm`}>{friccion.rebate2}</div>
            <input
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              placeholder="¿Por qué aun así? (se guarda con la decisión)"
              className="mt-3 w-full px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            />
          </>
        )}
        <div className="mt-4 flex items-center justify-between gap-2">
          <button
            onClick={onReconsiderar}
            className="px-4 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700"
          >
            Reconsiderar
          </button>
          {nivel === 1 ? (
            <button
              onClick={() => setNivel(2)}
              className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]"
            >
              Aun así quiero seguir →
            </button>
          ) : (
            <button
              onClick={() => onProceder(motivo)}
              className="px-3 py-1.5 text-sm rounded border border-rose-300 text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-900/20"
            >
              {etiquetaProceder}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
