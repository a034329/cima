'use client';

import { useState } from 'react';
import { fmtEUR, fmtNum, fmtPct, guardarColumnasPosiciones } from '@/lib/api';
import { DECISION_COLOR, DECISION_LABEL } from '@/lib/decisiones';
import { EvolucionChart } from '@/components/EvolucionChart';
import type { ColumnaCatalogo, PosicionMetricas, PosicionesResumen } from '@/lib/types';

// Columnas monetarias por acción (PM/precio) vs importes absolutos (€) vs %.
const COLS_PM = new Set(['pm_real', 'pm_fiscal_es', 'pm_desc', 'precio_actual_eur']);
const COLS_PCT = new Set([
  'gp_no_realizada_pct', 'rentab_total_pct', 'rentab_total_hist_pct', 'cagr4_div_pct',
  'umbral_rotacion_1y_pct', 'umbral_rotacion_2y_pct',
  'umbral_rotacion_3y_pct', 'umbral_rotacion_4y_pct',
]);
const COLS_GP = new Set(['gp_realizada_anio', 'gp_no_realizada_eur', 'gp_no_realizada_pct',
                          'rentab_total_pct', 'rentab_total_hist_pct',
                          'opciones_ejercidas_anio', 'opciones_ejercidas_hist',
                          'primas_opc_anio', 'primas_opc_hist']);

// Cabecera pegajosa: el panel tiene scroll propio (vertical+horizontal), por eso top-0.
const TH = 'sticky top-0 z-10 bg-[rgb(var(--card))] cursor-pointer hover:text-[rgb(var(--fg))] ' +
  'border-b border-[rgb(var(--border))] shadow-[0_1px_0_rgb(var(--border))]';

function valorColumna(p: PosicionMetricas, id: string): string {
  const valor = (p as unknown as Record<string, string | null>)[id];
  if (valor == null) return '—';   // umbrales sin plusvalía/estimación
  const raw = valor;
  // Precio en divisa local: número + divisa de cotización (GBp, USD, HKD…).
  if (id === 'precio_actual_local') {
    return `${fmtNum(raw, { maximumFractionDigits: 4 })} ${p.divisa_cotizacion ?? ''}`.trim();
  }
  if (COLS_PCT.has(id)) return fmtPct(raw, 1);
  if (COLS_PM.has(id)) return fmtEUR(raw, { maximumFractionDigits: 4 });
  return fmtEUR(raw, { maximumFractionDigits: 2 });
}

// Fila de posición + (si está abierta) fila expandida con la evolución mensual
// del valor de ESE valor. La gráfica solo se monta cuando se abre (lazy fetch).
function FragmentoFila({ p, abierto, colSpan, children }: {
  p: PosicionMetricas;
  abierto: boolean;
  colSpan: number;
  children: React.ReactNode;
}) {
  return (
    <>
      <tr className="border-t border-[rgb(var(--border))]/30">{children}</tr>
      {abierto && (
        <tr className="bg-[rgb(var(--bg))]/40">
          <td colSpan={colSpan} className="p-3">
            <div className="font-sans">
              <EvolucionChart isin={p.isin} titulo={`Evolución · ${p.nombre}`} />
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function PosicionesEnriquecidas({ data, onData }: {
  data: PosicionesResumen;
  onData: (d: PosicionesResumen) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [guardando, setGuardando] = useState(false);
  const [abrePanel, setAbrePanel] = useState(false);
  const [expandido, setExpandido] = useState<string | null>(null);   // ISIN con la gráfica abierta
  const [filtro, setFiltro] = useState('');
  const [orden, setOrden] = useState<{ key: string; dir: 1 | -1 }>({
    key: 'gp_no_realizada_eur', dir: -1,
  });

  const ordenarPor = (key: string) =>
    setOrden((o) => (o.key === key ? { key, dir: (o.dir === 1 ? -1 : 1) } : { key, dir: -1 }));

  async function toggle(id: string) {
    const sel = new Set(data.columnas_seleccionadas);
    if (sel.has(id)) sel.delete(id);
    else sel.add(id);
    sel.add('pm_real'); // siempre fija
    setGuardando(true);
    try {
      onData(await guardarColumnasPosiciones([...sel]));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGuardando(false);
    }
  }

  // Solo acciones y ETFs aquí; cripto y opciones tienen su propia sección.
  const securities = data.posiciones.filter((p) => p.tipo_activo !== 'CRYPTO');
  if (securities.length === 0) {
    return (
      <p className="text-sm text-[rgb(var(--muted))]">
        Sin acciones ni ETFs. Importa un extracto para empezar.
      </p>
    );
  }
  if (error) {
    return (
      <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4">
        <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
      </div>
    );
  }

  const catalogo = data.columnas_catalogo;
  const sel = data.columnas_seleccionadas;
  const colsMostradas = catalogo.filter((c) => sel.includes(c.id));

  const valorOrden = (p: PosicionMetricas, key: string): number | string => {
    if (key === 'nombre') return (p.nombre || p.isin).toLowerCase();
    if (key === 'decision') return p.decision;
    return parseFloat((p as unknown as Record<string, string>)[key] ?? '0');
  };
  const seguridadesFiltradas = filtro
    ? securities.filter((p) => (p.nombre || '').toLowerCase().includes(filtro.toLowerCase()))
    : securities;
  const filas = [...seguridadesFiltradas].sort((a, b) => {
    const va = valorOrden(a, orden.key);
    const vb = valorOrden(b, orden.key);
    if (va < vb) return -1 * orden.dir;
    if (va > vb) return 1 * orden.dir;
    return 0;
  });
  const flecha = (key: string) => (orden.key === key ? (orden.dir === 1 ? ' ▲' : ' ▼') : '');

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold">
          Acciones y ETFs ({filtro ? `${seguridadesFiltradas.length}/${securities.length}` : securities.length})
          <span className="ml-2 text-xs font-normal text-[rgb(var(--muted))]">
            métricas año {data.anio}
            {data.precios_actualizados && (
              <> · precios al {new Date(data.precios_actualizados).toLocaleString('es-ES', {
                day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
              })}</>
            )}
          </span>
        </h3>
        <div className="flex items-center gap-2">
          <input
            type="search"
            value={filtro}
            onChange={(e) => setFiltro(e.target.value)}
            placeholder="Filtrar por nombre…"
            className="w-56 px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
          />
        <div className="relative">
          <button
            onClick={() => setAbrePanel((v) => !v)}
            className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]"
          >
            Columnas {guardando && '…'}
          </button>
          {abrePanel && (
            <div
              className="absolute right-0 mt-1 z-20 w-64 rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] shadow-xl p-3 space-y-1"
              onMouseLeave={() => setAbrePanel(false)}
            >
              <p className="text-xs font-semibold text-[rgb(var(--muted))] mb-1">
                Mostrar columnas
              </p>
              {catalogo.map((c: ColumnaCatalogo) => (
                <label
                  key={c.id}
                  className={`flex items-center gap-2 text-sm py-0.5 ${
                    c.fija ? 'opacity-60' : 'cursor-pointer'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={sel.includes(c.id)}
                    disabled={c.fija || guardando}
                    onChange={() => toggle(c.id)}
                  />
                  {c.label}
                  {c.fija && <span className="text-[10px] text-[rgb(var(--muted))]">(fija)</span>}
                </label>
              ))}
            </div>
          )}
        </div>
        </div>
      </div>

      <div className="overflow-auto max-h-[72vh] rounded">
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))] select-none">
            <tr className="text-left">
              <th className={`${TH} py-2 pr-2`} onClick={() => ordenarPor('nombre')}>Nombre{flecha('nombre')}</th>
              <th className={`${TH} pr-2 text-right`} onClick={() => ordenarPor('cantidad')}>Cantidad{flecha('cantidad')}</th>
              <th className={`${TH} pr-2`} onClick={() => ordenarPor('decision')}>Decisión{flecha('decision')}</th>
              {colsMostradas.map((c) => (
                <th key={c.id} className={`${TH} pr-2 text-right`}
                    onClick={() => ordenarPor(c.id)}>{c.label}{flecha(c.id)}</th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono">
            {filas.map((p) => (
              <FragmentoFila key={p.isin}
                p={p}
                abierto={expandido === p.isin}
                colSpan={3 + colsMostradas.length}
              >
                <td className="py-1 pr-2">
                  <button type="button"
                    onClick={() => setExpandido((cur) => (cur === p.isin ? null : p.isin))}
                    className="font-sans font-medium flex items-center gap-1.5 text-left hover:text-brand-600 transition-colors"
                    title="Ver evolución mensual">
                    <span className="text-[rgb(var(--muted))] text-[10px]">{expandido === p.isin ? '▾' : '▸'}</span>
                    {p.nombre}
                    {p.tipo_activo === 'ETF' && (
                      <span className="text-[9px] px-1 py-0.5 rounded bg-[rgb(var(--bg))] border border-[rgb(var(--border))] text-[rgb(var(--muted))]">ETF</span>
                    )}
                  </button>
                  <div className="text-[10px] text-[rgb(var(--muted))]">{p.isin}</div>
                </td>
                <td className="pr-2 text-right">{fmtNum(p.cantidad)}</td>
                <td className="pr-2">
                  <span className={`font-sans text-[10px] px-1.5 py-0.5 rounded ${DECISION_COLOR[p.decision]}`}>
                    {DECISION_LABEL[p.decision]}
                  </span>
                </td>
                {colsMostradas.map((c) => {
                  const raw = (p as unknown as Record<string, string>)[c.id] ?? '0';
                  const num = parseFloat(raw);
                  let tono = '';
                  if (c.id === 'importe_diferido_2m' && num > 0) {
                    tono = 'text-amber-700 dark:text-amber-400';
                  } else if (COLS_GP.has(c.id) && num !== 0) {
                    tono = num >= 0
                      ? 'text-emerald-700 dark:text-emerald-400'
                      : 'text-rose-700 dark:text-rose-400';
                  } else if (
                    (c.id.startsWith('dividendos') || c.id.startsWith('opciones')) && num !== 0
                  ) {
                    tono = 'text-emerald-700 dark:text-emerald-400';
                  }
                  return (
                    <td key={c.id} className={`pr-2 text-right ${tono}`}>
                      {valorColumna(p, c.id)}
                    </td>
                  );
                })}
              </FragmentoFila>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
