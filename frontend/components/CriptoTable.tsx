'use client';

import { fmtEUR, fmtNum, fmtPct } from '@/lib/api';
import type { PosicionMetricas } from '@/lib/types';

// Cripto: sin ISIN real, sin dividendos ni yield. Tabla simple.
export function CriptoTable({ posiciones }: { posiciones: PosicionMetricas[] }) {
  if (posiciones.length === 0) return null;
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <h3 className="font-semibold mb-3">Cripto ({posiciones.length})</h3>
      <div className="overflow-auto rounded">
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Nombre</th>
              <th className="pr-2 text-right">Cantidad</th>
              <th className="pr-2 text-right">Precio medio</th>
              <th className="pr-2 text-right">Precio actual</th>
              <th className="pr-2 text-right">G/P no realizada</th>
              <th className="pr-2 text-right">%</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {posiciones.map((p) => {
              const gp = parseFloat(p.gp_no_realizada_eur);
              const tono = gp >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400';
              return (
                <tr key={p.isin} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2 font-sans font-medium">{p.nombre}</td>
                  <td className="pr-2 text-right">{fmtNum(p.cantidad)}</td>
                  <td className="pr-2 text-right">{fmtEUR(p.pm_real, { maximumFractionDigits: 4 })}</td>
                  <td className="pr-2 text-right">{fmtEUR(p.precio_actual_eur, { maximumFractionDigits: 4 })}</td>
                  <td className={`pr-2 text-right ${tono}`}>{fmtEUR(p.gp_no_realizada_eur, { maximumFractionDigits: 2 })}</td>
                  <td className={`pr-2 text-right ${tono}`}>{fmtPct(p.gp_no_realizada_pct, 1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
