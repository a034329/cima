import { fetchFugas, fmtEUR } from '@/lib/api';
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

export default async function FugasPage() {
  // Las fugas se calculan siempre sobre el ejercicio en curso + cartera viva:
  // el selector de año del hub no aplica aquí.
  let data: FugasResumen | null = null;
  let error: string | null = null;
  try {
    data = await fetchFugas();
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        Retención de origen por encima del tope del CDI: NO se recupera en la
        declaración española (casilla 0588) — solo reclamándola al fisco extranjero.
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.por_pais.length === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--fg))] font-semibold">Sin fugas fiscales detectadas 🎉</p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Ninguna posición con dividendo sufre retención de origen por encima del
            tope del convenio (p. ej. EE.UU. con W-8BEN, Reino Unido o España).
          </p>
        </div>
      )}

      {data && data.por_pais.length > 0 && <FugasContenido d={data} />}
    </div>
  );
}

function FugasContenido({ d }: { d: FugasResumen }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3">
        <Card
          label={`Fuga estimada anual`}
          value={fmtEUR(d.total_fuga_anual_estimada_eur, { maximumFractionDigits: 2 })}
          tono="warn"
          subtext="Proyección: yield estimado × valor × exceso CDI"
        />
        <Card
          label={`Exceso real ${d.ejercicio} (YTD)`}
          value={fmtEUR(d.total_exceso_real_ytd_eur, { maximumFractionDigits: 2 })}
          tono="muted"
          subtext="Sobre dividendos ya cobrados este ejercicio"
        />
      </div>

      {d.por_pais.map((p) => (
        <PaisCard key={p.pais} p={p} />
      ))}

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-xs text-[rgb(var(--muted))] space-y-1">
        <p className="font-semibold text-[rgb(var(--fg))]">Cómo leer esto</p>
        <p>
          · En la declaración española solo es deducible la retención de origen hasta el
          tope del convenio (normalmente 15%). El resto se queda en el país de origen.
        </p>
        <p>
          · Ese exceso SÍ es reclamable, pero al fisco extranjero, con su propio
          formulario y plazos (indicado en cada país).
        </p>
        <p>
          · La fuga también penaliza el CAGR4+Div de la posición: ya está descontada en
          la métrica neta de Estrategia.
        </p>
      </div>
    </div>
  );
}

function PaisCard({ p }: { p: FugaPais }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
        <h3 className="font-semibold">
          {PAIS_LABEL[p.pais] ?? p.pais}{' '}
          <span className="text-xs font-normal text-[rgb(var(--muted))]">
            exceso no recuperable: {fmtPct(p.exceso_pct)}
          </span>
        </h3>
        <div className="text-sm font-mono text-amber-700 dark:text-amber-400">
          {fmtEUR(p.fuga_anual_estimada_eur, { maximumFractionDigits: 2 })}/año
        </div>
      </div>
      <p className="text-xs text-[rgb(var(--muted))] mb-3">Recuperación: {p.mecanismo}</p>
      <table className="w-full text-xs">
        <thead className="text-[rgb(var(--muted))]">
          <tr className="text-left border-b border-[rgb(var(--border))]">
            <th className="py-2 pr-2">Posición</th>
            <th className="pr-2 text-right">Div. anual est. (EUR)</th>
            <th className="pr-2 text-right">Fuga anual est.</th>
            <th className="pr-2 text-right">Exceso real YTD</th>
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
                {fmtEUR(x.exceso_real_ytd_eur, { maximumFractionDigits: 2 })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
