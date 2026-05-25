import { fetchFiscal, fetchFiscalAcumulado, fmtEUR, fmtNum } from '@/lib/api';
import type {
  Compensacion,
  FifoMatch,
  FiscalResumen,
  PerdidaDiferida,
  PerdidaPendiente,
  PositionSummary,
} from '@/lib/types';

export default async function FiscalPage({
  params,
}: {
  params: { ano: string };
}) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let fiscal: FiscalResumen | null = null;
  let error: string | null = null;

  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      fiscal = esAcumulado
        ? await fetchFiscalAcumulado()
        : await fetchFiscal(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        {esAcumulado
          ? 'Todos los matches FIFO + dividendos del histórico en BD. Informativo (no es la cifra de RentaWEB).'
          : 'FIFO multi-año · Regla 2 meses · Compensación RCM↔patrimoniales · Bolsas 4 años'}
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
          <p className="text-xs text-[rgb(var(--muted))] mt-1">
            ¿Has hecho bootstrap? Comprueba en /api/bootstrap.
          </p>
        </div>
      )}

      {fiscal && fiscal.n_matches === 0 && fiscal.positions.length === 0 && parseFloat(fiscal.rcm_neto) === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado
              ? 'Sin operaciones ni dividendos en BD.'
              : `Sin operaciones ni dividendos para el ejercicio ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Importa un extracto desde la página de Cartera para empezar.
          </p>
        </div>
      )}

      {fiscal && (
        fiscal.n_matches > 0
        || fiscal.positions.length > 0
        || parseFloat(fiscal.rcm_neto) !== 0
      ) && <FiscalContenido f={fiscal} />}
    </div>
  );
}

function FiscalContenido({ f }: { f: FiscalResumen }) {
  const comp = f.compensacion;
  return (
    <div className="space-y-6">
      {/* Síntesis principal */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card
          label="G/P bruto patrimonial"
          value={fmtEUR(f.gp_bruto, { maximumFractionDigits: 0 })}
          tono={parseFloat(f.gp_bruto) >= 0 ? 'ok' : 'err'}
          subtext={`${f.n_matches} matches`}
        />
        <Card
          label="No deducible (2M)"
          value={fmtEUR(f.gp_no_deducible_2m, { maximumFractionDigits: 0 })}
          tono={parseFloat(f.gp_no_deducible_2m) > 0 ? 'warn' : 'muted'}
          subtext="Bloqueadas por Art. 33.5.f"
        />
        <Card
          label="Pérdidas afloradas"
          value={fmtEUR(f.total_perdida_aflorada, { maximumFractionDigits: 0 })}
          tono={parseFloat(f.total_perdida_aflorada) > 0 ? 'ok' : 'muted'}
          subtext="Liberadas este ejercicio"
        />
        <Card
          label="RCM neto"
          value={fmtEUR(f.rcm_neto, { maximumFractionDigits: 0 })}
          tono={parseFloat(f.rcm_neto) >= 0 ? 'ok' : 'err'}
          subtext="Dividendos + intereses (− retención ES)"
        />
      </div>

      {/* Compensación */}
      <ComposicionPanel comp={comp} />

      {/* Posiciones abiertas (inventario FIFO a fecha de corte) */}
      {f.positions.length > 0 && <PositionsTable positions={f.positions} />}

      {/* Pérdidas diferidas latentes */}
      {f.perdidas_diferidas_latentes.length > 0 && (
        <PerdidasDiferidasPanel perdidas={f.perdidas_diferidas_latentes} />
      )}

      {/* Bolsas pérdidas pendientes (arrastre 4 años) */}
      {comp.perdidas_actualizadas.length > 0 && (
        <BolsasPerdidasPanel
          actualizadas={comp.perdidas_actualizadas}
          expiradas={comp.perdidas_expiradas}
          proximas={comp.perdidas_proximas_expirar}
        />
      )}

      {/* Tabla de matches FIFO */}
      {f.matches.length > 0 && <MatchesTable matches={f.matches} />}

      {/* Warnings */}
      {f.warnings.length > 0 && (
        <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-4">
          <p className="text-xs font-semibold text-amber-700 dark:text-amber-300 mb-2">
            Avisos del motor fiscal
          </p>
          <ul className="text-xs space-y-1 text-amber-900 dark:text-amber-200 font-mono">
            {f.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Orphan sales */}
      {f.orphan_sales.length > 0 && (
        <div className="rounded-lg border border-rose-300 dark:border-rose-700 bg-rose-50 dark:bg-rose-900/20 p-4">
          <p className="text-xs font-semibold text-rose-700 dark:text-rose-300 mb-2">
            Ventas sin inventario ({f.orphan_sales.length})
          </p>
          <p className="text-xs text-rose-700 dark:text-rose-300 mb-2">
            Probablemente falta importar el extracto del broker donde estaba la compra.
          </p>
          <ul className="text-xs space-y-1 text-rose-900 dark:text-rose-200">
            {f.orphan_sales.map((o, i) => (
              <li key={i} className="font-mono">
                {o.fecha} {o.broker} {o.isin} ({o.nombre}) — falta{' '}
                {fmtNum(o.cantidad_faltante)} de {fmtNum(o.cantidad)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="text-xs text-[rgb(var(--muted))]">
        Calculado por el motor de Cuádrate (motor_fiscal.py + compensacion_perdidas.py)
        invocado in-process desde Cima. Fecha de corte: {f.fecha_corte}. Cálculo: {f.fecha_calculo}.
      </p>
    </div>
  );
}

function PositionsTable({ positions }: { positions: PositionSummary[] }) {
  const ordenadas = [...positions].sort(
    (a, b) => parseFloat(b.coste_total_eur) - parseFloat(a.coste_total_eur),
  );
  const costeTotal = ordenadas.reduce(
    (acc, p) => acc + parseFloat(p.coste_total_eur),
    0,
  );

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="font-semibold">Posiciones abiertas ({ordenadas.length})</h3>
        <span className="text-sm text-[rgb(var(--muted))]">
          Coste total: {fmtEUR(costeTotal.toString())}
        </span>
      </div>
      <table className="w-full text-xs">
        <thead className="text-[rgb(var(--muted))]">
          <tr className="text-left border-b border-[rgb(var(--border))]">
            <th className="py-2 pr-2">ISIN / Nombre</th>
            <th className="pr-2 text-right">Cantidad</th>
            <th className="pr-2 text-right">PM (€)</th>
            <th className="pr-2 text-right">Coste total (€)</th>
            <th className="pr-2 text-right">Lotes</th>
            <th className="pr-2">Antigüedad</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {ordenadas.map((p) => (
            <tr key={p.isin} className="border-t border-[rgb(var(--border))]/30">
              <td className="py-1 pr-2">
                <div>{p.isin}</div>
                <div className="text-[rgb(var(--muted))]">{p.nombre}</div>
              </td>
              <td className="pr-2 text-right">{fmtNum(p.cantidad_total)}</td>
              <td className="pr-2 text-right">
                {fmtEUR(p.pm_ponderado_eur, { maximumFractionDigits: 2 })}
              </td>
              <td className="pr-2 text-right font-semibold">
                {fmtEUR(p.coste_total_eur, { maximumFractionDigits: 2 })}
              </td>
              <td className="pr-2 text-right">{p.num_lotes}</td>
              <td className="pr-2 text-[rgb(var(--muted))]">
                {p.lote_mas_antiguo}
                {p.es_mixta && (
                  <span
                    className="ml-1 px-1 py-0.5 rounded bg-blue-200 dark:bg-blue-700 text-blue-900 dark:text-blue-100 text-[10px] font-sans"
                    title="Lotes de distintas fechas/brokers"
                  >
                    mixta
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ComposicionPanel({ comp }: { comp: Compensacion }) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5">
      <h3 className="font-semibold mb-3">Compensación intra-año + bolsas anteriores</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm">
        <Row label="G/P bruto" value={fmtEUR(comp.gp_bruto)} />
        <Row label="− No deducible 2M" value={fmtEUR(comp.gp_no_deducible_2m)} />
        <Row label="= G/P deducible" value={fmtEUR(comp.gp_deducible)} bold />
        <Row label="RCM neto" value={fmtEUR(comp.rcm_neto)} />
        <Row
          label="Cruce G/P → RCM (max 25%)"
          value={fmtEUR(comp.cruce_gp_a_rcm)}
          dim={parseFloat(comp.cruce_gp_a_rcm) === 0}
        />
        <Row
          label="Cruce RCM → G/P (max 25%)"
          value={fmtEUR(comp.cruce_rcm_a_gp)}
          dim={parseFloat(comp.cruce_rcm_a_gp) === 0}
        />
        <Row label="Saldo G/P tras cruce" value={fmtEUR(comp.saldo_gp_tras_cruce)} />
        <Row label="Saldo RCM tras cruce" value={fmtEUR(comp.saldo_rcm_tras_cruce)} />
        <Row
          label="Aplicado de pérdidas anteriores"
          value={fmtEUR(comp.aplicadas_de_anteriores)}
          dim={parseFloat(comp.aplicadas_de_anteriores) === 0}
        />
        <Row
          label="Nuevo saldo negativo a arrastrar"
          value={fmtEUR(comp.nuevo_saldo_negativo)}
          dim={parseFloat(comp.nuevo_saldo_negativo) === 0}
        />
      </div>
      <div className="mt-4 pt-3 border-t border-[rgb(var(--border))] grid grid-cols-2 gap-4">
        <BaseBox label="Base ahorro G/P" value={comp.base_ahorro_gp} />
        <BaseBox label="Base ahorro RCM" value={comp.base_ahorro_rcm} />
      </div>
    </div>
  );
}

function PerdidasDiferidasPanel({ perdidas }: { perdidas: PerdidaDiferida[] }) {
  return (
    <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-4">
      <h3 className="font-semibold text-amber-700 dark:text-amber-300 mb-2">
        Pérdidas diferidas latentes ({perdidas.length})
      </h3>
      <p className="text-xs text-amber-900 dark:text-amber-200 mb-3">
        Aflorarán al vender el lote recomprado que las disparó (Art. 33.5.f LIRPF).
      </p>
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-amber-700 dark:text-amber-400">
          <tr className="text-left">
            <th className="py-1">ISIN / Nombre</th>
            <th>Pérdida origen</th>
            <th>Cantidad pendiente</th>
            <th>Fecha venta origen</th>
          </tr>
        </thead>
        <tbody className="text-amber-900 dark:text-amber-200">
          {perdidas.map((p, i) => (
            <tr key={i} className="border-t border-amber-300/50">
              <td className="py-1">
                <span className="font-mono text-xs">{p.isin}</span>
                <br />
                <span className="text-xs">{p.nombre}</span>
              </td>
              <td>{fmtEUR(p.importe_eur)}</td>
              <td>{fmtNum(p.cantidad_pendiente)}</td>
              <td className="font-mono text-xs">{p.fecha_venta_origen}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BolsasPerdidasPanel({
  actualizadas,
  expiradas,
  proximas,
}: {
  actualizadas: PerdidaPendiente[];
  expiradas: PerdidaPendiente[];
  proximas: PerdidaPendiente[];
}) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <h3 className="font-semibold mb-2">Bolsas de pérdida (arrastre 4 años)</h3>
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-[rgb(var(--muted))]">
          <tr className="text-left">
            <th className="py-1">Ejercicio origen</th>
            <th>Original</th>
            <th>Compensado</th>
            <th>Pendiente</th>
            <th>Expira</th>
          </tr>
        </thead>
        <tbody>
          {actualizadas.map((p, i) => {
            const proximaExpirar = proximas.some(
              (x) => x.ejercicio_origen === p.ejercicio_origen,
            );
            return (
              <tr
                key={i}
                className={`border-t border-[rgb(var(--border))] ${
                  proximaExpirar
                    ? 'bg-amber-50/50 dark:bg-amber-900/10'
                    : ''
                }`}
              >
                <td className="py-1">{p.ejercicio_origen}</td>
                <td>{fmtEUR(p.importe_original_eur)}</td>
                <td>{fmtEUR(p.compensado_eur)}</td>
                <td className="font-semibold">{fmtEUR(p.pendiente_eur)}</td>
                <td className="text-xs">
                  {p.expira}
                  {proximaExpirar && (
                    <span className="ml-2 text-amber-600 dark:text-amber-400">
                      próxima
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
          {expiradas.map((p, i) => (
            <tr
              key={`exp-${i}`}
              className="border-t border-[rgb(var(--border))] text-[rgb(var(--muted))] line-through"
            >
              <td className="py-1">{p.ejercicio_origen}</td>
              <td>{fmtEUR(p.importe_original_eur)}</td>
              <td>{fmtEUR(p.compensado_eur)}</td>
              <td>{fmtEUR(p.pendiente_eur)}</td>
              <td className="text-xs">expirado</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MatchesTable({ matches }: { matches: FifoMatch[] }) {
  // Agrupar por ISIN para legibilidad
  const grupos = new Map<string, FifoMatch[]>();
  for (const m of matches) {
    const arr = grupos.get(m.isin) ?? [];
    arr.push(m);
    grupos.set(m.isin, arr);
  }

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <h3 className="font-semibold mb-3">Matches FIFO ({matches.length})</h3>
      <table className="w-full text-xs">
        <thead className="text-[rgb(var(--muted))]">
          <tr className="text-left border-b border-[rgb(var(--border))]">
            <th className="py-2 pr-2">Fecha venta</th>
            <th className="pr-2">Fecha compra</th>
            <th className="pr-2">ISIN / Nombre</th>
            <th className="pr-2 text-right">Cantidad</th>
            <th className="pr-2 text-right">Coste adq.</th>
            <th className="pr-2 text-right">Importe transm.</th>
            <th className="pr-2 text-right">G/P</th>
            <th className="pr-2 text-right">Aflorada</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {[...grupos.entries()].flatMap(([isin, ms]) =>
            ms.map((m, i) => {
              const gp = parseFloat(m.ganancia_perdida);
              const aflorada = parseFloat(m.perdida_diferida_aflorada_eur);
              return (
                <tr
                  key={`${isin}-${i}`}
                  className={`border-t border-[rgb(var(--border))]/30 ${
                    m.regla_2_meses
                      ? 'bg-amber-50/40 dark:bg-amber-900/10'
                      : ''
                  }`}
                >
                  <td className="py-1 pr-2">{m.fecha_venta}</td>
                  <td className="pr-2">{m.fecha_compra}</td>
                  <td className="pr-2">
                    <div>{isin}</div>
                    <div className="text-[rgb(var(--muted))] not-italic">{m.nombre}</div>
                  </td>
                  <td className="pr-2 text-right">{fmtNum(m.cantidad)}</td>
                  <td className="pr-2 text-right">{fmtEUR(m.coste_adquisicion, { maximumFractionDigits: 2 })}</td>
                  <td className="pr-2 text-right">{fmtEUR(m.importe_transmision, { maximumFractionDigits: 2 })}</td>
                  <td
                    className={`pr-2 text-right font-semibold ${
                      gp >= 0
                        ? 'text-emerald-700 dark:text-emerald-400'
                        : 'text-rose-700 dark:text-rose-400'
                    }`}
                  >
                    {fmtEUR(m.ganancia_perdida, { maximumFractionDigits: 2 })}
                  </td>
                  <td
                    className={`pr-2 text-right ${
                      aflorada > 0
                        ? 'text-amber-700 dark:text-amber-400'
                        : 'text-[rgb(var(--muted))]/50'
                    }`}
                  >
                    {aflorada > 0
                      ? fmtEUR(m.perdida_diferida_aflorada_eur, { maximumFractionDigits: 2 })
                      : '—'}
                  </td>
                  <td className="pr-2">
                    {m.regla_2_meses && (
                      <span
                        className="inline-block px-1.5 py-0.5 rounded bg-amber-200 dark:bg-amber-700 text-amber-900 dark:text-amber-100 text-[10px] font-sans font-medium"
                        title={m.regla_2_meses_detalle}
                      >
                        2M
                      </span>
                    )}
                    {m.es_scrip && (
                      <span className="inline-block ml-1 px-1.5 py-0.5 rounded bg-blue-200 dark:bg-blue-700 text-blue-900 dark:text-blue-100 text-[10px] font-sans">
                        scrip
                      </span>
                    )}
                    {m.es_corto && (
                      <span className="inline-block ml-1 px-1.5 py-0.5 rounded bg-purple-200 dark:bg-purple-700 text-purple-900 dark:text-purple-100 text-[10px] font-sans">
                        corto
                      </span>
                    )}
                  </td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
    </div>
  );
}

// ── Utilidades de UI ────────────────────────────────────────────────────

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
      <div className={`text-2xl font-semibold mt-1 ${tonoCss[tono]}`}>
        {value}
      </div>
      {subtext && (
        <div className="text-xs text-[rgb(var(--muted))] mt-1">{subtext}</div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  bold,
  dim,
}: {
  label: string;
  value: string;
  bold?: boolean;
  dim?: boolean;
}) {
  return (
    <div
      className={`flex justify-between gap-4 ${
        bold ? 'font-semibold' : ''
      } ${dim ? 'text-[rgb(var(--muted))]' : ''}`}
    >
      <span>{label}</span>
      <span className="font-mono tabular-nums">{value}</span>
    </div>
  );
}

function BaseBox({ label, value }: { label: string; value: string }) {
  const v = parseFloat(value);
  return (
    <div className="rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3 text-center">
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">
        {label}
      </div>
      <div
        className={`text-xl font-semibold mt-1 ${
          v >= 0
            ? 'text-emerald-700 dark:text-emerald-400'
            : 'text-rose-700 dark:text-rose-400'
        }`}
      >
        {fmtEUR(value)}
      </div>
    </div>
  );
}
