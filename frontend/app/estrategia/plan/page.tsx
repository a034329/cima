'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  actualizarPaso,
  auditarCompra,
  auditarVenta,
  crearPaso,
  eliminarPaso,
  evaluarFriccion,
  fetchPlanPasos,
  fetchPosicionesPlan,
  fmtEUR,
} from '@/lib/api';
import { AuditoriaVista } from '@/components/AuditoriaVista';
import {
  DECISION_COLOR,
  DECISION_LABEL,
  DECISIONES,
  PRIORIDAD_LABEL,
  PRIORIDADES,
} from '@/lib/decisiones';
import type {
  Auditoria,
  DecisionPlan,
  EstadoPlan,
  FriccionResultado,
  PasoPlan,
  PosicionPlan,
  PrioridadPlan,
} from '@/lib/types';

const ESTADO_LABEL: Record<EstadoPlan, string> = {
  PENDIENTE: 'Pendiente',
  EN_CURSO: 'En curso',
  COMPLETADO: 'Completado',
  CANCELADO: 'Cancelado',
};
const ESTADOS: EstadoPlan[] = ['PENDIENTE', 'EN_CURSO', 'COMPLETADO', 'CANCELADO'];

export default function PlanPage() {
  const [posiciones, setPosiciones] = useState<PosicionPlan[] | null>(null);
  const [pasos, setPasos] = useState<PasoPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editIsin, setEditIsin] = useState<string | null>(null);

  const cargar = useCallback(async () => {
    try {
      const [p, s] = await Promise.all([fetchPosicionesPlan(), fetchPlanPasos()]);
      setPosiciones(p);
      setPasos(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    cargar();
  }, [cargar]);

  const nombrePorIsin = (isin: string) =>
    posiciones?.find((p) => p.isin === isin)?.nombre ?? isin;

  const renderFila = (s: PasoPlan) => (
    <tr key={s.id} className="border-t border-[rgb(var(--border))]/40">
      <td className="py-1.5 px-3">{nombrePorIsin(s.isin)}</td>
      <td className="px-3">
        <span className={`text-xs px-2 py-0.5 rounded ${DECISION_COLOR[s.decision]}`}>
          {DECISION_LABEL[s.decision]}
        </span>
      </td>
      <td className="px-3 text-[rgb(var(--muted))]">{PRIORIDAD_LABEL[s.prioridad]}</td>
      <td className="px-3 text-right font-mono tabular-nums">
        {s.capital_objetivo_eur ? fmtEUR(s.capital_objetivo_eur) : '—'}
      </td>
      <td className="px-3">
        <select
          value={s.estado}
          onChange={async (e) => { await actualizarPaso(s.id, { estado: e.target.value }); await cargar(); }}
          className="px-2 py-1 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
        >
          {ESTADOS.map((es) => (<option key={es} value={es}>{ESTADO_LABEL[es]}</option>))}
        </select>
      </td>
      <td className="px-3 text-right">
        <button
          onClick={async () => { await eliminarPaso(s.id); await cargar(); }}
          className="text-xs text-rose-600 dark:text-rose-400 hover:underline"
        >
          eliminar
        </button>
      </td>
    </tr>
  );

  // "Próximos pasos" = solo activos. Los CANCELADO/COMPLETADO son auditoría → historial.
  const activos = (pasos ?? []).filter((p) => p.estado === 'PENDIENTE' || p.estado === 'EN_CURSO');
  const historial = (pasos ?? []).filter((p) => p.estado === 'COMPLETADO' || p.estado === 'CANCELADO');

  return (
    <div className="space-y-8">
      <p className="text-sm text-[rgb(var(--muted))]">
        Una decisión por cada valor según tu estrategia. La decisión vigente es la del paso
        activo de mayor prioridad; sin paso → Mantener.
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {/* Tabla por valor */}
      <section>
        <h3 className="text-lg font-semibold mb-3">Plan por valor</h3>
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-[rgb(var(--muted))] border-b border-[rgb(var(--border))]">
              <tr className="text-left">
                <th className="py-2 px-3">Posición</th>
                <th className="px-3">Bloque</th>
                <th className="px-3 text-right">Valor</th>
                <th className="px-3">Decisión</th>
                <th className="px-3 text-right">Capital obj.</th>
                <th className="px-3"></th>
              </tr>
            </thead>
            <tbody>
              {posiciones?.map((p) => (
                <tr key={p.isin} className="border-t border-[rgb(var(--border))]/40">
                  <td className="py-1.5 px-3">
                    {p.nombre}
                    {!p.en_cartera && (
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-400">
                        compra planeada
                      </span>
                    )}
                  </td>
                  <td className="px-3 text-[rgb(var(--muted))]">{p.bloque_nombre ?? '—'}</td>
                  <td className="px-3 text-right font-mono tabular-nums">
                    {p.en_cartera ? fmtEUR(p.valor_eur) : '—'}
                  </td>
                  <td className="px-3">
                    <span className={`text-xs px-2 py-0.5 rounded ${DECISION_COLOR[p.decision]}`}>
                      {DECISION_LABEL[p.decision]}
                    </span>
                    {p.razon && (
                      <span className="ml-2 text-xs text-[rgb(var(--muted))]" title={p.razon}>
                        · {p.razon.length > 24 ? p.razon.slice(0, 24) + '…' : p.razon}
                      </span>
                    )}
                  </td>
                  <td className="px-3 text-right font-mono tabular-nums">
                    {p.capital_objetivo_eur ? fmtEUR(p.capital_objetivo_eur) : '—'}
                  </td>
                  <td className="px-3 text-right">
                    <button
                      onClick={() => setEditIsin(editIsin === p.isin ? null : p.isin)}
                      className="text-xs text-brand-600 dark:text-brand-400 hover:underline"
                    >
                      {editIsin === p.isin ? 'cerrar' : '+ paso'}
                    </button>
                  </td>
                </tr>
              ))}
              {posiciones && posiciones.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-4 px-3 text-center text-[rgb(var(--muted))]">
                    No hay posiciones abiertas. Importa un extracto en Cartera.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {!posiciones && !error && <p className="text-sm text-[rgb(var(--muted))] mt-2">Cargando…</p>}

        {editIsin && (
          <NuevoPaso
            isin={editIsin}
            nombre={nombrePorIsin(editIsin)}
            onGuardar={async (payload) => {
              await crearPaso(payload);
              setEditIsin(null);
              await cargar();
            }}
            onCancelar={() => setEditIsin(null)}
          />
        )}
      </section>

      {/* Próximos pasos (solo activos) */}
      <section>
        <h3 className="text-lg font-semibold mb-3">Próximos pasos ({activos.length})</h3>
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-[rgb(var(--muted))] border-b border-[rgb(var(--border))]">
              <tr className="text-left">
                <th className="py-2 px-3">Valor</th>
                <th className="px-3">Decisión</th>
                <th className="px-3">Prioridad</th>
                <th className="px-3 text-right">Capital</th>
                <th className="px-3">Estado</th>
                <th className="px-3"></th>
              </tr>
            </thead>
            <tbody>
              {activos.map(renderFila)}
              {pasos && activos.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-4 px-3 text-center text-[rgb(var(--muted))]">
                    Sin pasos activos. Añade uno desde la tabla de arriba.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {historial.length > 0 && (
          <details className="mt-4 rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))]">
            <summary className="cursor-pointer select-none px-3 py-2 text-sm text-[rgb(var(--muted))]">
              Historial · auditoría ({historial.length}) — pasos completados o reemplazados
            </summary>
            <div className="overflow-x-auto border-t border-[rgb(var(--border))]">
              <table className="w-full text-sm opacity-70">
                <tbody>{historial.map(renderFila)}</tbody>
              </table>
            </div>
          </details>
        )}
      </section>
    </div>
  );
}

type GuardarPayload = {
  isin: string;
  decision: DecisionPlan;
  prioridad: PrioridadPlan;
  capital_objetivo_eur: string | null;
  razon: string | null;
  friccion_severidad?: string | null;
  friccion_motivo?: string | null;
};

const DECISIONES_PELIGROSAS: DecisionPlan[] = ['VENDER', 'RECORTAR'];

function NuevoPaso({
  isin,
  nombre,
  onGuardar,
  onCancelar,
}: {
  isin: string;
  nombre: string;
  onGuardar: (payload: GuardarPayload) => Promise<void>;
  onCancelar: () => void;
}) {
  const [decision, setDecision] = useState<DecisionPlan>('COMPRAR');
  const [prioridad, setPrioridad] = useState<PrioridadPlan>('MEDIA');
  const [capital, setCapital] = useState('');
  const [razon, setRazon] = useState('');
  const [friccion, setFriccion] = useState<FriccionResultado | null>(null);
  const [evaluando, setEvaluando] = useState(false);
  const [audit, setAudit] = useState<Auditoria | null>(null);
  const [auditando, setAuditando] = useState(false);

  // Audita la operación según la decisión (panel informativo; la fricción sigue
  // siendo el gate al guardar las decisiones peligrosas).
  useEffect(() => {
    const fn = (decision === 'VENDER' || decision === 'RECORTAR') ? auditarVenta
      : (decision === 'COMPRAR' || decision === 'REFORZAR') ? auditarCompra
        : null;
    if (!fn) { setAudit(null); return; }
    let vigente = true;
    setAudit(null); setAuditando(true);
    fn(isin)
      .then((a) => { if (vigente) setAudit(a); })
      .catch(() => {})
      .finally(() => { if (vigente) setAuditando(false); });
    return () => { vigente = false; };
  }, [decision, isin]);

  const base = (): GuardarPayload => ({
    isin, decision, prioridad,
    capital_objetivo_eur: capital.trim() ? capital.trim() : null,
    razon: razon.trim() ? razon.trim() : null,
  });

  const onGuardarClick = async () => {
    // Fricción solo en decisiones peligrosas: avisa antes de dejar pasar.
    if (DECISIONES_PELIGROSAS.includes(decision)) {
      setEvaluando(true);
      try {
        const f = await evaluarFriccion(isin, decision);
        if (f) { setFriccion(f); return; }   // abre el diálogo; no guarda aún
      } finally {
        setEvaluando(false);
      }
    }
    await onGuardar(base());
  };

  return (
    <div className="mt-3 rounded-lg border border-brand-300 dark:border-brand-700 bg-brand-50/40 dark:bg-brand-900/10 p-4">
      <div className="text-sm font-medium mb-3">Nuevo paso para {nombre}</div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={decision}
          onChange={(e) => setDecision(e.target.value as DecisionPlan)}
          className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
        >
          {DECISIONES.map((d) => (
            <option key={d} value={d}>{DECISION_LABEL[d]}</option>
          ))}
        </select>
        <select
          value={prioridad}
          onChange={(e) => setPrioridad(e.target.value as PrioridadPlan)}
          className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
        >
          {PRIORIDADES.map((p) => (
            <option key={p} value={p}>{PRIORIDAD_LABEL[p]}</option>
          ))}
        </select>
        <input
          value={capital}
          onChange={(e) => setCapital(e.target.value)}
          placeholder="Capital € (opc.)"
          inputMode="decimal"
          className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-36"
        />
        <input
          value={razon}
          onChange={(e) => setRazon(e.target.value)}
          placeholder="Razón (opc.)"
          className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] flex-1 min-w-[180px]"
        />
        <button
          onClick={onGuardarClick}
          disabled={evaluando}
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {evaluando ? 'Comprobando…' : 'Guardar'}
        </button>
        <button
          onClick={onCancelar}
          className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))]"
        >
          Cancelar
        </button>
      </div>

      {(auditando || audit) && (
        <div className="mt-3">
          {auditando && !audit
            ? <p className="text-xs text-[rgb(var(--muted))] animate-pulse">auditando la venta…</p>
            : audit && <AuditoriaVista a={audit} />}
        </div>
      )}

      {friccion && (
        <FriccionDialog
          friccion={friccion}
          onReconsiderar={() => setFriccion(null)}
          onProceder={async (motivo) => {
            await onGuardar({
              ...base(),
              friccion_severidad: friccion.severidad,
              friccion_motivo: motivo.trim() ? motivo.trim() : null,
            });
          }}
        />
      )}
    </div>
  );
}

// Diálogo de fricción: avisa, rebate 2 veces (datos → tú), te deja. Nunca bloquea.
function FriccionDialog({
  friccion,
  onReconsiderar,
  onProceder,
}: {
  friccion: FriccionResultado;
  onReconsiderar: () => void;
  onProceder: (motivo: string) => Promise<void>;
}) {
  const [nivel, setNivel] = useState<1 | 2>(1);
  const [motivo, setMotivo] = useState('');
  const alta = friccion.severidad === 'ALTA';
  // Clases literales (Tailwind JIT no procesa interpolación).
  const caja = alta
    ? 'border-rose-200 dark:border-rose-800 bg-rose-50 dark:bg-rose-900/20'
    : 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-xl border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-5 shadow-xl">
        <div className="flex items-center gap-2 mb-3">
          <span className={`text-lg ${alta ? 'text-rose-500' : 'text-amber-500'}`}>⚠️</span>
          <h3 className="text-base font-semibold">{friccion.titulo}</h3>
        </div>
        {friccion.etiquetas.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {friccion.etiquetas.map((e) => (
              <span key={e} className="text-[11px] px-1.5 py-0.5 rounded bg-[rgb(var(--border))]/50 text-[rgb(var(--muted))]">
                {e}
              </span>
            ))}
          </div>
        )}

        <div className={`rounded-lg border ${caja} p-3 text-sm`}>
          {friccion.rebate1}
        </div>

        {nivel === 2 && (
          <>
            <div className={`mt-2 rounded-lg border ${caja} p-3 text-sm`}>
              {friccion.rebate2}
            </div>
            <input
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              placeholder="¿Por qué aun así? (se guarda con la decisión)"
              className="mt-3 w-full px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            />
          </>
        )}

        <div className="mt-4 flex items-center justify-between gap-2">
          <button
            onClick={onReconsiderar}
            className="px-4 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700"
          >
            Reconsiderar
          </button>
          {nivel === 1 ? (
            <button
              onClick={() => setNivel(2)}
              className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]"
            >
              Aun así quiero seguir →
            </button>
          ) : (
            <button
              onClick={() => onProceder(motivo)}
              className="px-3 py-1.5 text-sm rounded border border-rose-300 text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-900/20"
            >
              Crear el paso de todos modos
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
