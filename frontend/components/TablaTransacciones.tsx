import type { TransaccionOut } from '@/lib/types';
import { fmtEUR, fmtNum } from '@/lib/api';
import { RestaurarTransaccionBtn } from '@/components/RestaurarTransaccionBtn';

interface Props {
  transacciones: TransaccionOut[];
}

const TIPO_LABEL: Record<string, string> = {
  BUY: 'Compra',
  SELL: 'Venta',
  DIVIDEND: 'Dividendo',
  INTEREST: 'Interés',
  STAKING_REWARD: 'Staking',
  CORPORATE_SPLIT: 'Split',
  CORPORATE_ISIN_CHANGE: 'Cambio ISIN',
  CORPORATE_SCRIP: 'Scrip',
  CORPORATE_RIGHTS: 'Derechos',
  CORPORATE_MERGER: 'Fusión',
  CORPORATE_OPA: 'OPA',
  OTRO: 'Otro',
};

const TIPO_COLOR: Record<string, string> = {
  BUY: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  SELL: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300',
  DIVIDEND: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  INTEREST: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  STAKING_REWARD: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
};

const ESTADO_LABEL: Record<string, string> = {
  pendiente_confirmar: 'Pendiente',
  confirmada: 'Confirmada',
  descartada: 'Descartada',
};

const ESTADO_COLOR: Record<string, string> = {
  pendiente_confirmar: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  confirmada: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  descartada: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
};

export function TablaTransacciones({ transacciones }: Props) {
  if (transacciones.length === 0) {
    return (
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
        <p className="text-sm text-[rgb(var(--muted))]">
          No hay transacciones todavía. Importa un extracto o añade una operación manual.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-[rgb(var(--bg))] border-b border-[rgb(var(--border))]">
            <tr className="text-left">
              <th className="px-3 py-2 font-medium">Fecha</th>
              <th className="px-3 py-2 font-medium">Tipo</th>
              <th className="px-3 py-2 font-medium">ISIN</th>
              <th className="px-3 py-2 font-medium text-right">Cantidad</th>
              <th className="px-3 py-2 font-medium text-right">Precio</th>
              <th className="px-3 py-2 font-medium text-right">Importe €</th>
              <th className="px-3 py-2 font-medium text-right">Gastos €</th>
              <th className="px-3 py-2 font-medium text-right">Ret. €</th>
              <th className="px-3 py-2 font-medium">Estado</th>
              <th className="px-3 py-2 font-medium">Origen</th>
              <th className="px-3 py-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {transacciones.map((t) => (
              <tr
                key={t.id}
                className="border-b border-[rgb(var(--border))] last:border-0 hover:bg-[rgb(var(--bg))]"
              >
                <td className="px-3 py-2 font-mono text-xs">{t.fecha}</td>
                <td className="px-3 py-2">
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded ${TIPO_COLOR[t.tipo] || 'bg-slate-100 text-slate-700'}`}
                  >
                    {TIPO_LABEL[t.tipo] || t.tipo}
                  </span>
                </td>
                <td className="px-3 py-2 font-mono text-xs" title={t.posicion_nombre ?? undefined}>{t.isin ?? '—'}</td>
                <td className="px-3 py-2 text-right font-mono text-xs">
                  {parseFloat(t.cantidad) > 0 ? fmtNum(t.cantidad) : '—'}
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs">
                  {parseFloat(t.precio_local) > 0
                    ? `${fmtNum(t.precio_local, { maximumFractionDigits: 4 })} ${t.divisa_local}`
                    : '—'}
                </td>
                <td className="px-3 py-2 text-right font-mono">
                  {fmtEUR(t.importe_eur, { maximumFractionDigits: 2 })}
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs text-[rgb(var(--muted))]">
                  {parseFloat(t.gastos_eur) > 0 ? fmtEUR(t.gastos_eur, { maximumFractionDigits: 2 }) : ''}
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs text-[rgb(var(--muted))]">
                  {parseFloat(t.retencion_eur) > 0
                    ? fmtEUR(t.retencion_eur, { maximumFractionDigits: 2 })
                    : ''}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded ${ESTADO_COLOR[t.estado] || ''}`}
                  >
                    {ESTADO_LABEL[t.estado] || t.estado}
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-[rgb(var(--muted))] font-mono">
                  {t.origen}
                </td>
                <td className="px-3 py-2">
                  {t.estado === 'descartada' && <RestaurarTransaccionBtn txId={t.id} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
