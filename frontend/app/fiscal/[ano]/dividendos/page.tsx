import { fetchDividendos, fetchDividendosAcumulado, fmtEUR } from '@/lib/api';
import type { DividendoPorIsin, DividendosResumen } from '@/lib/types';

function fmtPct(s: string | null): string {
  if (s === null) return '—';
  return `${(parseFloat(s) * 100).toFixed(2)}%`;
}

export default async function DividendosPage({
  params,
}: {
  params: { ano: string };
}) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: DividendosResumen | null = null;
  let error: string | null = null;

  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado
        ? await fetchDividendosAcumulado()
        : await fetchDividendos(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        Bruto → casilla 0029 · Retención ES (19%) → casilla 0591 · Deducción CDI → casilla 0588
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.n_pagadores === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado ? 'No hay dividendos en BD.' : `Sin dividendos en ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Importa DEGIRO (Transacciones + Cuenta) o IBKR con cobros de dividendos.
          </p>
        </div>
      )}

      {data && data.n_pagadores > 0 && <DividendosContenido d={data} />}
    </div>
  );
}

function DividendosContenido({ d }: { d: DividendosResumen }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card
          label="Bruto total (casilla 0029)"
          value={fmtEUR(d.bruto_total, { maximumFractionDigits: 0 })}
          tono="ok"
          subtext={`${d.n_pagadores} pagadores`}
        />
        <Card
          label="Retención ES (0591)"
          value={fmtEUR(d.ret_es_total, { maximumFractionDigits: 0 })}
          tono="muted"
          subtext="19% nacional + TR Sucursal ES · acreditable 100%"
        />
        <Card
          label="CDI recuperable (0588)"
          value={fmtEUR(d.cdi_recuperable_total, { maximumFractionDigits: 0 })}
          tono="ok"
          subtext="Deducción doble imposición extranjera"
        />
        <Card
          label="Exceso no recuperable"
          value={fmtEUR(d.exceso_total, { maximumFractionDigits: 0 })}
          tono={parseFloat(d.exceso_total) > 0 ? 'warn' : 'muted'}
          subtext="Retención sobre el tope CDI (coste)"
        />
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-3">Pagadores ({d.pagadores.length})</h3>
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">ISIN / Empresa</th>
              <th className="pr-2">País</th>
              <th className="pr-2 text-right">Bruto</th>
              <th className="pr-2 text-right">Ret. origen</th>
              <th className="pr-2 text-right">Ret. ES (0591)</th>
              <th className="pr-2 text-right">% Ret.</th>
              <th className="pr-2 text-right">% Tope CDI</th>
              <th className="pr-2 text-right">Límite CDI</th>
              <th className="pr-2 text-right">Recuperable (0588)</th>
              <th className="pr-2">Broker</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {[...d.pagadores]
              .sort((a, b) => parseFloat(b.bruto) - parseFloat(a.bruto))
              .map((p) => (
                <tr key={p.isin} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2">
                    <div>{p.isin}</div>
                    <div className="text-[rgb(var(--muted))]">{p.nombre}</div>
                  </td>
                  <td className="pr-2">
                    {p.pais}
                    {p.es_nacional && (
                      <span className="ml-1 px-1 py-0.5 rounded bg-emerald-200 dark:bg-emerald-700 text-emerald-900 dark:text-emerald-100 text-[10px] font-sans">
                        ES
                      </span>
                    )}
                  </td>
                  <td className="pr-2 text-right font-semibold">
                    {fmtEUR(p.bruto, { maximumFractionDigits: 2 })}
                  </td>
                  <td className="pr-2 text-right">{fmtEUR(p.ret_origen, { maximumFractionDigits: 2 })}</td>
                  <td className="pr-2 text-right text-emerald-700 dark:text-emerald-400">
                    {parseFloat(p.retencion_es) > 0
                      ? fmtEUR(p.retencion_es, { maximumFractionDigits: 2 })
                      : <span className="text-[rgb(var(--muted))]">—</span>}
                  </td>
                  <td className="pr-2 text-right text-[rgb(var(--muted))]">
                    {parseFloat(p.bruto) > 0
                      ? `${((parseFloat(p.ret_origen) / parseFloat(p.bruto)) * 100).toFixed(1)}%`
                      : '—'}
                  </td>
                  <td className="pr-2 text-right text-[rgb(var(--muted))]">{fmtPct(p.tasa_cdi)}</td>
                  <td className="pr-2 text-right text-[rgb(var(--muted))]">
                    {p.es_nacional ? '—' : fmtEUR(p.limite_cdi, { maximumFractionDigits: 2 })}
                  </td>
                  <td className="pr-2 text-right text-emerald-700 dark:text-emerald-400">
                    {fmtEUR(p.recuperable, { maximumFractionDigits: 2 })}
                  </td>
                  <td className="pr-2 text-[rgb(var(--muted))]">{p.brokers}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Dónde declararlo en RentaWEB</p>
        <p>· Bruto total ({fmtEUR(d.bruto_total)}) → <span className="font-mono">casilla 0029</span> (rendimientos capital mobiliario).</p>
        <p>· Retención española 19% ({fmtEUR(d.ret_es_total)}) → <span className="font-mono">casilla 0591</span> (acreditable 100%; incluye nacionales y la doble retención de TR Sucursal ES).</p>
        <p>· Deducción doble imposición CDI ({fmtEUR(d.cdi_recuperable_total)}) → <span className="font-mono">casilla 0588</span>; base = bruto extranjero con retención {fmtEUR(d.bruto_ext_con_ret)}.</p>
        <p>· Exceso no recuperable ({fmtEUR(d.exceso_total)}): retención sobre el tope CDI, coste definitivo no deducible.</p>
        <p className="italic mt-1">Calculado con calcular_resumen_dividendos de Cuádrate. Cálculo: {d.fecha_calculo}.</p>
      </div>
    </div>
  );
}

function Card({
  label,
  value,
  tono,
  subtext,
}: {
  label: string;
  value: string;
  tono: 'ok' | 'warn' | 'err' | 'muted';
  subtext?: string;
}) {
  const tonoCss: Record<typeof tono, string> = {
    ok: 'text-emerald-700 dark:text-emerald-400',
    warn: 'text-amber-700 dark:text-amber-400',
    err: 'text-rose-700 dark:text-rose-400',
    muted: 'text-[rgb(var(--muted))]',
  };
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">
        {label}
      </div>
      <div className={`text-2xl font-semibold mt-1 ${tonoCss[tono]}`}>{value}</div>
      {subtext && <div className="text-xs text-[rgb(var(--muted))] mt-1">{subtext}</div>}
    </div>
  );
}
