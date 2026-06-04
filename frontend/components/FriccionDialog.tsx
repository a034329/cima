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
  // Si NO hay rebate2 distinto a rebate1, no tiene sentido el flujo de 2 pasos
  // — quedaría una pantalla idéntica a la anterior y el usuario interpretaría
  // que "no pasa nada al pulsar". Colapsamos a 1 paso con el botón ejecutor
  // directo (caso real: el motor no siempre tiene un segundo argumento).
  const tieneRebate2 = !!(friccion.rebate2 || '').trim() &&
                       friccion.rebate2.trim() !== (friccion.rebate1 || '').trim();
  const [nivel, setNivel] = useState<1 | 2>(tieneRebate2 ? 1 : 2);
  const [motivo, setMotivo] = useState('');
  const alta = friccion.severidad === 'ALTA';
  const caja = alta
    ? 'border-rose-200 dark:border-rose-800 bg-rose-50 dark:bg-rose-900/20'
    : 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      // CRÍTICO: si el padre que monta el diálogo tiene un overlay con
      // click-to-close (típico modal con `onClick={onClose}` en el backdrop),
      // los clicks de los botones del diálogo burbujean hasta él y cierran el
      // modal padre entero — eso desmonta este componente y reinicia el nivel
      // a 1, dando la sensación de "no avanza al paso 2" (caso real Angel,
      // 2026-06-02→03). Cortamos la propagación al nivel del overlay propio.
      onClick={(e) => e.stopPropagation()}
    >
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
        {/* Paso 1: solo rebate1. Paso 2: solo rebate2 + input motivo (oculta
            la caja anterior para que el cambio visual sea evidente y no parezca
            que "no pasa nada al pulsar"). */}
        {nivel === 1 && (
          <div className={`rounded-lg border ${caja} p-3 text-sm`}>{friccion.rebate1}</div>
        )}
        {nivel === 2 && (
          <>
            <div className={`rounded-lg border-2 border-rose-400 dark:border-rose-500 bg-rose-50 dark:bg-rose-900/20 p-3 text-sm`}>
              {tieneRebate2 ? friccion.rebate2 : friccion.rebate1}
            </div>
            <input
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              placeholder="¿Por qué aun así? (se guarda con la decisión)"
              className="mt-3 w-full px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
              autoFocus
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
          {/* Indicador del nº de pasos para que el usuario no confunda el primer
              "seguir" con la ejecución real (caso real: usuario pulsó "Aun así
              quiero seguir" pensando que vendía, y nunca llegó al paso 2). */}
          {tieneRebate2 && (
            <span className="text-[10px] text-[rgb(var(--muted))]">
              paso {nivel} de 2
            </span>
          )}
          {nivel === 1 ? (
            <button
              onClick={() => setNivel(2)}
              title="No ejecuta todavía — abre la siguiente razón antes de decidir"
              className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]"
            >
              Dame otra razón antes →
            </button>
          ) : (
            <button
              onClick={() => onProceder(motivo)}
              className="px-3 py-1.5 text-sm rounded border-2 border-rose-400 text-rose-700 dark:text-rose-300 dark:border-rose-500 bg-rose-50 dark:bg-rose-900/30 hover:bg-rose-100 dark:hover:bg-rose-900/50 font-medium"
            >
              {etiquetaProceder}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
