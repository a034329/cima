import type { CarteraResumen } from '@/lib/types';
import { fmtEUR, fmtPct } from '@/lib/api';

interface Props {
  cartera: CarteraResumen;
}

interface KPI {
  label: string;
  value: string;
  hint?: string;
  tone?: 'normal' | 'positive' | 'warning';
}

export function ResumenCartera({ cartera }: Props) {
  const progreso = parseFloat(cartera.progreso_if_pct);
  const anos = parseFloat(cartera.anos_estimados_if);
  const gpReal = parseFloat(cartera.gp_realizada_anio);
  const optNeto = parseFloat(cartera.opciones_neto_anio);

  const kpis: KPI[] = [
    { label: 'Capital cartera', value: fmtEUR(cartera.capital_total_eur), hint: 'a fecha de hoy' },
    {
      label: 'Liquidez disponible',
      value: fmtEUR(cartera.liquidez_eur),
      hint: 'efectivo (saldo broker / cash flows)',
    },
    {
      label: 'Progreso IF',
      value: fmtPct(cartera.progreso_if_pct),
      hint: 'capital objetivo 300.000 €',
      tone: progreso >= 0.5 ? 'positive' : 'normal',
    },
    {
      label: 'Años a IF',
      value: anos.toFixed(2),
      hint: 'al ritmo actual',
    },
    {
      label: 'Yield actual',
      value: fmtPct(cartera.yield_actual_pct, 2),
      hint: `dividendos netos ${cartera.anio} / capital (YTD)`,
    },
    {
      label: `Dividendos brutos ${cartera.anio}`,
      value: fmtEUR(cartera.dividendos_bruto_anio),
      hint: 'cobrados este año (casilla 0029)',
      tone: 'positive',
    },
    {
      label: `Opciones netas ${cartera.anio}`,
      value: fmtEUR(cartera.opciones_neto_anio),
      hint: 'primas cobradas − pagadas (declarables)',
      tone: optNeto >= 0 ? 'positive' : 'warning',
    },
    {
      label: `G/P realizada ${cartera.anio}`,
      value: fmtEUR(cartera.gp_realizada_anio),
      hint: 'plusvalías/minusvalías realizadas (FIFO)',
      tone: gpReal >= 0 ? 'positive' : 'warning',
    },
    {
      label: `Aportación neta ${cartera.anio}`,
      value: fmtEUR(cartera.aportacion_neta_anio),
      hint: 'capital de tu bolsillo este año (IBKR/TR auto · DEGIRO manual)',
    },
  ];

  return (
    <section className="mb-10">
      <div className="mb-4">
        <h2 className="text-2xl font-semibold tracking-tight">{cartera.nombre}</h2>
        <p className="text-sm text-[rgb(var(--muted))]">
          Snapshot del {new Date(cartera.fecha_snapshot).toLocaleDateString('es-ES')}
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {kpis.map((kpi) => (
          <div
            key={kpi.label}
            className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4"
          >
            <div className="text-xs uppercase tracking-wider text-[rgb(var(--muted))]">
              {kpi.label}
            </div>
            <div
              className={`mt-1 text-2xl font-semibold ${
                kpi.tone === 'positive'
                  ? 'text-emerald-600 dark:text-emerald-400'
                  : kpi.tone === 'warning'
                    ? 'text-amber-600 dark:text-amber-400'
                    : ''
              }`}
            >
              {kpi.value}
            </div>
            {kpi.hint && (
              <div className="mt-1 text-xs text-[rgb(var(--muted))]">{kpi.hint}</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
