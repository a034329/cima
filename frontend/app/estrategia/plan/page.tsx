'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  actualizarPaso,
  crearPaso,
  eliminarPaso,
  fetchPlanPasos,
  fetchPosicionesPlan,
  fmtEUR,
} from '@/lib/api';
import {
  DECISION_COLOR,
  DECISION_LABEL,
  DECISIONES,
  PRIORIDAD_LABEL,
  PRIORIDADES,
} from '@/lib/decisiones';
import type {
  DecisionPlan,
  EstadoPlan,
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

      {/* Cola de pasos */}
      <section>
        <h3 className="text-lg font-semibold mb-3">Pasos del plan ({pasos?.length ?? 0})</h3>
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
              {pasos?.map((s) => (
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
                      onChange={async (e) => {
                        await actualizarPaso(s.id, { estado: e.target.value });
                        await cargar();
                      }}
                      className="px-2 py-1 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
                    >
                      {ESTADOS.map((es) => (
                        <option key={es} value={es}>{ESTADO_LABEL[es]}</option>
                      ))}
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
              ))}
              {pasos && pasos.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-4 px-3 text-center text-[rgb(var(--muted))]">
                    Sin pasos todavía. Añade uno desde la tabla de arriba.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function NuevoPaso({
  isin,
  nombre,
  onGuardar,
  onCancelar,
}: {
  isin: string;
  nombre: string;
  onGuardar: (payload: {
    isin: string;
    decision: DecisionPlan;
    prioridad: PrioridadPlan;
    capital_objetivo_eur: string | null;
    razon: string | null;
  }) => Promise<void>;
  onCancelar: () => void;
}) {
  const [decision, setDecision] = useState<DecisionPlan>('COMPRAR');
  const [prioridad, setPrioridad] = useState<PrioridadPlan>('MEDIA');
  const [capital, setCapital] = useState('');
  const [razon, setRazon] = useState('');

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
          onClick={() =>
            onGuardar({
              isin,
              decision,
              prioridad,
              capital_objetivo_eur: capital.trim() ? capital.trim() : null,
              razon: razon.trim() ? razon.trim() : null,
            })
          }
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700"
        >
          Guardar
        </button>
        <button
          onClick={onCancelar}
          className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))]"
        >
          Cancelar
        </button>
      </div>
    </div>
  );
}
