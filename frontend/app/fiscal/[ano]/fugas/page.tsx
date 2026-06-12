'use client';

import { useCallback, useEffect, useState } from 'react';
import { fetchFugas, fmtEUR, marcarFugaReclamada } from '@/lib/api';
import type { FugasResumen, FugaPais } from '@/lib/types';

const PAIS_LABEL: Record<string, string> = {
  CH: 'Suiza',
  DE: 'Alemania',
  FR: 'Francia',
  BE: 'Bélgica',
  IT: 'Italia',
  DK: 'Dinamarca',
  PT: 'Portugal',
  NL: 'Países Bajos',
  US: 'Estados Unidos',
  GB: 'Reino Unido',
  CA: 'Canadá',
  NO: 'Noruega',
  SE: 'Suecia',
  FI: 'Finlandia',
  AT: 'Austria',
  IE: 'Irlanda',
  JP: 'Japón',
};

const fmtPct = (frac: string) =>
  `${(parseFloat(frac) * 100).toLocaleString('es-ES', { maximumFractionDigits: 2 })}%`;

export default function FugasPage() {
  // El selector de año del hub NO aplica aquí: el panel mira la ventana
  // completa de reclamación de cada país (2-5 años hacia atrás).
  const [data, setData] = useState<FugasResumen | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cargar = useCallback(async () => {
    try {
      setData(await fetchFugas());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { cargar(); }, [cargar]);

  const toggle = async (pais: string, ejercicio: number, reclamado: boolean) => {
    try {
      await marcarFugaReclamada(pais, ejercicio, reclamado);
      await cargar();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        Retención de origen por encima del tope del CDI: NO se recupera en la
        declaración española (casilla 0588) — solo reclamándola al fisco extranjero.
        Cada país da un plazo de 2 a 5 años: aquí ves el acumulado reclamable y
        marcas lo que ya has gestionado.
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {!data && !error && <p className="text-sm text-[rgb(var(--muted))]">Cargando…</p>}

      {data && data.por_pais.length === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--fg))] font-semibold">Sin fugas fiscales detectadas 🎉</p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Ninguna posición con dividendo sufre retención de origen por encima del
            tope del convenio (p. ej. EE.UU. con W-8BEN, Reino Unido o España) en los
            últimos {data.ventana_anios} años.
          </p>
        </div>
      )}

      {data && data.por_pais.length > 0 && (
        <FugasContenido d={data} onToggle={toggle} />
      )}
    </div>
  );
}

function FugasContenido({ d, onToggle }: {
  d: FugasResumen;
  onToggle: (pais: string, ejercicio: number, reclamado: boolean) => Promise<void>;
}) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3">
        <Card
          label={`Pendiente de reclamar (últimos ${d.ventana_anios} años)`}
          value={fmtEUR(d.total_reclamable_pendiente_eur, { maximumFractionDigits: 2 })}
          tono="warn"
          subtext="Exceso real sobre dividendos cobrados, dentro de plazo y sin marcar como reclamado"
        />
        <Card
          label="Fuga estimada anual (a futuro)"
          value={fmtEUR(d.total_fuga_anual_estimada_eur, { maximumFractionDigits: 2 })}
          tono="muted"
          subtext="Proyección: yield estimado × valor × exceso CDI de cada posición"
        />
      </div>

      {d.por_pais.map((p) => (
        <PaisCard key={p.pais} p={p} onToggle={onToggle} />
      ))}

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Cómo leer esto</p>
        <p>
          · En la declaración española solo es deducible la retención de origen hasta el
          tope del convenio (normalmente 15%). El resto se queda en el país de origen.
        </p>
        <p>
          · Ese exceso SÍ es reclamable al fisco extranjero dentro de su plazo
          (Suiza 3 años, Alemania 4, Francia 2, Italia 4, Bélgica y Dinamarca 5 —
          contados desde el fin del año del cobro, aproximación conservadora).
        </p>
        <p>
          · Marca un año como «reclamado» cuando presentes el formulario: el panel
          descuenta ese importe del pendiente. La fuga también está descontada en el
          CAGR4+Div neto de Estrategia.
        </p>
      </div>
    </div>
  );
}

function PaisCard({ p, onToggle }: {
  p: FugaPais;
  onToggle: (pais: string, ejercicio: number, reclamado: boolean) => Promise<void>;
}) {
  const pendiente = parseFloat(p.reclamable_pendiente_eur);
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
        <h3 className="font-semibold">
          {PAIS_LABEL[p.pais] ?? p.pais}{' '}
          <span className="text-xs font-normal text-[rgb(var(--muted))]">
            exceso no recuperable: {fmtPct(p.exceso_pct)} · plazo {p.plazo_anios} años
            {!p.plazo_verificado && ' (orientativo)'}
          </span>
        </h3>
        <div className="text-sm font-mono">
          <span className={pendiente > 0 ? 'text-amber-700 dark:text-amber-400' : 'text-[rgb(var(--muted))]'}>
            {fmtEUR(p.reclamable_pendiente_eur, { maximumFractionDigits: 2 })} pendiente
          </span>
        </div>
      </div>
      <p className="text-xs text-[rgb(var(--muted))] mb-3">Recuperación: {p.mecanismo}</p>

      {p.anios.length > 0 && (
        <table className="w-full text-xs mb-4">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Año</th>
              <th className="pr-2 text-right">Exceso cobrado (EUR)</th>
              <th className="pr-2">Límite de reclamación</th>
              <th className="pr-2">Estado</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {p.anios.map((a) => (
              <tr key={a.ejercicio} className="border-t border-[rgb(var(--border))]/30">
                <td className="py-1.5 pr-2">{a.ejercicio}</td>
                <td className="pr-2 text-right">{fmtEUR(a.exceso_eur, { maximumFractionDigits: 2 })}</td>
                <td className="pr-2">{a.limite ?? '—'}</td>
                <td className="pr-2 font-sans">
                  {!a.dentro_plazo ? (
                    <span className="text-[rgb(var(--muted))]">prescrito</span>
                  ) : (
                    <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={a.reclamado}
                        onChange={(e) => onToggle(p.pais, a.ejercicio, e.target.checked)}
                      />
                      {a.reclamado
                        ? <span className="text-emerald-700 dark:text-emerald-400">reclamado</span>
                        : <span className="text-amber-700 dark:text-amber-400">pendiente</span>}
                    </label>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {p.posiciones.length > 0 && (
        <details>
          <summary className="text-xs text-[rgb(var(--muted))] cursor-pointer">
            Posiciones afectadas ({p.posiciones.length}) — proyección anual y exceso acumulado
          </summary>
          <table className="w-full text-xs mt-2">
            <thead className="text-[rgb(var(--muted))]">
              <tr className="text-left border-b border-[rgb(var(--border))]">
                <th className="py-2 pr-2">Posición</th>
                <th className="pr-2 text-right">Div. anual est. (EUR)</th>
                <th className="pr-2 text-right">Fuga anual est.</th>
                <th className="pr-2 text-right">Exceso acumulado</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {p.posiciones.map((x) => (
                <tr key={x.isin} className="border-t border-[rgb(var(--border))]/30">
                  <td className="py-1 pr-2 font-sans">
                    {x.nombre} <span className="text-[rgb(var(--muted))]">{x.isin}</span>
                  </td>
                  <td className="pr-2 text-right">
                    {x.div_anual_estimado_eur != null
                      ? fmtEUR(x.div_anual_estimado_eur, { maximumFractionDigits: 2 })
                      : '—'}
                  </td>
                  <td className="pr-2 text-right text-amber-700 dark:text-amber-400">
                    {x.fuga_anual_estimada_eur != null
                      ? fmtEUR(x.fuga_anual_estimada_eur, { maximumFractionDigits: 2 })
                      : '—'}
                  </td>
                  <td className="pr-2 text-right">
                    {fmtEUR(x.exceso_real_total_eur, { maximumFractionDigits: 2 })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
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
  tono: 'ok' | 'warn' | 'muted';
  subtext?: string;
}) {
  const color =
    tono === 'ok'
      ? 'text-emerald-700 dark:text-emerald-400'
      : tono === 'warn'
        ? 'text-amber-700 dark:text-amber-400'
        : 'text-[rgb(var(--fg))]';
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <p className="text-xs text-[rgb(var(--muted))]">{label}</p>
      <p className={`text-lg font-semibold font-mono ${color}`}>{value}</p>
      {subtext && <p className="text-[10px] text-[rgb(var(--muted))] mt-1">{subtext}</p>}
    </div>
  );
}
