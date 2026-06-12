'use client';

import { useCallback, useEffect, useState } from 'react';
import { fetchRotacion, fmtEUR, fmtPct } from '@/lib/api';
import type { RotacionFiscal, RotacionItem } from '@/lib/types';

const ANO_ACTUAL = new Date().getFullYear();
const ANOS = [ANO_ACTUAL, ANO_ACTUAL - 1, ANO_ACTUAL - 2];

export default function RotacionPage() {
  const [ejercicio, setEjercicio] = useState(ANO_ACTUAL);
  const [data, setData] = useState<RotacionFiscal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const cargar = useCallback(async () => {
    setCargando(true);
    setError(null);
    try {
      setData(await fetchRotacion(ejercicio));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCargando(false);
    }
  }, [ejercicio]);

  useEffect(() => {
    cargar();
  }, [cargar]);

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <p className="text-sm text-[rgb(var(--muted))] max-w-3xl">
          ¿Merece la pena rotar? Si vendes una posición con plusvalía para cambiarla por otra empresa,
          aflorar esa ganancia paga impuestos. Estos son los <strong>umbrales de rentabilidad</strong>{' '}
          (CAGR4+Div) que el destino debe batir para que rotar compense ese coste, según cuántos años
          mantengas la nueva posición. Por debajo del umbral, la rotación destruye valor:{' '}
          <em>mejor mantener</em>.
        </p>
        <div className="flex items-center gap-2">
          <label className="text-xs text-[rgb(var(--muted))]">Base fiscal</label>
          <select
            value={ejercicio}
            onChange={(e) => setEjercicio(parseInt(e.target.value, 10))}
            className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
          >
            {ANOS.map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
          <button
            onClick={cargar}
            disabled={cargando}
            className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))] disabled:opacity-50"
          >
            {cargando ? 'Calculando…' : 'Recalcular'}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}
      {cargando && !data && (
        <p className="text-sm text-[rgb(var(--muted))]">
          Obteniendo precios y estimaciones actuales (puede tardar la primera vez)…
        </p>
      )}

      {data && <Contenido d={data} />}
    </div>
  );
}

function Contenido({ d }: { d: RotacionFiscal }) {
  const conPlusvalia = d.items.filter((it) => parseFloat(it.gp_latente_eur) > 0);
  return (
    <>
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3 text-sm text-[rgb(var(--muted))]">
        Base del ahorro de partida ({d.ejercicio}):{' '}
        <strong className="text-[rgb(var(--fg))]">
          {fmtEUR(d.base_ahorro_actual_eur, { maximumFractionDigits: 0 })}
        </strong>
        . El coste fiscal de cada venta es <em>marginal</em>: el impuesto extra que añadiría aflorar
        la plusvalía sobre esta base ya acumulada (tramos 19 % → 28 %), no un tipo plano.
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-1">Umbral de rotación por posición</h3>
        <p className="text-xs text-[rgb(var(--muted))] mb-3">
          Con plusvalía, el impuesto a pagar sube el umbral por encima del retorno esperado (decrece
          al alargar el horizonte). Con pérdida, vender adelanta un crédito fiscal: el umbral baja por
          debajo del retorno esperado y sube hacia él con los años. El «Coste fiscal» es positivo
          (a pagar) o negativo (crédito).
        </p>
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Posición</th>
              <th className="pr-2 text-right">Valor</th>
              <th className="pr-2 text-right">G/P latente</th>
              <th className="pr-2 text-right">Coste fiscal</th>
              <th className="pr-2 text-right" title="Coste fiscal / plusvalía latente">Tipo efec.</th>
              <th className="pr-2 text-right" title="CAGR4+Div esperado de la propia posición (de Estimaciones)">r origen</th>
              <th className="pr-2 text-right">Umbral 1A</th>
              <th className="pr-2 text-right">Umbral 2A</th>
              <th className="pr-2 text-right">Umbral 3A</th>
              <th className="pr-2 text-right">Umbral 4A</th>
              <th className="pr-2 text-right" title="Años que retrasa la IF pagar hoy el coste fiscal, con la proyección del dashboard">Δ años IF</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {d.items.map((it) => (
              <Fila key={it.isin} it={it} />
            ))}
          </tbody>
        </table>
        {d.items.length === 0 && (
          <p className="text-sm text-[rgb(var(--muted))]">Sin posiciones abiertas con precio.</p>
        )}
        {conPlusvalia.length > 0 && (
          <p className="text-xs text-[rgb(var(--muted))] mt-3">
            Lectura: para que rotar <strong>{conPlusvalia[0].nombre}</strong> compense en 4 años, el
            destino debe ofrecer un CAGR4+Div sostenido mayor que su «Umbral 4A». A menor horizonte,
            mayor exigencia (el coste fiscal se reparte en menos años).
          </p>
        )}
        {d.sin_estimacion.length > 0 && (
          <p className="text-xs text-[rgb(var(--muted))] mt-1">
            {d.sin_estimacion.length} posiciones sin estimación de retorno (CAGR4+Div) — no se puede
            calcular su umbral. Complétalas en Estimaciones.
          </p>
        )}
      </div>
    </>
  );
}

function Fila({ it }: { it: RotacionItem }) {
  const g = parseFloat(it.gp_latente_eur);
  const perdida = g < 0;
  return (
    <tr className="border-t border-[rgb(var(--border))]/30">
      <td className="py-1 pr-2 font-sans">{it.nombre}</td>
      <td className="pr-2 text-right">{fmtEUR(it.valor_eur, { maximumFractionDigits: 0 })}</td>
      <td className={`pr-2 text-right ${perdida ? 'text-rose-700 dark:text-rose-400' : 'text-emerald-700 dark:text-emerald-400'}`}>
        {fmtEUR(it.gp_latente_eur, { maximumFractionDigits: 0 })}
      </td>
      <td className="pr-2 text-right">{fmtEUR(it.coste_fiscal_eur, { maximumFractionDigits: 0 })}</td>
      <td className="pr-2 text-right text-[rgb(var(--muted))]">
        {it.tipo_efectivo_pct == null ? '—' : fmtPct(it.tipo_efectivo_pct, 1)}
      </td>
      <td className="pr-2 text-right">{cell(it.cagr4_div_origen_pct)}</td>
      <td className="pr-2 text-right">{cell(it.umbral_1y_pct)}</td>
      <td className="pr-2 text-right">{cell(it.umbral_2y_pct)}</td>
      <td className="pr-2 text-right">{cell(it.umbral_3y_pct)}</td>
      <td className="pr-2 text-right font-medium">{cell(it.umbral_4y_pct)}</td>
      <td className="pr-2 text-right text-[rgb(var(--muted))]">
        {it.delta_anios_if == null ? '—'
          : parseFloat(it.delta_anios_if) === 0 ? '0'
            : `${parseFloat(it.delta_anios_if) > 0 ? '+' : ''}${it.delta_anios_if} a`}
      </td>
    </tr>
  );
}

function cell(v: string | null) {
  return v == null ? <span className="text-[rgb(var(--muted))]">—</span> : fmtPct(v, 1);
}
