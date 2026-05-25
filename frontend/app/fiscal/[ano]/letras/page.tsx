import { fetchBills, fetchBillsAcumulado, fmtEUR } from '@/lib/api';
import type { BillsResumen } from '@/lib/types';

export default async function BillsPage({ params }: { params: { ano: string } }) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: BillsResumen | null = null;
  let error: string | null = null;
  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado ? await fetchBillsAcumulado() : await fetchBills(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        T-Bills / Letras · el rendimiento tributa como RCM en España · sólo IBKR
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.lineas.length === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado ? 'No hay Letras del Tesoro en BD.' : `Sin Letras del Tesoro en ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Las Letras/T-Bills se importan de la sección Realized &amp; Unrealized Performance Summary de IBKR.
          </p>
        </div>
      )}

      {data && data.lineas.length > 0 && <BillsContenido d={data} />}
    </div>
  );
}

function BillsContenido({ d }: { d: BillsResumen }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Card
          label="Rendimiento (RCM)"
          value={fmtEUR(d.realized_total, { maximumFractionDigits: 2 })}
          tono={parseFloat(d.realized_total) >= 0 ? 'ok' : 'warn'}
          subtext="Rendimiento del capital mobiliario"
        />
        <Card
          label="Letras"
          value={String(d.lineas.length)}
          tono="muted"
          subtext="Títulos con resultado realizado"
        />
        <Card
          label="Periodo del statement"
          value={d.periodo_inicio && d.periodo_fin ? `${d.periodo_inicio} → ${d.periodo_fin}` : '—'}
          tono="muted"
          subtext="Rango cubierto por las cifras"
        />
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-3">Por título ({d.lineas.length})</h3>
        <table className="w-full text-sm">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Símbolo</th>
              <th className="pr-2 text-right">Rendimiento (EUR)</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {[...d.lineas]
              .sort((a, b) => parseFloat(b.realized_eur) - parseFloat(a.realized_eur))
              .map((l) => (
                <tr key={l.simbolo} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2">{l.simbolo}</td>
                  <td
                    className={`pr-2 text-right ${
                      parseFloat(l.realized_eur) >= 0
                        ? 'text-emerald-700 dark:text-emerald-400'
                        : 'text-rose-700 dark:text-rose-400'
                    }`}
                  >
                    {fmtEUR(l.realized_eur, { maximumFractionDigits: 2 })}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Dónde declararlo</p>
        <p>· El rendimiento de Letras del Tesoro ({fmtEUR(d.realized_total, { maximumFractionDigits: 2 })}) tributa como <span className="font-mono">RCM</span> (rendimiento del capital mobiliario), no como ganancia patrimonial.</p>
        <p>· La diferencia entre compra y amortización/venta es el rendimiento. IBKR ya lo reporta realizado en EUR.</p>
        <p className="italic mt-1">Datos de la sección Realized &amp; Unrealized Performance Summary (Treasury Bills) de IBKR. Cálculo: {d.fecha_calculo}.</p>
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
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${tonoCss[tono]}`}>{value}</div>
      {subtext && <div className="text-xs text-[rgb(var(--muted))] mt-1">{subtext}</div>}
    </div>
  );
}
