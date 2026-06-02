'use client';

import { useEffect, useState } from 'react';
import {
  descartarRegimenAuto,
  fetchRegimenAuto,
  firmarRegimenAuto,
  lanzarRegimenAuto,
} from '@/lib/api';
import type {
  ClaveIndicadorMacro,
  IndicadorPropuesta,
  PropuestaRegimen,
  RegimenAutoEstado,
  RegimenEstado,
  SenalMacro,
} from '@/lib/types';

const INDICADORES: { k: ClaveIndicadorMacro; label: string }[] = [
  { k: 'ciclo', label: 'Ciclo económico' },
  { k: 'inflacion', label: 'Inflación / Tipos' },
  { k: 'geopolitica', label: 'Geopolítica / M. primas' },
  { k: 'mercado', label: 'Mercado / Sentimiento' },
];

const SENAL_PILL: Record<SenalMacro, { bg: string; emoji: string }> = {
  VERDE:    { bg: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300', emoji: '🟢' },
  AMARILLA: { bg: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',         emoji: '🟡' },
  ROJA:     { bg: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300',             emoji: '🔴' },
};

function Pill({ s }: { s: SenalMacro }) {
  const p = SENAL_PILL[s];
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium ${p.bg}`}>
      <span>{p.emoji}</span>
      <span>{s}</span>
    </span>
  );
}

function FilaIndicador({
  k, label, vigente, propuesta,
}: {
  k: ClaveIndicadorMacro;
  label: string;
  vigente: SenalMacro;
  propuesta: IndicadorPropuesta | undefined;
}) {
  const cambia = propuesta && propuesta.senal !== vigente;
  return (
    <li className="rounded-md border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-2.5">
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="text-xs font-medium">{label}</span>
        <span className="flex items-center gap-1.5 text-[10px]">
          <span className="text-[rgb(var(--muted))]">ahora</span>
          <Pill s={vigente} />
          {propuesta && (
            <>
              <span className={`text-[rgb(var(--muted))] ${cambia ? 'font-bold' : ''}`}>→</span>
              <Pill s={propuesta.senal} />
            </>
          )}
        </span>
      </div>
      {propuesta && (
        <>
          <p className="text-[11px] leading-snug text-[rgb(var(--muted))]">{propuesta.razon}</p>
          {propuesta.fuentes.length > 0 && (
            <p className="mt-1 text-[10px] text-[rgb(var(--muted))] truncate" title={propuesta.fuentes.join(' · ')}>
              Fuentes: {propuesta.fuentes.slice(0, 2).join(' · ')}
              {propuesta.fuentes.length > 2 ? ` (+${propuesta.fuentes.length - 2})` : ''}
            </p>
          )}
        </>
      )}
    </li>
  );
}

export function PropuestaRegimenCard({
  vigente, onAplicar,
}: {
  vigente: RegimenEstado;
  onAplicar: (estado: RegimenEstado) => void;     // tras firmar, refresca el panel padre
}) {
  const [auto, setAuto] = useState<RegimenAutoEstado | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trabajando, setTrabajando] = useState(false);

  // Carga inicial: ¿hay propuesta pendiente o job en curso?
  useEffect(() => {
    void (async () => {
      try { setAuto(await fetchRegimenAuto()); } catch (e) {
        setError(e instanceof Error ? e.message : 'No se pudo leer la propuesta.');
      }
    })();
  }, []);

  // Polling mientras el job esté en curso (jobs reales: minutos).
  useEffect(() => {
    if (auto?.estado !== 'en_curso') return;
    const id = setInterval(async () => {
      try {
        const r = await fetchRegimenAuto();
        setAuto(r);
        if (r.estado !== 'en_curso') clearInterval(id);
      } catch { /* el siguiente tick reintenta */ }
    }, 3000);
    return () => clearInterval(id);
  }, [auto?.estado]);

  const lanzar = async () => {
    setError(null); setTrabajando(true);
    try {
      setAuto(await lanzarRegimenAuto());
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo lanzar la auto-clasificación.');
    } finally { setTrabajando(false); }
  };

  const firmar = async () => {
    setError(null); setTrabajando(true);
    try {
      const estado = await firmarRegimenAuto();
      onAplicar(estado);
      setAuto({ estado: 'ninguno', error: null, propuesta: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo firmar.');
    } finally { setTrabajando(false); }
  };

  const descartar = async () => {
    setError(null); setTrabajando(true);
    try {
      await descartarRegimenAuto();
      setAuto({ estado: 'ninguno', error: null, propuesta: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo descartar.');
    } finally { setTrabajando(false); }
  };

  const propuesta: PropuestaRegimen | null = auto?.propuesta ?? null;
  const enCurso = auto?.estado === 'en_curso';

  return (
    <div className="mt-3 rounded-md border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3">
      <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
            Auto-clasificación
          </span>
          {propuesta && !enCurso && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
              SENAL_PILL[propuesta.regimen === 'AMARILLO' ? 'AMARILLA'
                : propuesta.regimen === 'ROJO' ? 'ROJA' : 'VERDE'].bg
            }`}>
              propuesta: {propuesta.regimen}
            </span>
          )}
          {enCurso && (
            <span className="text-[10px] text-[rgb(var(--muted))] animate-pulse">
              clasificando con búsqueda web…
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {!enCurso && (
            <button
              onClick={lanzar}
              disabled={trabajando}
              className="px-2.5 py-1 text-[11px] rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--card))] disabled:opacity-50"
            >
              {propuesta ? 'Regenerar' : 'Auto-clasificar'}
            </button>
          )}
        </div>
      </div>

      {error && (
        <p className="text-[11px] text-rose-600 dark:text-rose-400 mb-2">{error}</p>
      )}
      {auto?.estado === 'error' && (
        <p className="text-[11px] text-rose-600 dark:text-rose-400 mb-2">
          El job falló: {auto.error || 'error desconocido'}.
        </p>
      )}

      {!propuesta && !enCurso && (
        <p className="text-[11px] text-[rgb(var(--muted))]">
          Pulsa <strong>Auto-clasificar</strong> para que el sistema lea S&amp;P/VIX/Brent/curva
          (ancla numérica) y la IA busque inflación + tipos + paro/PIB + geopolítica en la web. La
          propuesta aparece aquí y solo cambia el régimen vigente si la firmas.
        </p>
      )}

      {propuesta && (
        <>
          <ul className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {INDICADORES.map(({ k, label }) => (
              <FilaIndicador
                key={k}
                k={k}
                label={label}
                vigente={vigente.indicadores[k]}
                propuesta={propuesta.indicadores[k]}
              />
            ))}
          </ul>
          <div className="mt-2 flex items-center justify-between gap-2 flex-wrap">
            <span className="text-[10px] text-[rgb(var(--muted))]">
              Generada {propuesta.created_at.slice(0, 16).replace('T', ' ')}
              {propuesta.proveedor !== '?' && ` · ${propuesta.proveedor}`}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={descartar}
                disabled={trabajando}
                className="px-2.5 py-1 text-[11px] rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] disabled:opacity-50"
              >
                Descartar
              </button>
              <button
                onClick={firmar}
                disabled={trabajando}
                className="px-3 py-1 text-[11px] rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
              >
                Firmar y aplicar
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
