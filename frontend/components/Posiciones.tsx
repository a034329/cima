import type { PosicionResumen } from '@/lib/types';
import { fmtEUR, fmtNum } from '@/lib/api';

interface Props {
  posiciones: PosicionResumen[];
}

export function Posiciones({ posiciones }: Props) {
  return (
    <section>
      <h3 className="text-lg font-semibold mb-4">Posiciones</h3>
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[rgb(var(--bg))] border-b border-[rgb(var(--border))]">
              <tr className="text-left">
                <th className="px-4 py-3 font-medium">Posición</th>
                <th className="px-4 py-3 font-medium">Bloque</th>
                <th className="px-4 py-3 font-medium text-right">Cantidad</th>
                <th className="px-4 py-3 font-medium text-right" title="Precio medio real (FIFO/PEPS)">
                  PM real
                </th>
                <th
                  className="px-4 py-3 font-medium text-right"
                  title="Precio medio fiscal español (con opciones ejercidas reduciendo coste)"
                >
                  PM fiscal ES
                </th>
                <th
                  className="px-4 py-3 font-medium text-right"
                  title="PM con todas las primas de opciones cobradas descontadas"
                >
                  PM opc. total
                </th>
                <th className="px-4 py-3 font-medium text-right">Valor</th>
                <th className="px-4 py-3 font-medium text-right">P/L latente</th>
              </tr>
            </thead>
            <tbody>
              {posiciones.map((p) => {
                const plusvalia = parseFloat(p.plusvalia_latente_eur);
                return (
                  <tr
                    key={p.isin}
                    className="border-b border-[rgb(var(--border))] last:border-0 hover:bg-[rgb(var(--bg))]"
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium">{p.nombre}</div>
                      <div className="text-xs text-[rgb(var(--muted))] font-mono">
                        {p.ticker} · {p.isin} · {p.divisa_local}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-xs text-[rgb(var(--muted))]">
                        {p.bloque || '—'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {fmtNum(p.cantidad)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {fmtEUR(p.pm_real_eur, { maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {fmtEUR(p.pm_fiscal_es_eur, { maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {fmtEUR(p.pm_opciones_total_eur, { maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-4 py-3 text-right font-mono font-medium">
                      {fmtEUR(p.valor_eur)}
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-mono font-medium ${
                        plusvalia > 0
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : plusvalia < 0
                            ? 'text-rose-600 dark:text-rose-400'
                            : ''
                      }`}
                    >
                      {plusvalia > 0 ? '+' : ''}
                      {fmtEUR(p.plusvalia_latente_eur)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
