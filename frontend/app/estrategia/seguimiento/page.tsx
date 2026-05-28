'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  anadirSeguimiento,
  asignarBloque,
  auditarCompra,
  crearBloque,
  crearPaso,
  editarEstimacion,
  evaluarCandidato,
  fetchDistribucionBloques,
  fetchSeguimiento,
  fmtEUR,
  fmtNum,
  fmtPct,
  quitarSeguimiento,
  sugerirBloque,
} from '@/lib/api';
import { AuditoriaVista } from '@/components/AuditoriaVista';
import { CAT_COLOR, CAT_LABEL } from '@/lib/categorias';
import { PRIORIDADES, PRIORIDAD_LABEL } from '@/lib/decisiones';
import { notificarDatosActualizados } from '@/lib/refetch';
import type {
  Auditoria,
  BloqueDist,
  CategoriaBase,
  EvaluacionCandidato,
  PrioridadPlan,
  SeguimientoItem,
  SugerenciaBloque,
} from '@/lib/types';

export default function SeguimientoPage() {
  const [items, setItems] = useState<SeguimientoItem[]>([]);
  const [bloques, setBloques] = useState<BloqueDist[]>([]);
  const [ticker, setTicker] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);
  const [sugerencias, setSugerencias] = useState<Record<string, SugerenciaBloque>>({});
  const [sugiriendo, setSugiriendo] = useState<Record<string, boolean>>({});
  const [compraIsin, setCompraIsin] = useState<string | null>(null);
  const [evaluaciones, setEvaluaciones] = useState<Record<string, EvaluacionCandidato>>({});
  const [evaluando, setEvaluando] = useState<Record<string, boolean>>({});
  // Bloque-objetivo: si llegas desde el déficit de un bloque (Bloques → "Buscar
  // candidato"), evaluamos contra él y avisamos si el candidato no lo cubre.
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    setTarget(new URLSearchParams(window.location.search).get('bloque'));
  }, []);

  const cargar = useCallback(async () => {
    try {
      const [segs, dist] = await Promise.all([fetchSeguimiento(), fetchDistribucionBloques()]);
      setItems(segs);
      setBloques(dist.bloques.filter((b) => b.id !== 'sin_clasificar'));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { cargar(); }, [cargar]);

  const anadir = async () => {
    const t = ticker.trim().toUpperCase();
    if (!t) return;
    setCargando(true);
    try {
      await anadirSeguimiento(t);
      setTicker('');
      await cargar();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCargando(false);
    }
  };

  const quitar = async (isin: string) => {
    try { await quitarSeguimiento(isin); await cargar(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const guardar = async (isin: string, campos: Record<string, unknown>) => {
    try { await editarEstimacion(isin, campos); await cargar(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const catDeBloque = (id: string | null): CategoriaBase | null =>
    bloques.find((b) => b.id === id)?.categoria_base ?? null;

  const asignar = async (isin: string, bloqueId: string | null) => {
    const sug = sugerencias[isin];
    try {
      await asignarBloque(isin, bloqueId, sug
        ? { categoriaSugerida: sug.categoria_base, confianzaIa: sug.confianza }
        : {});
      await cargar();
      notificarDatosActualizados();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const sugerir = async (isin: string) => {
    setSugiriendo((prev) => ({ ...prev, [isin]: true }));
    try {
      const s = await sugerirBloque(isin);
      setSugerencias((prev) => ({ ...prev, [isin]: s }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSugiriendo((prev) => ({ ...prev, [isin]: false }));
    }
  };

  // Bloque de la cartera que corresponde a la categoría sugerida (o undefined).
  const bloqueDeSugerencia = (sug: SugerenciaBloque): string | undefined =>
    (sug.bloque_id && bloques.some((b) => b.id === sug.bloque_id) ? sug.bloque_id : undefined)
    ?? bloques.find((b) => b.categoria_base === sug.categoria_base)?.id;

  const aplicarSugerencia = async (isin: string) => {
    const sug = sugerencias[isin];
    if (!sug) return;
    const bid = bloqueDeSugerencia(sug);
    if (!bid) {
      setError(`No tienes un bloque de la categoría "${CAT_LABEL[sug.categoria_base]}". ` +
        'Pulsa "crear y asignar" o créalo en Bloques.');
      return;
    }
    await asignar(isin, bid);
  };

  // Crea el bloque de la categoría sugerida y asigna el candidato de un golpe.
  const crearYAsignar = async (isin: string) => {
    const sug = sugerencias[isin];
    if (!sug) return;
    try {
      const nuevo = await crearBloque(CAT_LABEL[sug.categoria_base], sug.categoria_base);
      await asignar(isin, nuevo.id);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const evaluar = async (isin: string) => {
    if (evaluaciones[isin]) {            // ya mostrada → toggle (cerrar)
      setEvaluaciones((prev) => { const n = { ...prev }; delete n[isin]; return n; });
      return;
    }
    setEvaluando((prev) => ({ ...prev, [isin]: true }));
    try {
      const ev = await evaluarCandidato(isin, target);
      setEvaluaciones((prev) => ({ ...prev, [isin]: ev }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setEvaluando((prev) => ({ ...prev, [isin]: false }));
    }
  };

  const planear = async (isin: string, capital: string, prioridad: PrioridadPlan) => {
    try {
      await crearPaso({
        isin, decision: 'COMPRAR', prioridad,
        capital_objetivo_eur: capital.trim() ? capital.trim() : null,
      });
      setCompraIsin(null);
      notificarDatosActualizados();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="space-y-5">
      <p className="text-sm text-[rgb(var(--muted))] max-w-2xl">
        Empresas que sigues sin tener en cartera. Añade por <strong>ticker</strong>, clasifícalas en
        un bloque (la IA puede sugerirlo) y <strong>planea su compra</strong> — aparecerá en el
        plan y en el hueco de asignación.
      </p>

      {target && (
        <div className="rounded-lg border border-brand-200 bg-brand-50/60 dark:bg-brand-900/15 dark:border-brand-800 p-3 text-sm">
          Buscando candidato para{' '}
          <span className="font-medium">{CAT_LABEL[target as CategoriaBase] ?? target}</span>.
          Pulsa <strong>evaluar</strong> en un valor para ver si encaja y cumple sus criterios.
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') anadir(); }}
          placeholder="Ticker (NVDA, AAPL…)"
          className="bg-[rgb(var(--bg))] border border-[rgb(var(--border))] rounded px-2 py-1.5 text-sm w-44 uppercase"
        />
        <button
          onClick={anadir}
          disabled={cargando || !ticker.trim()}
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {cargando ? 'Añadiendo…' : 'Añadir al seguimiento'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {items.length === 0 && !error && (
        <p className="text-sm text-[rgb(var(--muted))]">
          Aún no sigues ninguna empresa. Añade un ticker arriba.
        </p>
      )}

      {items.length > 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-auto max-h-[72vh]">
          <table className="w-full text-xs">
            <thead className="text-[rgb(var(--muted))]">
              <tr className="text-left">
                <th className={`${TH} py-2 px-2`}>Empresa</th>
                <th className={`${TH} px-2`}>Bloque</th>
                <th className={`${TH} px-2 text-right`}>Precio</th>
                <th className={`${TH} px-2 text-right`}>Múltiplo</th>
                <th className={`${TH} px-2 text-right`}>Métrica 4A</th>
                <th className={`${TH} px-2 text-right`}>Precio obj.</th>
                <th className={`${TH} px-2 text-right`}>CAGR4</th>
                <th className={`${TH} px-2 text-right`}>Yield</th>
                <th className={`${TH} px-2 text-right`}>CAGR4+Div</th>
                <th className={`${TH} px-2`}></th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {items.map((s) => (
                <Fila
                  key={s.isin}
                  s={s}
                  bloques={bloques}
                  catDeBloque={catDeBloque}
                  sugerencia={sugerencias[s.isin]}
                  cargandoSug={!!sugiriendo[s.isin]}
                  bloqueSugId={sugerencias[s.isin] ? bloqueDeSugerencia(sugerencias[s.isin]) : undefined}
                  compraAbierta={compraIsin === s.isin}
                  target={target}
                  evaluacion={evaluaciones[s.isin]}
                  evaluando={!!evaluando[s.isin]}
                  onQuitar={quitar}
                  onGuardar={guardar}
                  onAsignar={asignar}
                  onSugerir={sugerir}
                  onAplicarSugerencia={aplicarSugerencia}
                  onCrearYAsignar={crearYAsignar}
                  onToggleCompra={() => setCompraIsin(compraIsin === s.isin ? null : s.isin)}
                  onEvaluar={evaluar}
                  onPlanear={planear}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const TH = 'sticky top-0 z-10 bg-[rgb(var(--card))] border-b border-[rgb(var(--border))] ' +
  'shadow-[0_1px_0_rgb(var(--border))]';

function Fila({
  s, bloques, catDeBloque, sugerencia, cargandoSug, bloqueSugId, compraAbierta, target,
  evaluacion, evaluando,
  onQuitar, onGuardar, onAsignar, onSugerir, onAplicarSugerencia, onCrearYAsignar,
  onToggleCompra, onEvaluar, onPlanear,
}: {
  s: SeguimientoItem;
  bloques: BloqueDist[];
  catDeBloque: (id: string | null) => CategoriaBase | null;
  sugerencia?: SugerenciaBloque;
  cargandoSug: boolean;
  bloqueSugId?: string;
  compraAbierta: boolean;
  target: string | null;
  evaluacion?: EvaluacionCandidato;
  evaluando: boolean;
  onQuitar: (isin: string) => Promise<void>;
  onGuardar: (isin: string, campos: Record<string, unknown>) => Promise<void>;
  onAsignar: (isin: string, bloqueId: string | null) => Promise<void>;
  onSugerir: (isin: string) => Promise<void>;
  onAplicarSugerencia: (isin: string) => Promise<void>;
  onCrearYAsignar: (isin: string) => Promise<void>;
  onToggleCompra: () => void;
  onEvaluar: (isin: string) => Promise<void>;
  onPlanear: (isin: string, capital: string, prioridad: PrioridadPlan) => Promise<void>;
}) {
  const e = s.estimacion;
  const pct = (v: string | null, dp = 1) => (v == null ? '—' : fmtPct(v, dp));
  const colorPct = (v: string | null) =>
    v == null ? 'text-[rgb(var(--muted))]'
      : parseFloat(v) >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400';
  const precio = (v: string | null) => {
    if (v == null) return '—';
    const d = e.divisa ?? s.divisa;
    return !d || d === 'EUR' ? fmtEUR(v, { maximumFractionDigits: 2 })
      : `${fmtNum(v, { maximumFractionDigits: 2 })} ${d}`;
  };
  const cat = catDeBloque(s.bloque_id);

  return (
    <>
      <tr className="border-t border-[rgb(var(--border))]/30">
        <td className="py-1 px-2 font-sans">
          <div className="max-w-[180px] truncate">{s.nombre || s.ticker}</div>
          <div className="text-[rgb(var(--muted))] text-[10px]">{s.ticker} · {s.divisa}</div>
        </td>
        <td className="px-2 font-sans align-top">
          <div className="flex items-center gap-1">
            <select
              value={s.bloque_id ?? ''}
              onChange={(ev) => onAsignar(s.isin, ev.target.value || null)}
              className={`text-[11px] rounded px-1 py-0.5 border border-[rgb(var(--border))] bg-[rgb(var(--bg))] max-w-[120px] ${
                cat ? CAT_COLOR[cat] : ''
              }`}
            >
              <option value="">— sin bloque —</option>
              {bloques.map((b) => (
                <option key={b.id} value={b.id}>{b.nombre}</option>
              ))}
            </select>
            <button
              onClick={() => onSugerir(s.isin)}
              disabled={cargandoSug}
              title="Sugerir bloque con la IA"
              className="text-[10px] px-1 py-0.5 rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] disabled:opacity-50"
            >
              {cargandoSug ? '·· IA ··' : 'IA'}
            </button>
          </div>
          {cargandoSug && !sugerencia && (
            <div className="mt-1 text-[10px] text-[rgb(var(--muted))] animate-pulse">
              la IA está analizando…
            </div>
          )}
          {sugerencia && (
            <div className="mt-1 text-[10px] text-[rgb(var(--muted))] max-w-[200px]">
              <span className={`px-1 py-0.5 rounded ${CAT_COLOR[sugerencia.categoria_base]}`}>
                {CAT_LABEL[sugerencia.categoria_base]}
              </span>{' '}
              {Math.round(sugerencia.confianza * 100)}%{' '}
              {bloqueSugId ? (
                <button
                  onClick={() => onAplicarSugerencia(s.isin)}
                  className="text-brand-600 dark:text-brand-400 hover:underline"
                >
                  aplicar
                </button>
              ) : (
                <button
                  onClick={() => onCrearYAsignar(s.isin)}
                  className="text-brand-600 dark:text-brand-400 hover:underline"
                  title={`No tienes un bloque "${CAT_LABEL[sugerencia.categoria_base]}"; se creará y se asignará.`}
                >
                  crear bloque y asignar
                </button>
              )}
              <div className="italic leading-tight mt-0.5">{sugerencia.razonamiento}</div>
            </div>
          )}
        </td>
        <td className="px-2 text-right text-[rgb(var(--muted))]">{precio(e.precio_actual)}</td>
        <EditNum isin={s.isin} campo="multiplo_objetivo" valor={e.multiplo_objetivo}
          onGuardar={onGuardar} alerta={e.mult_alerta} />
        <EditNum isin={s.isin} campo="metrica_base_4y" valor={e.metrica_base_4y}
          onGuardar={onGuardar} />
        <td className="px-2 text-right">{precio(e.precio_objetivo)}</td>
        <td className={`px-2 text-right ${colorPct(e.cagr4_pct)}`}>{pct(e.cagr4_pct)}</td>
        <td className="px-2 text-right text-[rgb(var(--muted))]">{pct(e.div_yield_pct, 2)}</td>
        <td className={`px-2 text-right font-semibold ${colorPct(e.cagr4_div_pct)}`}>{pct(e.cagr4_div_pct)}</td>
        <td className="px-2 text-right whitespace-nowrap font-sans">
          <button
            onClick={() => onEvaluar(s.isin)}
            disabled={evaluando}
            title="¿Encaja en su bloque y cumple sus criterios? (IA + métricas)"
            className="text-[11px] text-brand-600 dark:text-brand-400 hover:underline mr-2 disabled:opacity-50"
          >
            {evaluando ? '·· IA ··' : evaluacion ? 'ocultar' : 'evaluar'}
          </button>
          <Link
            href={`/estrategia/analisis?isin=${encodeURIComponent(s.isin)}`}
            title="Estudio a fondo: one-pager + valoración (pestaña Análisis)"
            className="text-[11px] text-brand-600 dark:text-brand-400 hover:underline mr-2"
          >
            analizar →
          </Link>
          <button
            onClick={onToggleCompra}
            className="text-[11px] text-brand-600 dark:text-brand-400 hover:underline mr-2"
          >
            {compraAbierta ? 'cerrar' : 'planificar compra'}
          </button>
          <button
            onClick={() => onQuitar(s.isin)}
            className="text-[rgb(var(--muted))] hover:text-rose-600 text-sm"
            title="Quitar del seguimiento"
          >
            ✕
          </button>
        </td>
      </tr>
      {evaluacion && (
        <tr className="bg-[rgb(var(--bg))]">
          <td colSpan={10} className="px-3 py-2 font-sans">
            <EvaluacionPanel ev={evaluacion} />
          </td>
        </tr>
      )}
      {compraAbierta && (
        <tr className="bg-brand-50/40 dark:bg-brand-900/10">
          <td colSpan={10} className="px-3 py-2 font-sans">
            <FormCompra isin={s.isin} nombre={s.nombre || s.ticker} target={target}
              onPlanear={(capital, prioridad) => onPlanear(s.isin, capital, prioridad)} />
          </td>
        </tr>
      )}
    </>
  );
}

function FormCompra({ isin, nombre, target, onPlanear }: {
  isin: string;
  nombre: string;
  target: string | null;
  onPlanear: (capital: string, prioridad: PrioridadPlan) => Promise<void>;
}) {
  const [capital, setCapital] = useState('');
  const [prioridad, setPrioridad] = useState<PrioridadPlan>('MEDIA');
  const [audit, setAudit] = useState<Auditoria | null>(null);
  const [auditando, setAuditando] = useState(true);
  useEffect(() => {
    setAuditando(true);
    auditarCompra(isin, target)
      .then(setAudit).catch(() => setAudit(null)).finally(() => setAuditando(false));
  }, [isin, target]);
  return (
    <div className="space-y-3">
      {auditando ? (
        <div className="text-xs text-[rgb(var(--muted))] animate-pulse">auditando la compra…</div>
      ) : audit && <AuditoriaVista a={audit} />}
      <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-[rgb(var(--muted))]">Planificar compra de <strong>{nombre}</strong>:</span>
      <input
        value={capital}
        onChange={(e) => setCapital(e.target.value)}
        placeholder="Capital € (opc.)"
        inputMode="decimal"
        className="px-2 py-1 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-32"
      />
      <select
        value={prioridad}
        onChange={(e) => setPrioridad(e.target.value as PrioridadPlan)}
        className="px-2 py-1 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
      >
        {PRIORIDADES.map((p) => (
          <option key={p} value={p}>{PRIORIDAD_LABEL[p]}</option>
        ))}
      </select>
      <button
        onClick={() => onPlanear(capital, prioridad)}
        className="px-3 py-1 rounded bg-brand-600 text-white hover:bg-brand-700"
      >
        Planificar compra
      </button>
      </div>
    </div>
  );
}


function EvaluacionPanel({ ev }: { ev: EvaluacionCandidato }) {
  const noCubre = ev.cubre_target === false;
  return (
    <div className="text-xs space-y-1.5 max-w-2xl">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[rgb(var(--muted))]">Encaja en:</span>
        <span className={`px-1.5 py-0.5 rounded ${CAT_COLOR[ev.categoria_sugerida]}`}>
          {CAT_LABEL[ev.categoria_sugerida]}
        </span>
        <span className="text-[rgb(var(--muted))]">conf. {Math.round(ev.confianza * 100)}%</span>
      </div>
      {ev.razonamiento && <div className="italic text-[rgb(var(--muted))]">{ev.razonamiento}</div>}
      {ev.checks.length > 0 ? (
        <ul className="space-y-0.5 font-mono">
          {ev.checks.map((c) => (
            <li key={c.etiqueta} className="flex items-center gap-2">
              <span
                className={
                  c.cumple === true ? 'text-emerald-600 dark:text-emerald-400'
                    : c.cumple === false ? 'text-rose-600 dark:text-rose-400'
                      : 'text-[rgb(var(--muted))]'
                }
              >
                {c.cumple === true ? '✓' : c.cumple === false ? '✗' : '·'}
              </span>
              <span>{c.etiqueta}: <strong>{c.valor_txt}</strong></span>
              <span className="text-[rgb(var(--muted))]">(obj. {c.objetivo_txt})</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-[rgb(var(--muted))]">Sin criterios medibles para este bloque.</div>
      )}
      <div className="italic text-[rgb(var(--muted))]">{ev.cualitativo}</div>
      <div className={`font-medium ${noCubre ? 'text-amber-700 dark:text-amber-400' : ''}`}>
        {noCubre && '⚠ '}{ev.veredicto}
      </div>
    </div>
  );
}

function EditNum({ isin, campo, valor, onGuardar, alerta }: {
  isin: string; campo: string; valor: string | null;
  onGuardar: (isin: string, campos: Record<string, unknown>) => Promise<void>;
  alerta?: string | null;
}) {
  const [v, setV] = useState(valor ?? '');
  useEffect(() => { setV(valor ?? ''); }, [valor]);
  return (
    <td className="px-2 text-right">
      <div className="flex items-center justify-end gap-1">
        {alerta && (
          <span
            role="img"
            aria-label={`Aviso: ${alerta}`}
            title={alerta}
            className="cursor-help text-amber-500 dark:text-amber-400 text-sm leading-none select-none"
          >
            ⚠️
          </span>
        )}
        <input
          value={v}
          onChange={(ev) => setV(ev.target.value)}
          onBlur={() => {
            const n = v.trim() ? parseFloat(v.replace(',', '.')) : null;
            const actual = valor != null ? parseFloat(valor) : null;
            if (n !== actual) onGuardar(isin, { [campo]: n });
          }}
          inputMode="decimal"
          className="w-16 text-right bg-[rgb(var(--bg))] border border-[rgb(var(--border))] rounded px-1 py-0.5"
        />
      </div>
    </td>
  );
}
