'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { HojaRutaReview } from '@/components/HojaRutaReview';
import {
  fetchDistribucionBloques,
  fetchPlanFirmado,
  fetchProyeccionCartera,
  firmarPlan,
  fmtPct,
  prefillEstimaciones,
  proponerEstrategia,
} from '@/lib/api';
import { CAT_COLOR, CAT_LABEL } from '@/lib/categorias';
import { notificarDatosActualizados } from '@/lib/refetch';
import type {
  CategoriaBase,
  PerfilOnboarding,
  PlanFirmado,
  PropuestaEstrategia,
} from '@/lib/types';

const TOLERANCIAS = ['conservador', 'moderado', 'agresivo'];
const FASES = [
  { v: 'acumulacion', t: 'Acumulación (crecer hasta la IF)' },
  { v: 'preservacion', t: 'Preservación (proteger lo logrado)' },
];

export default function OnboardingPage() {
  const [paso, setPaso] = useState<1 | 2 | 3>(1);
  const [objetivoIf, setObjetivoIf] = useState('300000');
  const [horizonte, setHorizonte] = useState('10');
  const [aportacion, setAportacion] = useState('1000');
  const [tolerancia, setTolerancia] = useState('moderado');
  const [fase, setFase] = useState('acumulacion');

  const [propuesta, setPropuesta] = useState<PropuestaEstrategia | null>(null);
  // Perfil exacto con el que se generó `propuesta`, para detectar si el usuario
  // cambió los datos al volver atrás (entonces la propuesta queda obsoleta).
  const [perfilPropuesto, setPerfilPropuesto] = useState<string | null>(null);
  const [objetivos, setObjetivos] = useState<Record<string, number>>({});
  const [proponiendo, setProponiendo] = useState(false);
  const [firmando, setFirmando] = useState(false);
  const [firmado, setFirmado] = useState<PlanFirmado | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tienePosiciones, setTienePosiciones] = useState<boolean | null>(null);
  const [planVigente, setPlanVigente] = useState<PlanFirmado | null>(null);
  // CAGR4+Div proyectado de la cartera (de Estimaciones). Si faltan, se
  // auto-rellenan al entrar para que el diseño se haga con la proyección real.
  const [proyectadaPct, setProyectadaPct] = useState<number | null>(null);
  const [calculandoProy, setCalculandoProy] = useState(false);

  useEffect(() => {
    fetchDistribucionBloques()
      .then((d) => setTienePosiciones(d.bloques.some((b) => b.n_posiciones > 0)))
      .catch(() => setTienePosiciones(null));
    // Si ya hay un plan firmado, precargamos el perfil para rediseñar sobre él.
    fetchPlanFirmado().then((p) => {
      if (!p) return;
      setPlanVigente(p);
      const pf = p.perfil as Record<string, unknown>;
      if (pf.objetivo_if_eur != null) setObjetivoIf(String(pf.objetivo_if_eur));
      if (pf.horizonte_anios != null) setHorizonte(String(pf.horizonte_anios));
      if (pf.aportacion_mensual_eur != null) setAportacion(String(pf.aportacion_mensual_eur));
      if (typeof pf.tolerancia === 'string') setTolerancia(pf.tolerancia);
      if (typeof pf.fase === 'string') setFase(pf.fase);
    }).catch(() => {});
  }, []);

  // Al entrar: si faltan estimaciones (no hay CAGR proyectada fiable), se
  // rellenan automáticamente para diseñar la estrategia con la proyección real
  // de la cartera (no solo con el retorno requerido).
  useEffect(() => {
    let vivo = true;
    (async () => {
      try {
        let proy = await fetchProyeccionCartera();
        if (vivo) setProyectadaPct(proy.cagr_proyectada_pct);
        if (!proy.completa) {
          if (vivo) setCalculandoProy(true);
          await prefillEstimaciones().catch(() => {});
          proy = await fetchProyeccionCartera();
          if (vivo) setProyectadaPct(proy.cagr_proyectada_pct);
        }
      } catch {
        /* sin cartera o sin red: el wizard sigue sin la proyección */
      } finally {
        if (vivo) setCalculandoProy(false);
      }
    })();
    return () => { vivo = false; };
  }, []);

  const perfil = (): PerfilOnboarding => ({
    objetivo_if_eur: objetivoIf.trim() || null,
    horizonte_anios: horizonte.trim() ? parseFloat(horizonte.replace(',', '.')) : null,
    aportacion_mensual_eur: aportacion.trim() || null,
    tolerancia, fase,
  });

  const proponer = async () => {
    setProponiendo(true); setError(null);
    try {
      const p = await proponerEstrategia(perfil());
      setPropuesta(p);
      setPerfilPropuesto(JSON.stringify(perfil()));
      const obj: Record<string, number> = {};
      for (const b of p.bloques) obj[b.categoria_base] = b.peso_objetivo;
      setObjetivos(obj);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setProponiendo(false);
    }
  };

  const firmar = async () => {
    setFirmando(true); setError(null);
    try {
      const pf = await firmarPlan(perfil(), objetivos);
      setFirmado(pf);
      notificarDatosActualizados();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFirmando(false);
    }
  };

  const sumaPct = Object.values(objetivos).reduce((s, v) => s + v, 0);
  // La propuesta vigente quedó obsoleta si el perfil cambió desde que se generó.
  const propuestaObsoleta = propuesta != null && perfilPropuesto !== JSON.stringify(perfil());

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">
          {planVigente ? 'Rediseña tu estrategia' : 'Diseña tu estrategia'}
        </h2>
        <p className="text-sm text-[rgb(var(--muted))] mt-1">
          Define tu perfil, deja que la IA proponga un reparto por bloques, ajústalo y fírmalo.
          El plan firmado guía tus compras y la IA te lo recordará cuando vayas a desviarte.
        </p>
      </div>

      {planVigente && !firmado && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3 text-sm">
          Tienes un plan firmado <span className="font-medium">v{planVigente.version}</span>
          {planVigente.fecha && <> · {planVigente.fecha.slice(0, 10)}</>}. Hemos precargado tu
          perfil. Rediseñarlo guarda una <strong>nueva versión</strong>: solo se actualizan los
          objetivos por bloque — tus posiciones y su clasificación no se tocan.
        </div>
      )}

      {calculandoProy && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3 text-sm text-[rgb(var(--muted))] animate-pulse">
          ·· calculando la proyección (CAGR4+Div) de tu cartera ··
        </div>
      )}

      <Pasos paso={paso} />

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {/* Paso 1 — Perfil */}
      {paso === 1 && (
        <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-4">
          {tienePosiciones === false && (
            <div className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3 text-sm text-amber-800 dark:text-amber-300">
              ¿Ya tienes una cartera? <Link href="/" className="underline font-medium">Importa
              tus extractos del broker primero</Link> para que la IA tenga en cuenta lo que ya
              tienes y el hueco que falta. Si empiezas de cero, continúa.
            </div>
          )}
          <Campo label="Objetivo de independencia financiera (€)">
            <input value={objetivoIf} onChange={(e) => setObjetivoIf(e.target.value)}
              inputMode="numeric" className={INPUT} />
          </Campo>
          <Campo label="Horizonte (años)">
            <input value={horizonte} onChange={(e) => setHorizonte(e.target.value)}
              inputMode="decimal" placeholder="p.ej. 2,5" className={INPUT} />
          </Campo>
          <Campo label="Aportación mensual (€)">
            <input value={aportacion} onChange={(e) => setAportacion(e.target.value)}
              inputMode="numeric" className={INPUT} />
          </Campo>
          <Campo label="Tolerancia al riesgo">
            <select value={tolerancia} onChange={(e) => setTolerancia(e.target.value)} className={INPUT}>
              {TOLERANCIAS.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </Campo>
          <Campo label="Fase">
            <select value={fase} onChange={(e) => setFase(e.target.value)} className={INPUT}>
              {FASES.map((f) => <option key={f.v} value={f.v}>{f.t}</option>)}
            </select>
          </Campo>
          <div className="flex justify-end">
            <button onClick={() => setPaso(2)} className={BTN}>Siguiente →</button>
          </div>
        </section>
      )}

      {/* Paso 2 — Propuesta IA */}
      {paso === 2 && (
        <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-4">
          {!propuesta || propuestaObsoleta ? (
            <div className="text-center py-6">
              <p className="text-sm text-[rgb(var(--muted))] mb-4">
                {propuestaObsoleta
                  ? 'Has cambiado tu perfil. Vuelve a proponer para actualizar el reparto.'
                  : 'La IA propondrá un reparto por bloque según tu perfil. Tú lo ajustas después.'}
              </p>
              <button onClick={proponer} disabled={proponiendo} className={`${BTN} disabled:opacity-50`}>
                {proponiendo ? '·· la IA está pensando ··' : propuestaObsoleta ? 'Volver a proponer' : 'Proponer con IA'}
              </button>
            </div>
          ) : (
            <>
              {propuesta.viabilidad && (
                <div className={`rounded-lg border p-3 text-sm ${
                  propuesta.viabilidad.viable
                    ? 'border-[rgb(var(--border))] bg-[rgb(var(--bg))]'
                    : 'border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-900/20'
                }`}>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[rgb(var(--muted))]">Retorno anual requerido (aprox.)</span>
                    <span className={`font-mono font-semibold ${
                      propuesta.viabilidad.viable ? '' : 'text-rose-600 dark:text-rose-400'
                    }`}>
                      {propuesta.viabilidad.cagr_requerido_pct == null
                        ? '—'
                        : fmtPct(propuesta.viabilidad.cagr_requerido_pct, 1)}
                    </span>
                  </div>
                  {(() => {
                    const proy = propuesta.viabilidad.cagr_proyectada_pct ?? proyectadaPct;
                    const req = propuesta.viabilidad.cagr_requerido_pct;
                    if (proy == null) return null;
                    const gap = req != null && proy < req - 0.001;
                    return (
                      <div className="flex items-center justify-between gap-3 mt-1">
                        <span className="text-[rgb(var(--muted))]">Proyección de tu cartera (CAGR4+Div)</span>
                        <span className={`font-mono font-semibold ${gap ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'}`}>
                          {fmtPct(proy, 1)}{gap ? ' ⚠ gap' : ''}
                          {propuesta.viabilidad.cobertura_estim != null && propuesta.viabilidad.cobertura_estim < 0.8
                            ? <span className="text-[10px] text-[rgb(var(--muted))] font-sans ml-1">(parcial)</span>
                            : null}
                        </span>
                      </div>
                    );
                  })()}
                  <p className="mt-1 text-xs text-[rgb(var(--muted))]">
                    {propuesta.viabilidad.veredicto}
                  </p>
                </div>
              )}
              {propuesta.resumen && (
                <p className="text-sm italic text-[rgb(var(--muted))]">{propuesta.resumen}</p>
              )}
              <div className="space-y-2">
                {propuesta.bloques.map((b) => (
                  <div key={b.categoria_base} className="rounded border border-[rgb(var(--border))] p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className={`text-xs px-2 py-0.5 rounded ${CAT_COLOR[b.categoria_base as CategoriaBase]}`}>
                        {CAT_LABEL[b.categoria_base as CategoriaBase]}
                      </span>
                      <div className="flex items-center gap-1">
                        <input
                          value={Math.round((objetivos[b.categoria_base] ?? 0) * 100)}
                          onChange={(e) => {
                            const n = parseFloat(e.target.value);
                            setObjetivos((o) => ({ ...o, [b.categoria_base]: isNaN(n) ? 0 : n / 100 }));
                          }}
                          inputMode="numeric"
                          className="w-16 text-right text-sm px-2 py-1 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
                        />
                        <span className="text-sm text-[rgb(var(--muted))]">%</span>
                      </div>
                    </div>
                    <p className="text-xs text-[rgb(var(--muted))] mt-1.5">{b.razon}</p>
                  </div>
                ))}
              </div>
              <div className="text-xs text-[rgb(var(--muted))] text-right">
                Suma objetivos: {fmtPct(sumaPct, 0)}
              </div>
              {propuesta.disclaimer && (
                <p className="text-[11px] text-[rgb(var(--muted))] border-t border-[rgb(var(--border))] pt-2">
                  ⚠ {propuesta.disclaimer}
                </p>
              )}
              <div className="flex justify-between">
                <button onClick={() => setPaso(1)} className={BTN_SEC}>← Perfil</button>
                <button onClick={() => setPaso(3)} className={BTN}>Revisar y firmar →</button>
              </div>
            </>
          )}
        </section>
      )}

      {/* Paso 3 — Firmar */}
      {paso === 3 && (
        <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 space-y-4">
          {firmado ? (
            <HojaRutaReview />
          ) : (
            <>
              <p className="text-sm text-[rgb(var(--muted))]">
                Vas a fijar estos objetivos por bloque y firmar tu plan. Podrás ajustarlo cuando
                quieras (se guarda una nueva versión).
              </p>
              <div className="space-y-1">
                {Object.entries(objetivos).map(([cat, peso]) => (
                  <div key={cat} className="flex items-center justify-between text-sm">
                    <span className={`text-xs px-2 py-0.5 rounded ${CAT_COLOR[cat as CategoriaBase]}`}>
                      {CAT_LABEL[cat as CategoriaBase]}
                    </span>
                    <span className="font-mono tabular-nums">{fmtPct(peso, 0)}</span>
                  </div>
                ))}
              </div>
              <div className="flex justify-between">
                <button onClick={() => setPaso(2)} className={BTN_SEC}>← Propuesta</button>
                <button onClick={firmar} disabled={firmando} className={`${BTN} disabled:opacity-50`}>
                  {firmando ? 'Firmando…' : 'Firmar mi plan'}
                </button>
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}

const INPUT = 'w-full px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]';
const BTN = 'px-4 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700';
const BTN_SEC = 'px-4 py-1.5 text-sm rounded border border-[rgb(var(--border))]';

function Campo({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-sm font-medium mb-1">{label}</span>
      {children}
    </label>
  );
}

function Pasos({ paso }: { paso: 1 | 2 | 3 }) {
  const items = ['Perfil', 'Propuesta IA', 'Firmar'];
  return (
    <div className="flex items-center gap-2 text-xs">
      {items.map((t, i) => {
        const n = (i + 1) as 1 | 2 | 3;
        const activo = n === paso;
        const hecho = n < paso;
        return (
          <div key={t} className="flex items-center gap-2">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded ${
              activo ? 'bg-brand-600 text-white'
                : hecho ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
                : 'bg-[rgb(var(--border))]/50 text-[rgb(var(--muted))]'
            }`}>
              {hecho ? '✓' : n} {t}
            </span>
            {i < items.length - 1 && <span className="text-[rgb(var(--muted))]">→</span>}
          </div>
        );
      })}
    </div>
  );
}
