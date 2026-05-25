'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  editarEstimacion,
  fetchEstimaciones,
  fmtEUR,
  fmtNum,
  fmtPct,
  prefillEstimaciones,
} from '@/lib/api';
import type { EstimacionItem, EstimacionesResumen, TipoVal } from '@/lib/types';

// Precio en su DIVISA NATIVA (las estimaciones son agnósticas de divisa): €
// para EUR, código (USD/GBp/CHF…) para el resto. NO formatear todo como €.
function precioNativo(v: string | null, divisa: string | null): string {
  if (!v) return '—';
  if (!divisa || divisa === 'EUR') return fmtEUR(v, { maximumFractionDigits: 2 });
  return `${fmtNum(v, { maximumFractionDigits: 2 })} ${divisa}`;
}

const TIPO_LABEL: Record<TipoVal, string> = {
  PER: 'PER', P_FCF: 'P/FCF', P_BV: 'P/BV', P_FRE: 'P/FRE',
};
const TIPOS: TipoVal[] = ['PER', 'P_FCF', 'P_BV', 'P_FRE'];

const TH = 'sticky top-0 z-10 bg-[rgb(var(--card))] border-b border-[rgb(var(--border))] ' +
  'shadow-[0_1px_0_rgb(var(--border))]';

export default function EstimacionesPage() {
  const [data, setData] = useState<EstimacionesResumen | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const cargar = useCallback(async () => {
    try {
      setData(await fetchEstimaciones());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    cargar();
  }, [cargar]);

  const autoRellenar = async () => {
    setCargando(true);
    try {
      await prefillEstimaciones();
      await cargar();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCargando(false);
    }
  };

  const guardar = async (isin: string, campos: Record<string, unknown>) => {
    await editarEstimacion(isin, campos);
    await cargar();
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <p className="text-sm text-[rgb(var(--muted))] max-w-2xl">
          Valoración por valor (modelo WG): precio objetivo = múltiplo × métrica base 4A.
          Por defecto el múltiplo es el <strong>PER forward de consenso</strong> (precio
          objetivo de analistas, el menor entre media y mediana ÷ EPS forward) y la métrica
          4A es el <strong>EPS de consenso a ~4 años</strong>. El consenso (en gris) es
          referencia; tú ajustas múltiplo y métrica a tu criterio.
        </p>
        <button
          onClick={autoRellenar}
          disabled={cargando}
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {cargando ? 'Rellenando…' : 'Auto-rellenar del feed'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {data && (
        <div className="grid grid-cols-3 gap-3">
          <Card label="Yield estimado" value={data.yield_estimado_pct ? fmtPct(data.yield_estimado_pct, 2) : '—'} />
          <Card label="CAGR4 + Div ponderado" value={data.cagr4_div_ponderado_pct ? fmtPct(data.cagr4_div_ponderado_pct, 1) : '—'} destacado />
          <Card label="Cobertura" value={fmtPct(data.cobertura, 0)} sub="del valor con estimación" />
        </div>
      )}

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-auto max-h-[72vh]">
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left">
              <th className={`${TH} py-2 px-2`}>Valor</th>
              <th className={`${TH} px-2`}>Tipo</th>
              <th className={`${TH} px-2 text-right`}>Precio</th>
              <th className={`${TH} px-2 text-right`}>Métrica act.</th>
              <th className={`${TH} px-2 text-right`}>Múltiplo obj.</th>
              <th className={`${TH} px-2 text-right`}>Métrica 4A</th>
              <th className={`${TH} px-2 text-right`}>Div/acc</th>
              <th className={`${TH} px-2 text-right`}>Precio obj.</th>
              <th className={`${TH} px-2 text-right`}>Crec.</th>
              <th className={`${TH} px-2 text-right`}>CAGR4</th>
              <th className={`${TH} px-2 text-right`}>Yield</th>
              <th className={`${TH} px-2 text-right`}>CAGR4+Div</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {data?.estimaciones.map((e) => <Fila key={e.isin} e={e} onGuardar={guardar} />)}
          </tbody>
        </table>
      </div>
      {!data && !error && <p className="text-sm text-[rgb(var(--muted))]">Cargando…</p>}
    </div>
  );
}

function Fila({ e, onGuardar }: {
  e: EstimacionItem;
  onGuardar: (isin: string, campos: Record<string, unknown>) => Promise<void>;
}) {
  const pct = (v: string | null, dp = 1) =>
    v == null ? '—' : fmtPct(v, dp);
  const colorPct = (v: string | null) =>
    v == null ? 'text-[rgb(var(--muted))]'
      : parseFloat(v) >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400';

  return (
    <tr className="border-t border-[rgb(var(--border))]/30 [&>td]:align-top [&>td]:py-2">
      <td className="px-2 font-sans max-w-[180px] truncate">{e.nombre}</td>
      <td className="px-2">
        <select
          value={e.tipo_val}
          onChange={(ev) => onGuardar(e.isin, { tipo_val: ev.target.value })}
          className="bg-[rgb(var(--bg))] border border-[rgb(var(--border))] rounded px-1 py-0.5"
        >
          {TIPOS.map((t) => <option key={t} value={t}>{TIPO_LABEL[t]}</option>)}
        </select>
      </td>
      <td className="px-2 text-right text-[rgb(var(--muted))]">
        {precioNativo(e.precio_actual, e.divisa)}
      </td>
      <EditNum isin={e.isin} campo="eps_actual" valor={e.eps_actual} onGuardar={onGuardar} />
      <EditNum isin={e.isin} campo="multiplo_objetivo" valor={e.multiplo_objetivo} onGuardar={onGuardar}
        hint={hintMultiplo(e)} alerta={e.mult_alerta} />
      <EditNum isin={e.isin} campo="metrica_base_4y" valor={e.metrica_base_4y} onGuardar={onGuardar}
        hint={hintMetrica(e)} />
      <EditNum isin={e.isin} campo="dividendo_share" valor={e.dividendo_share} onGuardar={onGuardar} />
      <td className="px-2 text-right">{precioNativo(e.precio_objetivo, e.divisa)}</td>
      <td className="px-2 text-right text-[rgb(var(--muted))]">{pct(e.crecimiento_pct)}</td>
      <td className={`px-2 text-right ${colorPct(e.cagr4_pct)}`}>{pct(e.cagr4_pct)}</td>
      <td className="px-2 text-right text-[rgb(var(--muted))]">{pct(e.div_yield_pct, 2)}</td>
      <td className={`px-2 text-right font-semibold ${colorPct(e.cagr4_div_pct)}`}>{pct(e.cagr4_div_pct)}</td>
    </tr>
  );
}

function EditNum({ isin, campo, valor, onGuardar, hint, alerta }: {
  isin: string; campo: string; valor: string | null;
  onGuardar: (isin: string, campos: Record<string, unknown>) => Promise<void>;
  hint?: string | null;
  alerta?: string | null;
}) {
  const [v, setV] = useState(valor ?? '');
  useEffect(() => { setV(valor ?? ''); }, [valor]);
  return (
    <td className="px-2 text-right align-top">
      <div className="flex items-center justify-end gap-1">
        {alerta && (
          <span
            role="img"
            aria-label={`Aviso: ${alerta}`}
            title={alerta}
            className="cursor-help text-amber-500 dark:text-amber-400 text-sm leading-none select-none"
          >
            ⚠️
          </span>
        )}
        <input
          value={v}
          onChange={(e) => setV(e.target.value)}
          onBlur={() => {
            const n = v.trim() ? parseFloat(v.replace(',', '.')) : null;
            const actual = valor != null ? parseFloat(valor) : null;
            if (n !== actual) onGuardar(isin, { [campo]: n });
          }}
          inputMode="decimal"
          className="w-16 text-right bg-[rgb(var(--bg))] border border-[rgb(var(--border))] rounded px-1 py-0.5"
        />
      </div>
      {hint && <div className="text-[10px] text-[rgb(var(--muted))] mt-0.5 whitespace-nowrap">{hint}</div>}
    </td>
  );
}

const num = (v: string | null, dp = 1): string | null =>
  v == null ? null : parseFloat(v).toLocaleString('es-ES', { minimumFractionDigits: dp, maximumFractionDigits: dp });

// Pista bajo el múltiplo: PER forward de consenso (target menor ÷ EPS fwd) + PER histórico.
function hintMultiplo(e: EstimacionItem): string | null {
  const partes: string[] = [];
  const tgt = e.precio_obj_consenso ? parseFloat(e.precio_obj_consenso) : null;
  const fwd = e.eps_forward ? parseFloat(e.eps_forward) : null;
  if (tgt != null && fwd && fwd > 0) partes.push(`cons ${(tgt / fwd).toLocaleString('es-ES', { maximumFractionDigits: 1 })}`);
  const hist = e.per_hist_mediano ?? e.per_hist_medio;
  if (hist) partes.push(`hist ${num(hist)}`);
  return partes.length ? partes.join(' · ') : null;
}

// Pista bajo la métrica 4A: EPS de consenso a 4 años + rango + nº analistas + año.
function hintMetrica(e: EstimacionItem): string | null {
  if (e.eps_consenso_4y == null) return null;
  let s = `cons ${num(e.eps_consenso_4y, 2)}`;
  const lo = num(e.eps_consenso_low, 1), hi = num(e.eps_consenso_high, 1);
  if (lo && hi) s += ` (${lo}–${hi})`;
  const meta: string[] = [];
  if (e.num_analistas_eps) meta.push(`${e.num_analistas_eps} an.`);
  if (e.anio_consenso_4y) meta.push(`'${String(e.anio_consenso_4y).slice(2)}`);
  if (meta.length) s += ` ${meta.join(' ')}`;
  return s;
}

function Card({ label, value, sub, destacado }: { label: string; value: string; sub?: string; destacado?: boolean }) {
  return (
    <div className={`rounded-lg border p-4 ${destacado
      ? 'border-brand-400 bg-brand-50/30 dark:bg-brand-900/10'
      : 'border-[rgb(var(--border))] bg-[rgb(var(--card))]'}`}>
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className="text-2xl font-semibold mt-1 text-emerald-700 dark:text-emerald-400">{value}</div>
      {sub && <div className="text-xs text-[rgb(var(--muted))] mt-1">{sub}</div>}
    </div>
  );
}
