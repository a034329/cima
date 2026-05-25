import { fetchForex, fetchForexAcumulado, fmtEUR } from '@/lib/api';
import type { ForexResumen } from '@/lib/types';

export default async function ForexPage({ params }: { params: { ano: string } }) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: ForexResumen | null = null;
  let error: string | null = null;
  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado ? await fetchForexAcumulado() : await fetchForex(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        G/P de divisa (Art. 33.5.e LIRPF) · sólo el realizado es declarable · sólo IBKR
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.lineas.length === 0 && (
        <Vacio esAcumulado={esAcumulado} ejercicio={ejercicio} />
      )}

      {data && data.lineas.length > 0 && <ForexContenido d={data} />}
    </div>
  );
}

function ForexContenido({ d }: { d: ForexResumen }) {
  const realized = parseFloat(d.realized_total);
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Card
          label="Realizado (declarable)"
          value={fmtEUR(d.realized_total, { maximumFractionDigits: 2 })}
          tono={realized >= 0 ? 'ok' : 'warn'}
          subtext="Ganancia/pérdida patrimonial · base del ahorro"
        />
        <Card
          label="Latente (informativo)"
          value={fmtEUR(d.unrealized_total, { maximumFractionDigits: 2 })}
          tono="muted"
          subtext="No realizado · no se declara"
        />
        <Card
          label="Periodo del statement"
          value={d.periodo_inicio && d.periodo_fin ? `${d.periodo_inicio} → ${d.periodo_fin}` : '—'}
          tono="muted"
          subtext="Rango cubierto por las cifras"
        />
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-3">Por divisa ({d.lineas.length})</h3>
        <table className="w-full text-sm">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Divisa</th>
              <th className="pr-2 text-right">Realizado (declarable)</th>
              <th className="pr-2 text-right">Latente</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {[...d.lineas]
              .sort((a, b) => parseFloat(a.realized_eur) - parseFloat(b.realized_eur))
              .map((l) => (
                <tr key={l.divisa} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2 font-sans font-semibold">{l.divisa}</td>
                  <td
                    className={`pr-2 text-right ${
                      parseFloat(l.realized_eur) >= 0
                        ? 'text-emerald-700 dark:text-emerald-400'
                        : 'text-rose-700 dark:text-rose-400'
                    }`}
                  >
                    {fmtEUR(l.realized_eur, { maximumFractionDigits: 2 })}
                  </td>
                  <td className="pr-2 text-right text-[rgb(var(--muted))]">
                    {fmtEUR(l.unrealized_eur, { maximumFractionDigits: 2 })}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Dónde declararlo</p>
        <p>· G/P de divisa realizada ({fmtEUR(d.realized_total, { maximumFractionDigits: 2 })}) → ganancias/pérdidas patrimoniales de la <span className="font-mono">base del ahorro</span> (Art. 33.5.e LIRPF).</p>
        <p>· El resultado latente ({fmtEUR(d.unrealized_total, { maximumFractionDigits: 2 })}) NO se declara hasta materializarse.</p>
        <p className="italic mt-1">Datos de la sección Realized &amp; Unrealized Performance Summary de IBKR (en EUR). Cálculo: {d.fecha_calculo}.</p>
      </div>
    </div>
  );
}

function Vacio({ esAcumulado, ejercicio }: { esAcumulado: boolean; ejercicio: number | null }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
      <p className="text-[rgb(var(--muted))]">
        {esAcumulado ? 'No hay G/P de divisa en BD.' : `Sin G/P de divisa en ${ejercicio}.`}
      </p>
      <p className="text-xs text-[rgb(var(--muted))] mt-2">
        Importa un Activity Statement de IBKR con la sección Realized &amp; Unrealized Performance Summary.
      </p>
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
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${tonoCss[tono]}`}>{value}</div>
      {subtext && <div className="text-xs text-[rgb(var(--muted))] mt-1">{subtext}</div>}
    </div>
  );
}
