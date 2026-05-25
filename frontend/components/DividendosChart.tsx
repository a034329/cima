'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { fetchSerieDividendos, fmtEUR } from '@/lib/api';
import { onDatosActualizados } from '@/lib/refetch';
import type { SerieDividendos } from '@/lib/types';

const ORO = '#E6B763';        // bruto (brand-300)
const BRONCE = '#B8860B';     // neto (brand-600)
const MESES = ['E', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];

export function DividendosChart() {
  const [serie, setSerie] = useState<SerieDividendos | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vista, setVista] = useState<'anual' | 'mensual'>('anual');
  const [anioMes, setAnioMes] = useState<number | null>(null);

  const cargar = useCallback(() => {
    fetchSerieDividendos()
      .then((s) => { setSerie(s); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    cargar();
    return onDatosActualizados(cargar);
  }, [cargar]);

  const aniosDisponibles = useMemo(
    () => (serie ? [...new Set(serie.mensual.map((m) => m.anio))].sort((a, b) => b - a) : []),
    [serie],
  );
  const anioSel = anioMes ?? aniosDisponibles[0] ?? new Date().getFullYear();

  if (error) {
    return (
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
        <p className="text-sm text-[rgb(var(--muted))]">Sin datos de dividendos todavía.</p>
      </div>
    );
  }
  if (!serie) {
    return <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 text-sm text-[rgb(var(--muted))]">Cargando dividendos…</div>;
  }

  const barras = vista === 'anual'
    ? serie.anual.map((p) => ({
        etiqueta: String(p.anio),
        bruto: parseFloat(p.bruto),
        neto: parseFloat(p.neto),
      }))
    : Array.from({ length: 12 }, (_, i) => {
        const m = serie.mensual.find((x) => x.anio === anioSel && x.mes === i + 1);
        return { etiqueta: MESES[i], bruto: m ? parseFloat(m.bruto) : 0, neto: m ? parseFloat(m.bruto) : 0 };
      });

  const ultimo = serie.anual[serie.anual.length - 1];
  const previo = serie.anual[serie.anual.length - 2];
  const yoy = ultimo && previo && parseFloat(previo.bruto) > 0
    ? (parseFloat(ultimo.bruto) / parseFloat(previo.bruto) - 1) * 100
    : null;

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <h4 className="font-semibold text-base">Dividendos cobrados</h4>
        <div className="flex items-center gap-2">
          {vista === 'mensual' && aniosDisponibles.length > 1 && (
            <select
              value={anioSel}
              onChange={(e) => setAnioMes(parseInt(e.target.value, 10))}
              className="px-2 py-0.5 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            >
              {aniosDisponibles.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
          )}
          <div className="flex rounded border border-[rgb(var(--border))] overflow-hidden text-xs">
            {(['anual', 'mensual'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setVista(v)}
                className={`px-2 py-0.5 ${vista === v ? 'bg-brand-600 text-white' : 'hover:bg-[rgb(var(--bg))]'}`}
              >
                {v === 'anual' ? 'Anual' : 'Mensual'}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4 text-xs text-[rgb(var(--muted))] mb-2">
        <span className="flex items-center gap-1"><i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: BRONCE }} /> neto</span>
        <span className="flex items-center gap-1"><i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: ORO }} /> bruto</span>
        {vista === 'anual' && yoy != null && (
          <span className={`ml-auto font-medium ${yoy >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
            {yoy >= 0 ? '▲' : '▼'} {Math.abs(yoy).toFixed(0)}% interanual
          </span>
        )}
      </div>

      <Barras barras={barras} />
    </div>
  );
}

function Barras({ barras }: { barras: { etiqueta: string; bruto: number; neto: number }[] }) {
  const W = 100 * barras.length || 100;
  const H = 200;
  const PAD_T = 22, PAD_B = 22, PLOT = H - PAD_T - PAD_B;
  const max = Math.max(...barras.map((b) => b.bruto), 1);
  const paso = W / barras.length;
  const ancho = paso * 0.6;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 200 }} preserveAspectRatio="none">
      {barras.map((b, i) => {
        const x = i * paso + (paso - ancho) / 2;
        const hBruto = (b.bruto / max) * PLOT;
        const hNeto = (b.neto / max) * PLOT;
        const yBruto = PAD_T + (PLOT - hBruto);
        const yNeto = PAD_T + (PLOT - hNeto);
        return (
          <g key={i}>
            {b.bruto > 0 && (
              <>
                <rect x={x} y={yBruto} width={ancho} height={hBruto} fill={ORO} rx="2">
                  <title>{`${b.etiqueta}: ${fmtEUR(b.bruto, { maximumFractionDigits: 0 })} bruto`}</title>
                </rect>
                <rect x={x} y={yNeto} width={ancho} height={hNeto} fill={BRONCE} rx="2">
                  <title>{`${b.etiqueta}: ${fmtEUR(b.neto, { maximumFractionDigits: 0 })} neto`}</title>
                </rect>
                <text x={x + ancho / 2} y={yBruto - 4} textAnchor="middle"
                      className="fill-[rgb(var(--muted))]" style={{ fontSize: 11 }}>
                  {b.bruto >= 1000 ? `${(b.bruto / 1000).toFixed(1)}k` : Math.round(b.bruto)}
                </text>
              </>
            )}
            <text x={x + ancho / 2} y={H - 6} textAnchor="middle"
                  className="fill-[rgb(var(--muted))]" style={{ fontSize: 11 }}>
              {b.etiqueta}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
