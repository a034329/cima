'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchDiversificacionDividendos, fetchSerieDividendos, fmtEUR } from '@/lib/api';
import { onDatosActualizados } from '@/lib/refetch';
import type { DiversificacionDividendos as Div, TrozoDiv } from '@/lib/types';

// Paleta de marca (cálida) para los donuts.
const PALETA = ['#E6B763', '#B8860B', '#5B9279', '#C2693B', '#7E8AA2',
                '#8C8577', '#A8743A', '#6B8E9E', '#9C8AA5', '#57514A'];

export function DiversificacionDividendos() {
  const [anios, setAnios] = useState<number[]>([]);
  // Arranca en el año en curso para que etiqueta y datos coincidan desde el
  // primer render (evita la carrera etiqueta-2026/datos-2025). null = "Todo".
  const [anioSel, setAnioSel] = useState<number | null>(new Date().getFullYear());
  const [data, setData] = useState<Div | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  // Años disponibles (de la serie). NO toca anioSel (ya inicializado).
  const cargarAnios = useCallback(() => {
    fetchSerieDividendos()
      .then((s) => setAnios(s.anual.map((p) => p.anio).sort((a, b) => b - a)))
      .catch(() => {});
  }, []);

  const reqId = useRef(0);
  const cargar = useCallback(() => {
    const id = ++reqId.current;
    setCargando(true);
    fetchDiversificacionDividendos(anioSel ?? undefined)
      .then((d) => { if (id === reqId.current) { setData(d); setError(null); } })
      .catch((e) => { if (id === reqId.current) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (id === reqId.current) setCargando(false); });
  }, [anioSel]);

  useEffect(() => { cargarAnios(); return onDatosActualizados(cargarAnios); }, [cargarAnios]);
  useEffect(() => { cargar(); }, [cargar]);

  if (error) return <p className="text-sm text-rose-600">{error}</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <h3 className="text-lg font-semibold mr-auto">Diversificación de la renta</h3>
        <div className="flex rounded border border-[rgb(var(--border))] overflow-hidden text-xs">
          <button onClick={() => setAnioSel(null)}
            className={`px-2.5 py-1 ${anioSel === null ? 'bg-brand-600 text-white' : 'hover:bg-[rgb(var(--bg))]'}`}>
            Todo
          </button>
          {anios.map((y) => (
            <button key={y} onClick={() => setAnioSel(y)}
              className={`px-2.5 py-1 border-l border-[rgb(var(--border))] ${anioSel === y ? 'bg-brand-600 text-white' : 'hover:bg-[rgb(var(--bg))]'}`}>
              {y}
            </button>
          ))}
        </div>
      </div>

      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <Panel titulo="Por empresa" nota="concentración de tu renta">
            <RankBars trozos={data.por_empresa} total={parseFloat(data.bruto_total)} cargando={cargando} />
          </Panel>
          <Panel titulo="Por sector">
            <DonutLeyenda trozos={data.por_sector} total={parseFloat(data.bruto_total)} />
          </Panel>
          <Panel titulo="Por país / divisa">
            <DonutLeyenda trozos={data.por_pais} total={parseFloat(data.bruto_total)} />
          </Panel>
        </div>
      )}
    </div>
  );
}

function Panel({ titulo, nota, children }: { titulo: string; nota?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="mb-3">
        <h4 className="font-semibold">{titulo}</h4>
        {nota && <p className="text-xs text-[rgb(var(--muted))]">{nota}</p>}
      </div>
      {children}
    </div>
  );
}

function RankBars({ trozos, total, cargando }: { trozos: TrozoDiv[]; total: number; cargando: boolean }) {
  const TOP = 12;
  const top = trozos.slice(0, TOP);
  const resto = trozos.slice(TOP).reduce((s, t) => s + parseFloat(t.bruto), 0);
  const filas = resto > 0 ? [...top, { clave: `Otras (${trozos.length - TOP})`, bruto: String(resto) }] : top;
  const max = filas.length ? Math.max(...filas.map((t) => parseFloat(t.bruto))) : 1;
  if (!trozos.length) return <p className="text-sm text-[rgb(var(--muted))]">{cargando ? 'Calculando…' : 'Sin dividendos en el periodo.'}</p>;
  return (
    <div className="space-y-1.5">
      {filas.map((t) => {
        const v = parseFloat(t.bruto);
        const pct = total > 0 ? (v / total) * 100 : 0;
        return (
          <div key={t.clave} className="text-xs">
            <div className="flex justify-between gap-2 mb-0.5">
              <span className="truncate">{t.clave}</span>
              <span className="text-[rgb(var(--muted))] tabular-nums shrink-0">
                {fmtEUR(v, { maximumFractionDigits: 0 })} · {pct.toFixed(0)}%
              </span>
            </div>
            <div className="h-2 bg-[rgb(var(--border))] rounded overflow-hidden">
              <div className="h-full rounded" style={{ width: `${(v / max) * 100}%`, background: '#E6B763' }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DonutLeyenda({ trozos, total }: { trozos: TrozoDiv[]; total: number }) {
  if (!trozos.length) return <p className="text-sm text-[rgb(var(--muted))]">Sin datos.</p>;
  const r = 52, c = 2 * Math.PI * r;
  let offset = 0;
  const segs = trozos.map((t, i) => ({
    clave: t.clave, valor: parseFloat(t.bruto), color: PALETA[i % PALETA.length],
  }));
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 140 140" className="w-28 h-28 shrink-0">
        <g transform="rotate(-90 70 70)">
          <circle cx="70" cy="70" r={r} fill="none" stroke="rgb(var(--border))" strokeWidth="16" />
          {segs.map((s, i) => {
            const len = (s.valor / (total || 1)) * c;
            const el = (
              <circle key={i} cx="70" cy="70" r={r} fill="none" stroke={s.color} strokeWidth="16"
                strokeDasharray={`${len} ${c - len}`} strokeDashoffset={-offset} />
            );
            offset += len;
            return el;
          })}
        </g>
      </svg>
      <ul className="text-xs space-y-1 min-w-0">
        {segs.slice(0, 7).map((s) => (
          <li key={s.clave} className="flex items-center gap-1.5">
            <i className="inline-block w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: s.color }} />
            <span className="truncate">{s.clave}</span>
            <span className="ml-auto text-[rgb(var(--muted))] tabular-nums shrink-0">
              {total > 0 ? ((s.valor / total) * 100).toFixed(0) : 0}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
