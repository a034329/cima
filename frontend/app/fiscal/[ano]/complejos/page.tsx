import { fetchComplejos, fetchComplejosAcumulado, fmtEUR, fmtNum } from '@/lib/api';
import type { ComplejosResumen } from '@/lib/types';

export default async function ComplejosPage({ params }: { params: { ano: string } }) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: ComplejosResumen | null = null;
  let error: string | null = null;
  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado ? await fetchComplejosAcumulado() : await fetchComplejos(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        CFD · futuros · warrants · estructurados · fondos · cripto IBKR — detección, sin cálculo fiscal
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-800 p-4 mb-4">
          <p className="text-sm text-amber-800 dark:text-amber-200">
            <span className="font-semibold">Aviso:</span> estos instrumentos NO los calcula el motor
            fiscal de Cima todavía. Se listan sólo para que sepas que están en tu extracto y los
            declares por tu cuenta. No entran en ninguna casilla automática.
          </p>
        </div>
      )}

      {data && data.n === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado
              ? 'No se han detectado productos complejos en tus extractos.'
              : `Sin productos complejos en ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Bien — tu cartera no tiene CFDs, futuros ni estructurados que requieran tratamiento aparte.
          </p>
        </div>
      )}

      {data && data.n > 0 && <ComplejosContenido d={data} />}
    </div>
  );
}

function ComplejosContenido({ d }: { d: ComplejosResumen }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <h3 className="font-semibold mb-3">Detectados ({d.n})</h3>
      <table className="w-full text-xs">
        <thead className="text-[rgb(var(--muted))]">
          <tr className="text-left border-b border-[rgb(var(--border))]">
            <th className="py-2 pr-2">Fecha</th>
            <th className="pr-2">Símbolo</th>
            <th className="pr-2">Nombre</th>
            <th className="pr-2">Categoría IBKR</th>
            <th className="pr-2 text-right">Cantidad</th>
            <th className="pr-2 text-right">Importe (EUR)</th>
            <th className="pr-2">Broker</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {[...d.lineas]
            .sort((a, b) => ((a.fecha ?? '') < (b.fecha ?? '') ? 1 : -1))
            .map((l, i) => (
              <tr key={`${l.simbolo}-${i}`} className="border-t border-[rgb(var(--border))]/30">
                <td className="py-1 pr-2">{l.fecha ?? '—'}</td>
                <td className="pr-2">{l.simbolo}</td>
                <td className="pr-2 font-sans text-[rgb(var(--muted))]">{l.nombre}</td>
                <td className="pr-2 font-sans">{l.asset_category || '—'}</td>
                <td className="pr-2 text-right">{fmtNum(l.cantidad)}</td>
                <td className="pr-2 text-right">{fmtEUR(l.importe_eur, { maximumFractionDigits: 2 })}</td>
                <td className="pr-2 text-[rgb(var(--muted))]">{l.broker}</td>
              </tr>
            ))}
        </tbody>
      </table>
      <p className="text-xs text-[rgb(var(--muted))] mt-3 italic">
        Detección desde la clasificación por Asset Category de IBKR. Cálculo: {d.fecha_calculo}.
      </p>
    </div>
  );
}
