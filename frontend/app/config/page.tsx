'use client';

import { useCallback, useEffect, useState } from 'react';
import { fetchConfig, fmtEUR, guardarConfig } from '@/lib/api';
import { parseNumEs } from '@/lib/num';
import { notificarDatosActualizados } from '@/lib/refetch';
import type { ConfigCartera } from '@/lib/types';

const BROKER_LABEL: Record<string, string> = {
  degiro: 'DEGIRO', ibkr: 'Interactive Brokers', tr: 'Trade Republic',
  trading212: 'Trading 212', ing: 'ING Broker Naranja', myinvestor: 'MyInvestor',
};

export default function ConfigPage() {
  const [data, setData] = useState<ConfigCartera | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nombre, setNombre] = useState('');
  const [objetivo, setObjetivo] = useState('');
  const [aportacion, setAportacion] = useState('');
  const [guardando, setGuardando] = useState(false);
  const [ok, setOk] = useState(false);

  const cargar = useCallback(() => {
    fetchConfig()
      .then((d) => { setData(d); setNombre(d.nombre_cartera); setObjetivo(d.objetivo_if_eur); setAportacion(d.aportacion_mensual_eur); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => { cargar(); }, [cargar]);

  async function guardar() {
    setGuardando(true); setOk(false); setError(null);
    try {
      // parseNumEs: "300.000" ya no se guarda como 300 € de objetivo IF (A10)
      const obj = parseNumEs(objetivo);
      const ap = parseNumEs(aportacion);
      const d = await guardarConfig({
        nombre_cartera: nombre.trim(),
        objetivo_if_eur: obj ?? undefined,
        aportacion_mensual_eur: ap ?? undefined,
      });
      setData(d); setNombre(d.nombre_cartera); setObjetivo(d.objetivo_if_eur); setAportacion(d.aportacion_mensual_eur);
      setOk(true);
      notificarDatosActualizados();   // el objetivo IF cambia el progreso del dashboard
      setTimeout(() => setOk(false), 3000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGuardando(false);
    }
  }

  if (error && !data) return <p className="text-sm text-rose-600">{error}</p>;
  if (!data) return <p className="text-sm text-[rgb(var(--muted))]">Cargando…</p>;

  const cambiado = nombre !== data.nombre_cartera || objetivo !== data.objetivo_if_eur
    || aportacion !== data.aportacion_mensual_eur;

  return (
    <div className="max-w-2xl space-y-6">
      <h2 className="text-2xl font-semibold tracking-tight">Configuración</h2>

      {/* Perfil / cartera */}
      <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 space-y-4">
        <h3 className="font-semibold">Cartera</h3>
        <Campo label="Email">
          <span className="text-sm text-[rgb(var(--muted))]">{data.email}</span>
        </Campo>
        <Campo label="Nombre de la cartera">
          <input
            value={nombre}
            onChange={(e) => setNombre(e.target.value)}
            className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-64"
          />
        </Campo>
        <Campo label="Objetivo IF (€)" hint="Capital para la Independencia Financiera; mueve el «Progreso IF» del dashboard.">
          <input
            value={objetivo}
            onChange={(e) => setObjetivo(e.target.value)}
            inputMode="decimal"
            className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-40 text-right font-mono"
          />
        </Campo>
        <Campo label="Aportación mensual (€)" hint="Aportación periódica prevista; alimenta la estimación de «años a IF» del dashboard. 0 = usar tus aportaciones reales del año.">
          <input
            value={aportacion}
            onChange={(e) => setAportacion(e.target.value)}
            inputMode="decimal"
            className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-40 text-right font-mono"
          />
        </Campo>
        <Campo label="Modo" hint="Definido por el entorno (CIMA_MODE). Owner = IA sin capar; SaaS = con disclaimers.">
          <span className={`text-xs px-2 py-0.5 rounded ${
            data.modo === 'owner'
              ? 'bg-brand-100 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300'
              : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
          }`}>{data.modo === 'owner' ? 'Owner' : 'SaaS'}</span>
        </Campo>
        <div className="flex items-center gap-3 pt-1">
          <button
            onClick={guardar}
            disabled={guardando || !cambiado}
            className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
          >
            {guardando ? 'Guardando…' : 'Guardar'}
          </button>
          {ok && <span className="text-sm text-emerald-600 dark:text-emerald-400">Guardado ✓</span>}
          {error && <span className="text-sm text-rose-600">{error}</span>}
        </div>
      </section>

      {/* Brokers */}
      <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
        <h3 className="font-semibold mb-3">Brokers</h3>
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-1.5 pr-2">Broker</th>
              <th className="pr-2 text-right">Saldo reportado</th>
              <th className="pr-2">Fecha saldo</th>
            </tr>
          </thead>
          <tbody>
            {data.brokers.map((b) => (
              <tr key={b.broker_tipo} className="border-t border-[rgb(var(--border))]/30">
                <td className="py-1.5 pr-2">{BROKER_LABEL[b.broker_tipo] ?? b.alias ?? b.broker_tipo}</td>
                <td className="pr-2 text-right font-mono">
                  {b.saldo_reportado_eur != null
                    ? fmtEUR(b.saldo_reportado_eur, { maximumFractionDigits: 2 })
                    : <span className="text-[rgb(var(--muted))]">—</span>}
                </td>
                <td className="pr-2 text-[rgb(var(--muted))]">{b.saldo_fecha ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        <p className="text-xs text-[rgb(var(--muted))] mt-3">
          El saldo reportado es el del último extracto importado (DEGIRO/IBKR). Trade Republic y los
          demás no reportan saldo final → su liquidez se calcula de los flujos.
        </p>
      </section>

      <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4">
        <p className="text-xs text-[rgb(var(--muted))]">
          Próximamente: tasa fiscal, integración con Cuádrate (declaración IRPF) y divisa de
          visualización.
        </p>
      </section>
    </div>
  );
}

function Campo({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="text-xs text-[rgb(var(--muted))] max-w-sm mt-0.5">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}
