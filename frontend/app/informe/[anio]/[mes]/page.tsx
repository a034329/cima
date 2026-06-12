import Link from 'next/link';
import { fetchInformeMensual, fmtEUR, fmtPct } from '@/lib/api';
import type { InformeMensual } from '@/lib/types';

const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
  'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'];

const TIPO_LABEL: Record<string, string> = {
  BUY: 'Compra', SELL: 'Venta', DIVIDEND: 'Dividendo',
};

export default async function InformeMensualPage({
  params,
}: { params: { anio: string; mes: string } }) {
  const anio = parseInt(params.anio, 10);
  const mes = parseInt(params.mes, 10);

  let data: InformeMensual | null = null;
  let error: string | null = null;
  if (!Number.isFinite(anio) || !Number.isFinite(mes) || mes < 1 || mes > 12) {
    error = `Periodo inválido: ${params.anio}/${params.mes}`;
  } else {
    try {
      data = await fetchInformeMensual(anio, mes);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  const prev = mes === 1 ? { a: anio - 1, m: 12 } : { a: anio, m: mes - 1 };
  const next = mes === 12 ? { a: anio + 1, m: 1 } : { a: anio, m: mes + 1 };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">
            Cierre de {MESES[mes - 1] ?? ''} {anio}
          </h2>
          <p className="text-sm text-[rgb(var(--muted))]">
            Flujos reales del mes + foto de la marcha hacia la IF (a hoy).
          </p>
        </div>
        <div className="flex gap-2 text-sm">
          <Link href={`/informe/${prev.a}/${prev.m}`}
            className="px-3 py-1.5 rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]">
            ← anterior
          </Link>
          <Link href={`/informe/${next.a}/${next.m}`}
            className="px-3 py-1.5 rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]">
            siguiente →
          </Link>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && <Contenido d={data} />}
    </div>
  );
}

function Contenido({ d }: { d: InformeMensual }) {
  const sinActividad =
    d.n_compras === 0 && d.n_ventas === 0
    && parseFloat(d.dividendos_bruto_eur) === 0
    && parseFloat(d.aportaciones_eur) === 0
    && parseFloat(d.intereses_eur) === 0;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card label="Aportado de bolsillo" value={fmtEUR(d.aportaciones_eur, { maximumFractionDigits: 0 })} />
        <Card label={`Compras (${d.n_compras})`} value={fmtEUR(d.compras_eur, { maximumFractionDigits: 0 })} />
        <Card label={`Ventas (${d.n_ventas})`} value={fmtEUR(d.ventas_eur, { maximumFractionDigits: 0 })} />
        <Card label="Gastos y comisiones" value={fmtEUR(d.gastos_eur, { maximumFractionDigits: 2 })} tono="muted" />
        <Card label="Dividendos netos" value={fmtEUR(d.dividendos_neto_eur, { maximumFractionDigits: 2 })}
          sub={`bruto ${fmtEUR(d.dividendos_bruto_eur, { maximumFractionDigits: 2 })} − ret. ${fmtEUR(d.dividendos_retencion_eur, { maximumFractionDigits: 2 })}`}
          tono="ok" />
        <Card label="Intereses" value={fmtEUR(d.intereses_eur, { maximumFractionDigits: 2 })} />
        <Card label="G/P realizada (FIFO)" value={fmtEUR(d.gp_realizada_eur, { maximumFractionDigits: 2 })}
          tono={parseFloat(d.gp_realizada_eur) >= 0 ? 'ok' : 'warn'} />
        <Card label="Progreso IF (hoy)"
          value={d.progreso_if_pct != null ? fmtPct(d.progreso_if_pct, 1) : '—'}
          sub={d.anios_if != null
            ? `~${parseFloat(d.anios_if).toFixed(1)} años al objetivo`
            : (d.capital_estrategia_eur != null ? 'objetivo no alcanzable con estos supuestos' : undefined)} />
      </div>

      {sinActividad && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">Sin actividad registrada este mes.</p>
        </div>
      )}

      {d.ventas_detalle.length > 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
          <h3 className="font-semibold mb-3">G/P realizada por valor</h3>
          <ul className="text-sm space-y-1 font-mono">
            {d.ventas_detalle.map((v) => (
              <li key={v.isin} className="flex justify-between gap-3">
                <span className="font-sans truncate">{v.nombre}</span>
                <span className={parseFloat(v.gp_eur) >= 0
                  ? 'text-emerald-700 dark:text-emerald-400'
                  : 'text-rose-700 dark:text-rose-400'}>
                  {fmtEUR(v.gp_eur, { maximumFractionDigits: 2 })}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {d.destacados.length > 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
          <h3 className="font-semibold mb-3">Movimientos destacados</h3>
          <table className="w-full text-xs">
            <thead className="text-[rgb(var(--muted))]">
              <tr className="text-left border-b border-[rgb(var(--border))]">
                <th className="py-2 pr-2">Fecha</th>
                <th className="pr-2">Tipo</th>
                <th className="pr-2">Valor</th>
                <th className="pr-2 text-right">Importe (EUR)</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {d.destacados.map((m, i) => (
                <tr key={`${m.fecha}-${i}`} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2">{m.fecha}</td>
                  <td className="pr-2 font-sans">{TIPO_LABEL[m.tipo] ?? m.tipo}</td>
                  <td className="pr-2 font-sans">{m.nombre}</td>
                  <td className="pr-2 text-right">{fmtEUR(m.importe_eur, { maximumFractionDigits: 2 })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-[rgb(var(--muted))]">
        La G/P realizada usa el mismo motor FIFO fiscal (incluye gastos y reglas de
        homogeneidad). El progreso IF es la foto de HOY, no la del cierre del mes —
        Cima no guarda histórico de precios.
      </p>
    </div>
  );
}

function Card({ label, value, sub, tono }: {
  label: string; value: string; sub?: string; tono?: 'ok' | 'warn' | 'muted';
}) {
  const color =
    tono === 'ok' ? 'text-emerald-700 dark:text-emerald-400'
      : tono === 'warn' ? 'text-rose-700 dark:text-rose-400'
        : tono === 'muted' ? 'text-[rgb(var(--muted))]' : 'text-[rgb(var(--fg))]';
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <p className="text-xs text-[rgb(var(--muted))]">{label}</p>
      <p className={`text-lg font-semibold font-mono ${color}`}>{value}</p>
      {sub && <p className="text-[10px] text-[rgb(var(--muted))] mt-1">{sub}</p>}
    </div>
  );
}
