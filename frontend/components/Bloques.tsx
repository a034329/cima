import type { Bloque } from '@/lib/types';
import { fmtEUR, fmtPct } from '@/lib/api';
import { CAT_COLOR as CATEGORIA_COLOR, CAT_LABEL as CATEGORIA_LABEL } from '@/lib/categorias';

interface Props {
  bloques: Bloque[];
}

export function Bloques({ bloques }: Props) {
  return (
    <section className="mb-10">
      <h3 className="text-lg font-semibold mb-4">Bloques de estrategia</h3>
      <div className="grid gap-3">
        {bloques.map((b) => {
          const objetivo = parseFloat(b.peso_objetivo);
          const actual = parseFloat(b.peso_actual);
          const desviacion = parseFloat(b.desviacion);
          const fueraRango = Math.abs(desviacion) > 0.05;

          // Barra de progreso visual del peso actual vs objetivo
          const barraActualPct = Math.min(actual * 100 * 2, 100);
          const objetivoPct = Math.min(objetivo * 100 * 2, 100);

          return (
            <div
              key={b.nombre}
              className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4"
            >
              <div className="flex items-start justify-between gap-4 mb-3">
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <h4 className="font-semibold">{b.nombre}</h4>
                    <span
                      className={`text-xs px-2 py-0.5 rounded ${CATEGORIA_COLOR[b.categoria_base]}`}
                    >
                      {CATEGORIA_LABEL[b.categoria_base]}
                    </span>
                    {fueraRango && (
                      <span className="text-xs px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                        Fuera de tolerancia
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-[rgb(var(--muted))]">
                    {fmtEUR(b.valor_eur)}
                  </div>
                </div>
                <div className="text-right text-sm">
                  <div className="text-[rgb(var(--muted))]">objetivo {fmtPct(objetivo)}</div>
                  <div className="font-semibold">{fmtPct(actual)} actual</div>
                  <div
                    className={
                      desviacion > 0
                        ? 'text-amber-600 dark:text-amber-400'
                        : desviacion < 0
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : 'text-[rgb(var(--muted))]'
                    }
                  >
                    {desviacion > 0 ? '+' : ''}
                    {fmtPct(desviacion, 2)}
                  </div>
                </div>
              </div>

              {/* Barra de progreso */}
              <div className="relative h-2 bg-[rgb(var(--border))] rounded overflow-hidden">
                <div
                  className="absolute top-0 left-0 h-full bg-brand-500"
                  style={{ width: `${barraActualPct}%` }}
                  aria-label={`Peso actual: ${fmtPct(actual)}`}
                />
                <div
                  className="absolute top-0 h-full w-0.5 bg-[rgb(var(--fg))]"
                  style={{ left: `${objetivoPct}%` }}
                  aria-label={`Objetivo: ${fmtPct(objetivo)}`}
                />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
