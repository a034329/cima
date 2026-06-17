'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  fetchEvolucionCartera,
  fetchEvolucionPosicion,
  refrescarHistorico,
  fmtEUR,
} from '@/lib/api';
import { onDatosActualizados } from '@/lib/refetch';
import type { SerieEvolucion } from '@/lib/api';

const VALOR = '#E6B763';     // valor de mercado (brand-300, oro)
const APORTADO = '#94a3b8';  // capital aportado (slate-400, neutro)

/** Evolución mensual: valor de mercado vs capital aportado. Para la cartera
 *  completa (sin `isin`) o una posición (`isin`). Si faltan cierres, dispara el
 *  backfill en segundo plano y hace polling hasta que se complete. */
export function EvolucionChart({ isin, titulo }: { isin?: string; titulo?: string }) {
  const [serie, setSerie] = useState<SerieEvolucion | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lanzado = useRef(false);

  const cargar = useCallback(async () => {
    try {
      const s = isin ? await fetchEvolucionPosicion(isin) : await fetchEvolucionCartera();
      setSerie(s);
      setError(null);
      return s;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [isin]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let vivo = true;

    async function ciclo() {
      const s = await cargar();
      if (!vivo || !s) return;
      const enMarcha = s.meses_pendientes > 0 || s.job === 'en_curso';
      // Dispara el backfill una sola vez si hay meses por bajar.
      if (s.meses_pendientes > 0 && s.job !== 'en_curso' && !lanzado.current) {
        lanzado.current = true;
        try { await refrescarHistorico(); } catch { /* reintenta el ciclo */ }
      }
      if (enMarcha) timer = setTimeout(ciclo, 3000);   // polling mientras se construye
    }
    ciclo();
    const off = onDatosActualizados(() => { lanzado.current = false; ciclo(); });
    return () => { vivo = false; if (timer) clearTimeout(timer); off(); };
  }, [cargar]);

  if (error) {
    return (
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
        <p className="text-sm text-[rgb(var(--muted))]">No se pudo cargar la evolución.</p>
      </div>
    );
  }
  if (!serie) {
    return <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 text-sm text-[rgb(var(--muted))]">Cargando evolución…</div>;
  }

  const puntos = serie.puntos.map((p) => ({
    ym: p.anio_mes,
    valor: parseFloat(p.valor_eur),
    aportado: parseFloat(p.aportado_eur),
    completo: p.completo,
  }));
  const conValor = puntos.filter((p) => p.valor > 0 || p.aportado > 0);
  const ultimo = conValor[conValor.length - 1];
  const ganancia = ultimo ? ultimo.valor - ultimo.aportado : 0;
  const gananciaPct = ultimo && ultimo.aportado > 0 ? (ganancia / ultimo.aportado) * 100 : null;
  const construyendo = serie.meses_pendientes > 0 || serie.job === 'en_curso';

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <h4 className="font-semibold text-base">{titulo ?? 'Evolución de la cartera'}</h4>
        {ultimo && (
          <span className={`text-sm font-medium ${ganancia >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
            {ganancia >= 0 ? '▲' : '▼'} {fmtEUR(Math.abs(ganancia), { maximumFractionDigits: 0 })}
            {gananciaPct != null && ` (${ganancia >= 0 ? '+' : '−'}${Math.abs(gananciaPct).toFixed(0)}%)`}
          </span>
        )}
      </div>

      <div className="flex items-center gap-4 text-xs text-[rgb(var(--muted))] mb-2">
        <span className="flex items-center gap-1"><i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: VALOR }} /> valor de mercado</span>
        {!isin && <span className="flex items-center gap-1"><i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: APORTADO }} /> capital aportado</span>}
        {construyendo && <span className="ml-auto animate-pulse">·· construyendo histórico ··</span>}
      </div>

      {conValor.length < 2 ? (
        <p className="text-sm text-[rgb(var(--muted))] py-8 text-center">
          {construyendo ? 'Descargando cierres mensuales…' : 'Aún no hay suficientes cierres mensuales para la gráfica.'}
        </p>
      ) : (
        <Lineas puntos={puntos} mostrarAportado={!isin} />
      )}
    </div>
  );
}

interface P { ym: string; valor: number; aportado: number; completo: boolean }

function Lineas({ puntos, mostrarAportado }: { puntos: P[]; mostrarAportado: boolean }) {
  const W = 100 * Math.max(puntos.length, 2);
  const H = 220;
  const PAD_T = 16, PAD_B = 24, PAD_L = 4, PAD_R = 4;
  const PLOT = H - PAD_T - PAD_B;
  const vals = puntos.flatMap((p) => (mostrarAportado ? [p.valor, p.aportado] : [p.valor]));
  const max = Math.max(...vals, 1);
  const min = Math.min(...vals, 0);
  const span = max - min || 1;
  const n = puntos.length;
  const x = (i: number) => PAD_L + (n === 1 ? 0 : (i * (W - PAD_L - PAD_R)) / (n - 1));
  const y = (v: number) => PAD_T + (PLOT - ((v - min) / span) * PLOT);

  const linea = (sel: (p: P) => number) =>
    puntos.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(sel(p)).toFixed(1)}`).join(' ');

  // Etiquetas de eje X: ~6 repartidas para no saturar.
  const stepLbl = Math.max(1, Math.ceil(n / 6));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 220 }} preserveAspectRatio="none">
      {mostrarAportado && (
        <path d={linea((p) => p.aportado)} fill="none" stroke={APORTADO} strokeWidth="1.5"
              strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
      )}
      <path d={linea((p) => p.valor)} fill="none" stroke={VALOR} strokeWidth="2"
            vectorEffect="non-scaling-stroke" />
      {puntos.map((p, i) => (
        <g key={p.ym}>
          {p.valor > 0 && (
            <circle cx={x(i)} cy={y(p.valor)} r="2.5" fill={VALOR}>
              <title>{`${p.ym}: ${fmtEUR(p.valor, { maximumFractionDigits: 0 })}${mostrarAportado ? ` · aportado ${fmtEUR(p.aportado, { maximumFractionDigits: 0 })}` : ''}${p.completo ? '' : ' (incompleto)'}`}</title>
            </circle>
          )}
          {i % stepLbl === 0 && (
            <text x={x(i)} y={H - 6} textAnchor="middle"
                  className="fill-[rgb(var(--muted))]" style={{ fontSize: 10 }}>
              {p.ym.slice(2)}
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}
