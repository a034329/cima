'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  editarEstimacion,
  fetchCompsEstado,
  fetchOnePagerEstado,
  fetchPosicionesBloque,
  fetchSeguimiento,
  fetchValoracionEstado,
  fmtPct,
  lanzarComps,
  lanzarOnePager,
  lanzarValoracion,
} from '@/lib/api';
import type { Comps, EstadoAnalisis, OnePager, Valoracion } from '@/lib/types';

type Empresa = { isin: string; nombre: string; fuente: 'cartera' | 'watchlist' };

const BADGE: Record<string, string> = {
  COYUNTURAL: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  GRIS: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  ESTRUCTURAL: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300',
};

// (múltiplo, métrica por acción) — espejo de ETIQUETAS_TIPO_VAL del backend.
const ETIQUETAS_TIPO_VAL: Record<string, [string, string]> = {
  PER: ['PER', 'EPS'],
  P_FCF: ['P/FCF', 'FCF/acc.'],
  P_BV: ['P/BV', 'NAV/acc.'],
  P_FRE: ['P/FRE', 'FRE/acc.'],
  SOTP: ['P/NAV', 'NAV/acc. (suma de partes)'],
};
const etiquetasTipoVal = (tv?: string): [string, string] =>
  ETIQUETAS_TIPO_VAL[tv ?? 'PER'] ?? ETIQUETAS_TIPO_VAL.PER;

export default function AnalisisPage() {
  const [empresas, setEmpresas] = useState<Empresa[]>([]);
  const [isin, setIsin] = useState('');
  const [op, setOp] = useState<OnePager | null>(null);
  const [estado, setEstado] = useState<EstadoAnalisis>('ninguno');
  const [error, setError] = useState<string | null>(null);
  const cargando = estado === 'en_curso';

  const cargar = useCallback(async () => {
    try {
      const [pos, segs] = await Promise.all([fetchPosicionesBloque(), fetchSeguimiento()]);
      const lista: Empresa[] = [
        ...pos.map((p) => ({ isin: p.isin, nombre: p.nombre, fuente: 'cartera' as const })),
        ...segs.map((s) => ({ isin: s.isin, nombre: s.nombre || s.ticker, fuente: 'watchlist' as const })),
      ];
      // dedupe por isin (una posición puede estar también en watchlist)
      const vistos = new Set<string>();
      const dedup = lista.filter((e) => !vistos.has(e.isin) && vistos.add(e.isin));
      setEmpresas(dedup);
      const url = new URLSearchParams(window.location.search).get('isin');
      if (url && dedup.some((e) => e.isin === url)) setIsin(url);
      else if (dedup.length && !isin) setIsin(dedup[0].isin);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { cargar(); }, [cargar]);

  // Al cambiar de empresa, carga estado + resultado guardado (sin lanzar IA).
  useEffect(() => {
    if (!isin) { setOp(null); setEstado('ninguno'); return; }
    let vigente = true;
    setOp(null); setEstado('ninguno'); setError(null);
    fetchOnePagerEstado(isin).then((r) => {
      if (!vigente) return;
      setOp(r.resultado); setEstado(r.estado); setError(r.error);
    }).catch(() => {});
    return () => { vigente = false; };
  }, [isin]);

  // Polling mientras el job está en curso.
  useEffect(() => {
    if (estado !== 'en_curso' || !isin) return;
    let vigente = true;
    const id = setInterval(async () => {
      try {
        const r = await fetchOnePagerEstado(isin);
        if (!vigente) return;
        if (r.resultado) setOp(r.resultado);
        if (r.estado !== 'en_curso') { setEstado(r.estado); setError(r.error); }
      } catch { /* reintenta en el próximo tick */ }
    }, 4000);
    return () => { vigente = false; clearInterval(id); };
  }, [estado, isin]);

  const generar = async () => {
    if (!isin) return;
    setError(null);
    try {
      const r = await lanzarOnePager(isin);
      setEstado(r.estado);                 // 'en_curso' → arranca el polling
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="space-y-5">
      <p className="text-sm text-[rgb(var(--muted))] max-w-3xl">
        Estudio inicial de una empresa: la IA reúne lo que Cima sabe (valoración, encaje de bloque,
        régimen) y <strong>busca contexto reciente en la web</strong> para escribir una tesis con
        fuentes. Orientativo — verifica las fuentes.
      </p>

      <div className="flex items-center gap-2 flex-wrap">
        <select
          value={isin}
          onChange={(e) => setIsin(e.target.value)}
          className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] max-w-xs"
        >
          {empresas.length === 0 && <option value="">— sin empresas —</option>}
          {empresas.map((e) => (
            <option key={e.isin} value={e.isin}>{e.nombre} · {e.fuente}</option>
          ))}
        </select>
        <button
          onClick={generar}
          disabled={cargando || !isin}
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {cargando ? 'Estudiando…' : op ? 'Regenerar' : 'Generar one-pager'}
        </button>
        {op && !cargando && (
          <span className="text-xs text-[rgb(var(--muted))]">guardado · {op.fecha}</span>
        )}
        <Link href="/estrategia/estimaciones"
          className="ml-auto text-xs text-brand-600 dark:text-brand-400 hover:underline">
          ver en Estimaciones →
        </Link>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {cargando && (
        <p className="text-sm text-[rgb(var(--muted))] animate-pulse">
          Estudiando la empresa y buscando contexto en la web… puede tardar varios minutos
          (se ejecuta en segundo plano; puedes navegar y volver).
        </p>
      )}

      {op && (
        <article className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-4 max-w-3xl">
          <header className="flex items-center gap-2 flex-wrap border-b border-[rgb(var(--border))] pb-3">
            <h3 className="text-lg font-semibold">{op.nombre}</h3>
            {op.clasificacion && BADGE[op.clasificacion] && (
              <span className={`text-xs px-2 py-0.5 rounded font-medium ${BADGE[op.clasificacion]}`}>
                {op.clasificacion}
              </span>
            )}
            <span className="ml-auto text-xs text-[rgb(var(--muted))]">{op.fecha}</span>
          </header>

          <Seccion titulo="Qué hace" texto={op.que_hace} />
          <Seccion titulo="Tesis" texto={op.tesis} />
          <Seccion titulo="Riesgos" texto={op.riesgos} />
          <Seccion titulo="Valoración" texto={op.valoracion} />
          <Seccion titulo="Encaje en tu estrategia" texto={op.encaje} />
          {op.veredicto && (
            <div className="rounded-md bg-[rgb(var(--bg))] p-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-[rgb(var(--muted))] mb-1">Veredicto</div>
              <p className="text-sm font-medium">{op.veredicto}</p>
            </div>
          )}

          {op.fuentes.length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs border-t border-[rgb(var(--border))] pt-3">
              <span className="text-[rgb(var(--muted))]">Fuentes:</span>
              {op.fuentes.map((u, i) => (
                <a key={i} href={u} target="_blank" rel="noreferrer"
                   className="text-brand-600 dark:text-brand-400 hover:underline truncate max-w-[240px]">
                  {hostOf(u)}
                </a>
              ))}
            </div>
          )}
          <p className="text-[11px] text-[rgb(var(--muted))] italic">
            {op.disclaimer ?? 'One-pager de IA con búsqueda web; orientativo, verifica las fuentes.'}
          </p>
        </article>
      )}

      {isin && <ValoracionAsistida isin={isin} />}
      {isin && <CompsAsistida isin={isin} />}
    </div>
  );
}

function ValoracionAsistida({ isin }: { isin: string }) {
  const [val, setVal] = useState<Valoracion | null>(null);
  const [estado, setEstado] = useState<EstadoAnalisis>('ninguno');
  const [error, setError] = useState<string | null>(null);
  const [editando, setEditando] = useState<string | null>(null);   // nombre del escenario en edición
  const [mult, setMult] = useState('');
  const [eps, setEps] = useState('');
  const [aplicado, setAplicado] = useState<string | null>(null);
  const cargando = estado === 'en_curso';
  const esPer = (val?.tipo_val ?? 'PER') === 'PER';
  // Trabajamos a nivel de "múltiplo" (genérico); la métrica indica la base (EPS, FRE/acc., …).
  const [, metlabel] = etiquetasTipoVal(val?.tipo_val);

  useEffect(() => {
    let vigente = true;
    setVal(null); setEstado('ninguno'); setError(null); setEditando(null); setAplicado(null);
    fetchValoracionEstado(isin).then((r) => {
      if (!vigente) return;
      setVal(r.resultado); setEstado(r.estado); setError(r.error);
    }).catch(() => {});
    return () => { vigente = false; };
  }, [isin]);

  useEffect(() => {
    if (estado !== 'en_curso') return;
    let vigente = true;
    const id = setInterval(async () => {
      try {
        const r = await fetchValoracionEstado(isin);
        if (!vigente) return;
        if (r.resultado) setVal(r.resultado);
        if (r.estado !== 'en_curso') { setEstado(r.estado); setError(r.error); }
      } catch { /* reintenta */ }
    }, 4000);
    return () => { vigente = false; clearInterval(id); };
  }, [estado, isin]);

  const generar = async () => {
    setError(null);
    try {
      const r = await lanzarValoracion(isin);
      setEstado(r.estado);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const abrir = (nombre: string, m: number, e: number) => {
    setEditando(nombre); setMult(String(m)); setEps(String(e)); setAplicado(null);
  };

  const aplicar = async (nombre: string) => {
    const m = parseFloat(mult.replace(',', '.'));
    const e = parseFloat(eps.replace(',', '.'));
    if (isNaN(m) || isNaN(e)) return;
    if (!window.confirm('Esto sobrescribirá el múltiplo y la métrica base 4Y de tu estimación. ¿Continuar?')) return;
    try {
      await editarEstimacion(isin, {
        multiplo_objetivo: m, metrica_base_4y: e,
        notas: `Múltiplo/${metlabel} de escenario IA «${nombre}» (${val?.fecha ?? ''})`,
      });
      setEditando(null); setAplicado(nombre);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-3 max-w-3xl">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
          Valoración asistida
        </h3>
        <button onClick={generar} disabled={cargando}
          className="px-3 py-1 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50">
          {cargando ? 'Calculando…' : val ? 'Regenerar escenarios' : 'Proponer escenarios'}
        </button>
      </div>

      {!val && !cargando && (
        <p className="text-xs text-[rgb(var(--muted))]">
          Los escenarios se anclan en el consenso/histórico (o en comparables del sector si no es por
          beneficios) y se ligan a la tesis de tu one-pager (genéralo arriba primero para mejores
          resultados). Puede tardar unos minutos.
        </p>
      )}
      {error && <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>}
      {cargando && <p className="text-xs text-[rgb(var(--muted))] animate-pulse">La IA está valorando en segundo plano… (puede tardar minutos)</p>}

      {val && (
        <>
          <div className="text-xs text-[rgb(var(--muted))] flex flex-wrap gap-x-4 gap-y-1 border-b border-[rgb(var(--border))] pb-2">
            <span>Anclas:</span>
            {esPer ? (
              <>
                <span>EPS actual <strong>{fnum(val.anclas.eps_actual)}</strong></span>
                <span>EPS consenso 4Y <strong>{fnum(val.anclas.eps_consenso_4y)}</strong></span>
                <span>PER hist. <strong>{fnum(val.anclas.per_hist_medio)}</strong>/<strong>{fnum(val.anclas.per_hist_mediano)}</strong></span>
                <span>P.obj. consenso <strong>{fnum(val.anclas.precio_obj_consenso)}</strong></span>
              </>
            ) : (
              <span>modelo actual <strong>múltiplo {fnum(val.anclas.multiplo_actual)}</strong> × <strong>{metlabel} {fnum(val.anclas.metrica_actual)}</strong></span>
            )}
            <span className="ml-auto">precio actual {fnum(val.precio_actual)}</span>
          </div>

          <div className="grid md:grid-cols-3 gap-2">
            {val.escenarios.map((s) => (
              <div key={s.nombre} className="rounded-md border border-[rgb(var(--border))] p-3 text-sm space-y-1">
                <div className="font-medium capitalize">{s.nombre}</div>
                <div className="text-xs text-[rgb(var(--muted))]">Múltiplo {fnum(s.multiplo)} × {metlabel} {fnum(s.metrica_base_4y)}</div>
                <div>P. objetivo <strong>{fnum(s.precio_objetivo)}</strong></div>
                <div className={s.cagr4_pct != null && s.cagr4_pct >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>
                  CAGR4 {s.cagr4_pct == null ? '—' : fmtPct(s.cagr4_pct, 1)}
                </div>
                <p className="text-xs text-[rgb(var(--muted))] leading-snug">{s.razon}</p>
                {editando === s.nombre ? (
                  <div className="space-y-1 pt-1">
                    <div className="flex gap-1">
                      <input value={mult} onChange={(e) => setMult(e.target.value)} inputMode="decimal"
                        className="w-16 px-1 py-0.5 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]" title="Múltiplo" />
                      <span className="text-xs text-[rgb(var(--muted))]">×</span>
                      <input value={eps} onChange={(e) => setEps(e.target.value)} inputMode="decimal"
                        className="w-16 px-1 py-0.5 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]" title={`${metlabel} 4Y`} />
                    </div>
                    <div className="flex gap-2">
                      <button onClick={() => aplicar(s.nombre)} className="text-xs text-brand-600 dark:text-brand-400 hover:underline">Confirmar</button>
                      <button onClick={() => setEditando(null)} className="text-xs text-[rgb(var(--muted))] hover:underline">Cancelar</button>
                    </div>
                  </div>
                ) : (
                  <button onClick={() => abrir(s.nombre, s.multiplo, s.metrica_base_4y)}
                    className="text-xs text-brand-600 dark:text-brand-400 hover:underline pt-1">
                    {aplicado === s.nombre ? '✓ aplicado · editar' : 'Aplicar a Estimaciones →'}
                  </button>
                )}
              </div>
            ))}
          </div>
          <p className="text-[11px] text-[rgb(var(--muted))] italic">
            {val.disclaimer ?? 'Escenarios orientativos de IA anclados en consenso/histórico; tú fijas los inputs finales.'}
          </p>
        </>
      )}
    </section>
  );
}

function fnum(v: number | string | null | undefined): string {
  if (v == null || v === '') return '—';
  const n = typeof v === 'string' ? parseFloat(v) : v;
  return isNaN(n) ? '—' : n.toFixed(2);
}

function CompsAsistida({ isin }: { isin: string }) {
  const [c, setC] = useState<Comps | null>(null);
  const [estado, setEstado] = useState<EstadoAnalisis>('ninguno');
  const [error, setError] = useState<string | null>(null);
  const cargando = estado === 'en_curso';

  useEffect(() => {
    let vigente = true;
    setC(null); setEstado('ninguno'); setError(null);
    fetchCompsEstado(isin).then((r) => {
      if (!vigente) return;
      setC(r.resultado); setEstado(r.estado); setError(r.error);
    }).catch(() => {});
    return () => { vigente = false; };
  }, [isin]);

  useEffect(() => {
    if (estado !== 'en_curso') return;
    let vigente = true;
    const id = setInterval(async () => {
      try {
        const r = await fetchCompsEstado(isin);
        if (!vigente) return;
        if (r.resultado) setC(r.resultado);
        if (r.estado !== 'en_curso') { setEstado(r.estado); setError(r.error); }
      } catch { /* reintenta */ }
    }, 4000);
    return () => { vigente = false; clearInterval(id); };
  }, [estado, isin]);

  const generar = async () => {
    setError(null);
    try { const r = await lanzarComps(isin); setEstado(r.estado); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-3 max-w-3xl">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
          Comparables del sector{c?.sector ? ` · ${c.sector}` : ''}
        </h3>
        <button onClick={generar} disabled={cargando}
          className="px-3 py-1 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50">
          {cargando ? 'Buscando…' : c ? 'Regenerar comps' : 'Buscar comparables'}
        </button>
      </div>

      {!c && !cargando && (
        <p className="text-xs text-[rgb(var(--muted))]">
          La IA busca pares del sector y sus múltiplos para situar si la empresa está cara o barata.
          Útil antes de abrir/reforzar una posición. Puede tardar unos minutos.
        </p>
      )}
      {error && <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>}
      {cargando && <p className="text-xs text-[rgb(var(--muted))] animate-pulse">La IA está buscando comparables en segundo plano… (puede tardar minutos)</p>}

      {c && c.peers.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-[rgb(var(--muted))] text-right">
                  <th className="text-left font-medium py-1">Empresa</th>
                  <th className="font-medium px-2">PER</th>
                  <th className="font-medium px-2">EV/EBITDA</th>
                  <th className="font-medium px-2">P/FCF</th>
                  <th className="font-medium px-2">Yield</th>
                  <th className="font-medium px-2">Crec.</th>
                  <th className="font-medium px-2">ROIC</th>
                </tr>
              </thead>
              <tbody>
                {c.peers.map((p, i) => (
                  <tr key={`${p.ticker}-${i}`}
                    className={`text-right border-t border-[rgb(var(--border))] ${
                      p.es_objetivo ? 'bg-brand-50/60 dark:bg-brand-900/15 font-medium' : ''}`}>
                    <td className="text-left py-1.5">
                      {p.nombre}{p.ticker ? <span className="text-[rgb(var(--muted))] text-xs"> · {p.ticker}</span> : null}
                      {p.es_objetivo && <span className="ml-1 text-[10px] text-brand-600 dark:text-brand-400">(objetivo)</span>}
                    </td>
                    <td className="px-2 font-mono">{fnum(p.per)}</td>
                    <td className="px-2 font-mono">{fnum(p.ev_ebitda)}</td>
                    <td className="px-2 font-mono">{fnum(p.p_fcf)}</td>
                    <td className="px-2 font-mono">{p.yield_pct == null ? '—' : fmtPct(p.yield_pct, 1)}</td>
                    <td className="px-2 font-mono">{p.crecimiento_pct == null ? '—' : fmtPct(p.crecimiento_pct, 0)}</td>
                    <td className="px-2 font-mono">{p.roic_pct == null ? '—' : fmtPct(p.roic_pct, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {c.lectura && <p className="text-sm leading-snug">{c.lectura}</p>}

          {c.fuentes.length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-[rgb(var(--muted))]">
              <span>Fuentes:</span>
              {c.fuentes.map((f, i) => (
                <a key={i} href={f} target="_blank" rel="noopener noreferrer"
                  className="text-brand-600 dark:text-brand-400 hover:underline truncate max-w-[220px]">{f}</a>
              ))}
            </div>
          )}
          <p className="text-[11px] text-[rgb(var(--muted))] italic">
            {c.disclaimer ?? 'Múltiplos de los pares estimados por IA con búsqueda web; verifica antes de decidir.'}
          </p>
        </>
      )}
      {c && c.peers.length === 0 && estado === 'ok' && (
        <p className="text-sm text-[rgb(var(--muted))]">La IA no devolvió comparables. Prueba a regenerar.</p>
      )}
    </section>
  );
}

function Seccion({ titulo, texto }: { titulo: string; texto: string }) {
  if (!texto) return null;
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wider text-[rgb(var(--muted))] mb-1">{titulo}</h4>
      <p className="text-sm leading-relaxed">{texto}</p>
    </div>
  );
}

function hostOf(u: string): string {
  try { return new URL(u).hostname.replace('www.', ''); } catch { return u; }
}
