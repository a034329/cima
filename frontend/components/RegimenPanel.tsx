'use client';

import { useState } from 'react';
import { fmtEUR } from '@/lib/api';
import { PropuestaRegimenCard } from '@/components/PropuestaRegimenCard';
import type { RegimenEstado, SenalMacro } from '@/lib/types';

const INDICADORES: { k: keyof RegimenEstado['indicadores']; label: string }[] = [
  { k: 'ciclo', label: 'Ciclo económico' },
  { k: 'inflacion', label: 'Inflación / Tipos' },
  { k: 'geopolitica', label: 'Geopolítica / M. primas' },
  { k: 'mercado', label: 'Mercado / Sentimiento' },
];

const SENAL_OPT: { v: SenalMacro; t: string }[] = [
  { v: 'VERDE', t: '🟢 Verde' },
  { v: 'AMARILLA', t: '🟡 Amarilla' },
  { v: 'ROJA', t: '🔴 Roja' },
];

const REGIMEN_BADGE: Record<RegimenEstado['regimen'], string> = {
  VERDE: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  AMARILLO: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  ROJO: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300',
};

export function RegimenPanel({ estado, onGuardar, onAutoFirmada }: {
  estado: RegimenEstado;
  onGuardar: (ind: RegimenEstado['indicadores']) => Promise<void>;
  // Tras firmar la propuesta auto, el padre re-fetchea el régimen vigente. Si no
  // se pasa, cae al `onGuardar` con los nuevos indicadores (efecto equivalente).
  onAutoFirmada?: (e: RegimenEstado) => void;
}) {
  const [guardando, setGuardando] = useState(false);

  const cambiar = async (k: keyof RegimenEstado['indicadores'], v: SenalMacro) => {
    setGuardando(true);
    try {
      await onGuardar({ ...estado.indicadores, [k]: v });
    } finally {
      setGuardando(false);
    }
  };

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="mb-3 flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
            Régimen macro
          </span>
          <span className={`text-xs px-2 py-0.5 rounded font-medium ${REGIMEN_BADGE[estado.regimen]}`}>
            {estado.regimen}
          </span>
          {guardando && <span className="text-[10px] text-[rgb(var(--muted))]">guardando…</span>}
        </div>
        <span className="text-xs text-[rgb(var(--muted))]">
          Tramo <strong>{fmtEUR(estado.tramo_min, { maximumFractionDigits: 0 })}–{fmtEUR(estado.tramo_max, { maximumFractionDigits: 0 })}</strong> · cada {estado.espaciado}
          {estado.actualizado && ` · act. ${estado.actualizado}`}
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {INDICADORES.map(({ k, label }) => (
          <label key={k} className="block text-xs">
            <span className="block text-[rgb(var(--muted))] mb-1 truncate">{label}</span>
            <select
              value={estado.indicadores[k]}
              onChange={(e) => cambiar(k, e.target.value as SenalMacro)}
              className="w-full px-2 py-1 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            >
              {SENAL_OPT.map((o) => <option key={o.v} value={o.v}>{o.t}</option>)}
            </select>
          </label>
        ))}
      </div>
      {estado.correccion && (estado.correccion.sp_drawdown != null || estado.correccion.activa) && (
        <div className={`mt-3 rounded-md p-2 text-[11px] leading-snug ${
          estado.correccion.activa
            ? 'border border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-300'
            : 'border border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-[rgb(var(--muted))]'
        }`}>
          <span className="font-medium">Regla −14%</span>
          {estado.correccion.sp_drawdown != null && (
            <span className="font-mono"> · S&P {(estado.correccion.sp_drawdown * 100).toFixed(0)}%</span>
          )}
          {estado.correccion.vix != null && <span className="font-mono"> · VIX {estado.correccion.vix.toFixed(0)}</span>}
          <span> — {estado.correccion.nota}</span>
        </div>
      )}
      <p className="mt-2 text-[11px] text-[rgb(var(--muted))]">
        El modelo da el destino; el macro da el ritmo. El régimen calibra el tamaño y espaciado del
        DCA en la guía de compra de cada bloque.
      </p>
      <PropuestaRegimenCard
        vigente={estado}
        onAplicar={(e) => {
          if (onAutoFirmada) onAutoFirmada(e);
          else void onGuardar(e.indicadores);
        }}
      />
    </div>
  );
}
