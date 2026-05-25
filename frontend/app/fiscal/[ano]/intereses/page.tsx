import { fetchIntereses, fetchInteresesAcumulado, fmtEUR } from '@/lib/api';
import type { InteresesResumen } from '@/lib/types';

const TIPO_LABEL: Record<string, string> = {
  credit: 'Crédito (RCM)',
  bond_interest: 'Cupón bono (RCM)',
  debit: 'Débito (no deducible)',
};

export default async function InteresesPage({ params }: { params: { ano: string } }) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: InteresesResumen | null = null;
  let error: string | null = null;
  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado ? await fetchInteresesAcumulado() : await fetchIntereses(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        Crédito y cupones → RCM (casilla 0023) · Débito al broker → informativo no deducible
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.n_lineas === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado ? 'No hay intereses en BD.' : `Sin intereses en ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Los intereses (crédito/débito/cupones) se importan del Activity Statement de IBKR.
          </p>
        </div>
      )}

      {data && data.n_lineas > 0 && <InteresesContenido d={data} />}
    </div>
  );
}

function InteresesContenido({ d }: { d: InteresesResumen }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Card
          label="RCM — casilla 0023"
          value={fmtEUR(d.rcm_total, { maximumFractionDigits: 2 })}
          tono="ok"
          subtext="Crédito + cupones de bonos"
        />
        <Card
          label="Débito (no deducible)"
          value={fmtEUR(d.debit_total, { maximumFractionDigits: 2 })}
          tono="muted"
          subtext="Interés pagado al broker · informativo"
        />
        <Card
          label="Neto"
          value={fmtEUR(d.neto_total, { maximumFractionDigits: 2 })}
          tono={parseFloat(d.neto_total) >= 0 ? 'ok' : 'warn'}
          subtext={`${d.n_lineas} movimientos`}
        />
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-3">Movimientos ({d.lineas.length})</h3>
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Fecha</th>
              <th className="pr-2">Tipo</th>
              <th className="pr-2">Descripción</th>
              <th className="pr-2">Divisa</th>
              <th className="pr-2 text-right">Importe (EUR)</th>
              <th className="pr-2">Casilla</th>
              <th className="pr-2">Broker</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {[...d.lineas]
              .sort((a, b) => (a.fecha < b.fecha ? 1 : -1))
              .map((l, i) => (
                <tr key={`${l.fecha}-${i}`} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2">{l.fecha}</td>
                  <td className="pr-2 font-sans">{TIPO_LABEL[l.tipo] ?? l.tipo}</td>
                  <td className="pr-2 font-sans text-[rgb(var(--muted))]">{l.descripcion}</td>
                  <td className="pr-2">{l.divisa}</td>
                  <td
                    className={`pr-2 text-right ${
                      parseFloat(l.importe_eur) >= 0
                        ? 'text-emerald-700 dark:text-emerald-400'
                        : 'text-rose-700 dark:text-rose-400'
                    }`}
                  >
                    {fmtEUR(l.importe_eur, { maximumFractionDigits: 2 })}
                  </td>
                  <td className="pr-2">{l.casilla ?? '—'}</td>
                  <td className="pr-2 text-[rgb(var(--muted))]">{l.broker}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Dónde declararlo</p>
        <p>· Intereses de crédito y cupones ({fmtEUR(d.rcm_total, { maximumFractionDigits: 2 })}) → <span className="font-mono">casilla 0023</span> (rendimientos del capital mobiliario).</p>
        <p>· Intereses de débito ({fmtEUR(d.debit_total, { maximumFractionDigits: 2 })}): interés pagado al broker. NO deducible automáticamente para particulares (criterio AEAT, Art. 26.1.b LIRPF). Informativo.</p>
        <p className="italic mt-1">Datos de la sección Interest de IBKR (convertidos a EUR vía BCE). Cálculo: {d.fecha_calculo}.</p>
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
