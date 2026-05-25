'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { fetchDashboard, fmtEUR, fmtPct } from '@/lib/api';
import { onDatosActualizados } from '@/lib/refetch';
import { DividendosChart } from '@/components/DividendosChart';
import { DECISION_COLOR, DECISION_LABEL } from '@/lib/decisiones';
import { CAT_HEX, CAT_LABEL } from '@/lib/categorias';
import type { DashboardData } from '@/lib/types';

export function Dashboard() {
  const [d, setD] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cargar = useCallback(() => {
    fetchDashboard()
      .then((data) => { setD(data); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    cargar();
    return onDatosActualizados(cargar);   // recarga tras import / alta de operación
  }, [cargar]);

  if (error) {
    return (
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
        <p className="text-[rgb(var(--muted))]">No hay datos de cartera todavía.</p>
        <p className="text-xs text-[rgb(var(--muted))] mt-2">
          Importa un extracto para ver tu dashboard.
        </p>
      </div>
    );
  }
  if (!d) return <p className="text-sm text-[rgb(var(--muted))]">Cargando dashboard…</p>;

  const gpNoReal = parseFloat(d.gp_no_realizada_eur);

  return (
    <div className="space-y-8">
      {/* ── ¿Cómo voy? ── */}
      <Grupo titulo="¿Cómo voy?">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <KPI label="Invertido" value={fmtEUR(d.capital_mercado_eur)} sub="acciones, ETF y opciones · sin liquidez" />
          <KPI
            label="G/P no realizada"
            value={fmtEUR(d.gp_no_realizada_eur)}
            sub={fmtPct(d.gp_no_realizada_pct, 1)}
            tono={gpNoReal >= 0 ? 'ok' : 'warn'}
          />
          <KPI
            label="Progreso IF"
            value={fmtPct(d.progreso_if_pct, 0)}
            sub={d.anios_if
              ? `${parseFloat(d.anios_if).toFixed(1)} años · al ${fmtPct(d.retorno_if_pct, 0)} est.`
              : 'objetivo no alcanzable con estos supuestos'}
            barra={parseFloat(d.progreso_if_pct)}
          />
          <KPI label="Liquidez" value={fmtEUR(d.liquidez_eur)} sub="disponible" />
        </div>
      </Grupo>

      {/* ── ¿Cómo está compuesta? ── */}
      <Grupo titulo="¿Cómo está compuesta?" href="/estrategia" cta="Ver bloques →">
        <Composicion comp={d.composicion} />
      </Grupo>

      {/* ── ¿Qué rinde? ── */}
      <Grupo titulo="¿Qué rinde?">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <KPI label="Yield actual" value={fmtPct(d.yield_actual_pct, 2)} sub="dividendos netos / capital" tono="ok" />
          <KPI label="Yield estimado"
            value={d.yield_estimado_pct ? fmtPct(d.yield_estimado_pct, 2) : '—'}
            sub="dividendo/acción previsto" tono={d.yield_estimado_pct ? 'ok' : 'muted'} />
          <KPI label="CAGR potencial (anual)"
            value={d.cagr_anual_pct ? fmtPct(d.cagr_anual_pct, 1) : '—'}
            sub="CAGR4 + Div ponderado" tono={d.cagr_anual_pct ? 'ok' : 'muted'} />
          <KPI label="Retorno potencial 5a"
            value={d.retorno_5y_pct ? fmtPct(d.retorno_5y_pct, 0) : '—'}
            sub="acumulado estimado" tono={d.retorno_5y_pct ? 'ok' : 'muted'} />
        </div>
        <div className="mt-3">
          <DividendosChart />
        </div>
      </Grupo>

      {/* ── ¿Qué hago? ── */}
      <Grupo titulo="¿Qué hago?">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          {/* Próximos pasos */}
          <Panel titulo="Próximos pasos" href="/estrategia/plan" cta="Ver plan →">
            {d.proximos_pasos.length === 0 ? (
              <p className="text-sm text-[rgb(var(--muted))]">
                Sin pasos activos. Define decisiones por valor en el Plan.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {d.proximos_pasos.map((p, i) => (
                  <li key={p.isin + i} className="flex items-center gap-2 text-sm">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${DECISION_COLOR[p.decision]}`}>
                      {DECISION_LABEL[p.decision]}
                    </span>
                    <span className="truncate">{p.nombre}</span>
                    <span className="ml-auto text-xs text-[rgb(var(--muted))]">{p.prioridad}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          {/* Eficiencia fiscal */}
          <Panel titulo="Eficiencia fiscal" href="/fiscal/2026/optimizar" cta="Optimizar →">
            <div className="space-y-1 text-sm">
              <Fila label="G/P realizada año" valor={d.gp_realizada_anio} colored />
              <Fila label="Compensable ahora" valor={d.compensable_ahora} />
              <Fila label="Pérdidas por aflorar" valor={d.perdidas_por_aflorar} dim />
              <Fila label="Pérdida a arrastrar" valor={d.perdida_a_arrastrar} dim />
            </div>
          </Panel>

          {/* Opciones en riesgo */}
          <Panel titulo="Opciones" href="/fiscal/2026/opciones" cta="Ver opciones →">
            <div className="text-sm text-[rgb(var(--muted))] mb-2">
              {d.opciones_proximas_vencer} próximas a vencer · {d.opciones_itm} ITM
            </div>
            {d.opciones_riesgo.length === 0 ? (
              <p className="text-sm text-[rgb(var(--muted))]">Sin opciones en riesgo inminente.</p>
            ) : (
              <ul className="space-y-1.5">
                {d.opciones_riesgo.slice(0, 4).map((o, i) => (
                  <li key={o.simbolo + i} className="flex items-center gap-2 text-xs">
                    {o.riesgo_ejercicio && (
                      <span className="text-amber-600 dark:text-amber-400" title="Riesgo de ejercicio">⚠</span>
                    )}
                    <span className="font-mono truncate">{o.simbolo}</span>
                    {o.moneyness && (
                      <span className={o.moneyness === 'ITM'
                        ? 'text-amber-700 dark:text-amber-400' : 'text-[rgb(var(--muted))]'}>
                        {o.moneyness}
                      </span>
                    )}
                    {o.dias_a_vencer != null && (
                      <span className="ml-auto text-[rgb(var(--muted))]">{o.dias_a_vencer}d</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </Grupo>
    </div>
  );

  function Composicion({ comp }: { comp: DashboardData['composicion'] }) {
    const segmentos = comp.filter((c) => parseFloat(c.valor_eur) > 0);
    const total = segmentos.reduce((s, c) => s + parseFloat(c.valor_eur), 0);
    return (
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 flex flex-col md:flex-row items-center gap-6">
        <Donut segmentos={segmentos.map((c) => ({
          valor: parseFloat(c.valor_eur), color: CAT_HEX[c.categoria_base],
        }))} />
        <div className="flex-1 w-full space-y-1.5">
          {segmentos
            .sort((a, b) => parseFloat(b.valor_eur) - parseFloat(a.valor_eur))
            .map((c) => (
              <div key={c.nombre} className="flex items-center gap-2 text-sm">
                <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: CAT_HEX[c.categoria_base] }} />
                <span className="truncate">{c.nombre}</span>
                <span className="text-xs text-[rgb(var(--muted))]">{CAT_LABEL[c.categoria_base]}</span>
                <span className="ml-auto font-mono">{fmtEUR(c.valor_eur, { maximumFractionDigits: 0 })}</span>
                <span className="w-12 text-right text-[rgb(var(--muted))]">
                  {total > 0 ? `${((parseFloat(c.valor_eur) / total) * 100).toFixed(0)}%` : '—'}
                </span>
              </div>
            ))}
        </div>
      </div>
    );
  }
}

function Donut({ segmentos }: { segmentos: { valor: number; color: string }[] }) {
  const total = segmentos.reduce((s, x) => s + x.valor, 0) || 1;
  const r = 52;
  const c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg viewBox="0 0 140 140" className="w-36 h-36 shrink-0">
      <g transform="rotate(-90 70 70)">
        <circle cx="70" cy="70" r={r} fill="none" stroke="rgb(var(--border))" strokeWidth="16" />
        {segmentos.map((s, i) => {
          const len = (s.valor / total) * c;
          const el = (
            <circle
              key={i}
              cx="70"
              cy="70"
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth="16"
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-offset}
            />
          );
          offset += len;
          return el;
        })}
      </g>
    </svg>
  );
}

function Grupo({ titulo, href, cta, children }: {
  titulo: string; href?: string; cta?: string; children: React.ReactNode;
}) {
  return (
    <section>
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">{titulo}</h3>
        {href && cta && (
          <Link href={href} className="text-xs text-brand-600 dark:text-brand-300 hover:underline">{cta}</Link>
        )}
      </div>
      {children}
    </section>
  );
}

function KPI({ label, value, sub, tono = 'normal', barra }: {
  label: string; value: string; sub?: string;
  tono?: 'normal' | 'ok' | 'warn' | 'muted'; barra?: number;
}) {
  const css = {
    normal: '', ok: 'text-emerald-600 dark:text-emerald-400',
    warn: 'text-rose-600 dark:text-rose-400', muted: 'text-[rgb(var(--muted))]',
  }[tono];
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${css}`}>{value}</div>
      {barra != null && (
        <div className="h-1.5 bg-[rgb(var(--border))] rounded mt-2 overflow-hidden">
          <div className="h-full bg-brand-500" style={{ width: `${Math.min(barra * 100, 100)}%` }} />
        </div>
      )}
      {sub && <div className="text-xs text-[rgb(var(--muted))] mt-1">{sub}</div>}
    </div>
  );
}

function Panel({ titulo, href, cta, children }: {
  titulo: string; href: string; cta: string; children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="flex items-baseline justify-between mb-2">
        <h4 className="font-semibold text-base">{titulo}</h4>
        <Link href={href} className="text-xs text-brand-600 dark:text-brand-300 hover:underline">{cta}</Link>
      </div>
      {children}
    </div>
  );
}

function Fila({ label, valor, colored, dim }: { label: string; valor: string; colored?: boolean; dim?: boolean }) {
  const n = parseFloat(valor);
  const css = colored ? (n >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400')
    : dim ? 'text-[rgb(var(--muted))]' : '';
  return (
    <div className="flex justify-between gap-3">
      <span className="text-[rgb(var(--muted))]">{label}</span>
      <span className={`font-mono tabular-nums ${css}`}>{fmtEUR(valor, { maximumFractionDigits: 0 })}</span>
    </div>
  );
}
