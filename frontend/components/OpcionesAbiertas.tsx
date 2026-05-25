'use client';

import { fmtEUR, fmtNum, fmtPct } from '@/lib/api';
import type { OpcionAbierta } from '@/lib/types';

// Opciones abiertas vivas (esquema propio: contratos, no holdings con PM).
export function OpcionesAbiertas({ opciones }: { opciones: OpcionAbierta[] }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <h3 className="font-semibold mb-3">Opciones abiertas ({opciones.length})</h3>
      {opciones.length === 0 ? (
        <p className="text-sm text-[rgb(var(--muted))]">
          Sin opciones vivas. Las vencidas/ejercidas se ven en Fiscalidad → Opciones.
        </p>
      ) : (
        <div className="overflow-auto rounded">
          <table className="w-full text-xs">
            <thead className="text-[rgb(var(--muted))]">
              <tr className="text-left border-b border-[rgb(var(--border))]">
                <th className="py-2 pr-2">Subyacente</th>
                <th className="pr-2">Tipo</th>
                <th className="pr-2 text-right">Strike</th>
                <th className="pr-2 text-right" title="Precio actual del subyacente">Subyacente</th>
                <th className="pr-2 text-right">Contratos</th>
                <th className="pr-2 text-right">Prima neta</th>
                <th className="pr-2 text-right" title="Estimación por valor intrínseco (sin valor temporal)">G/P est.</th>
                <th className="pr-2">Vence</th>
                <th className="pr-2 text-right">Días</th>
                <th className="pr-2">Moneyness</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {opciones.map((o, i) => (
                <tr key={i} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2 font-sans font-medium">{o.subyacente}</td>
                  <td className="pr-2">
                    <span className={`font-sans text-[10px] px-1.5 py-0.5 rounded ${
                      o.es_corta ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                        : 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400'
                    }`}>
                      {o.es_corta ? 'Vendida' : 'Comprada'} {o.tipo_op === 'C' ? 'CALL' : o.tipo_op === 'P' ? 'PUT' : o.tipo_op}
                    </span>
                  </td>
                  <td className="pr-2 text-right">{o.strike}</td>
                  <td className="pr-2 text-right text-[rgb(var(--muted))]">
                    {o.precio_subyacente == null ? '—'
                      : `${fmtNum(o.precio_subyacente, { maximumFractionDigits: 2 })} ${o.divisa_subyacente ?? ''}`.trim()}
                  </td>
                  <td className="pr-2 text-right">{o.contratos}</td>
                  <td className={`pr-2 text-right ${parseFloat(o.prima_neta_eur) >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}>
                    {fmtEUR(o.prima_neta_eur, { maximumFractionDigits: 2 })}
                  </td>
                  <td className={`pr-2 text-right ${
                    o.gp_estimada_eur == null ? 'text-[rgb(var(--muted))]'
                      : parseFloat(o.gp_estimada_eur) >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'
                  }`}>
                    {o.gp_estimada_eur == null ? '—'
                      : `${fmtEUR(o.gp_estimada_eur, { maximumFractionDigits: 0 })}${o.gp_estimada_pct != null ? ` · ${fmtPct(o.gp_estimada_pct, 0)}` : ''}`}
                  </td>
                  <td className="pr-2 font-sans">{o.vencimiento}</td>
                  <td className="pr-2 text-right">{o.dias_a_vencer ?? '—'}</td>
                  <td className="pr-2 font-sans">
                    {o.moneyness && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                        o.moneyness === 'ITM' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400'
                          : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
                      }`}>{o.moneyness}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-[rgb(var(--muted))] mt-3">
            <strong>Vendida + ITM</strong> próxima a vencer = riesgo de asignación. La prima neta es lo
            cobrado (vendida) o pagado (comprada). <strong>G/P est.</strong> es una estimación por valor
            intrínseco (subyacente vs strike, ×100 acciones/contrato), <em>sin valor temporal</em> — no
            hay feed del precio de la opción, así que el cierre real diferiría algo.
          </p>
        </div>
      )}
    </div>
  );
}
