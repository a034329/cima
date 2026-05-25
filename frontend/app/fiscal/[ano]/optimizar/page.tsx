'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  fetchOptimizador,
  fetchPerdidasPendientes,
  fijarPrecioManual,
  fmtEUR,
  fmtNum,
  setPerdidaPendiente,
} from '@/lib/api';
import type { OptimizadorFiscal, PerdidaPendienteManual } from '@/lib/types';

export default function OptimizarPage({ params }: { params: { ano: string } }) {
  const ejercicio = parseInt(params.ano, 10);
  const [data, setData] = useState<OptimizadorFiscal | null>(null);
  const [perdidas, setPerdidas] = useState<PerdidaPendienteManual[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const cargar = useCallback(async () => {
    setCargando(true);
    setError(null);
    try {
      const [opt, pp] = await Promise.all([fetchOptimizador(ejercicio), fetchPerdidasPendientes()]);
      setData(opt);
      setPerdidas(pp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCargando(false);
    }
  }, [ejercicio]);

  useEffect(() => {
    cargar();
  }, [cargar]);

  if (Number.isNaN(ejercicio)) {
    return <p className="text-sm text-rose-600">El optimizador requiere un ejercicio (no acumulado).</p>;
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-sm text-[rgb(var(--muted))]">
          Cierre de año {ejercicio}: realizado YTD + pérdidas latentes para compensar plusvalías
          (regla 2M). Precios automáticos (best-effort); corrige los que veas mal.
        </p>
        <button
          onClick={cargar}
          disabled={cargando}
          className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))] disabled:opacity-50"
        >
          {cargando ? 'Calculando…' : 'Recalcular'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}
      {cargando && !data && (
        <p className="text-sm text-[rgb(var(--muted))]">
          Obteniendo precios actuales (puede tardar unos segundos la primera vez)…
        </p>
      )}

      {data && <Contenido d={data} onPrecio={async (isin, p) => { await fijarPrecioManual(isin, p); await cargar(); }} />}

      {data && (
        <PerdidasPendientes
          perdidas={perdidas}
          onGuardar={async (anio, importe) => { await setPerdidaPendiente(anio, importe); await cargar(); }}
        />
      )}
    </div>
  );
}

function PerdidasPendientes({
  perdidas,
  onGuardar,
}: {
  perdidas: PerdidaPendienteManual[];
  onGuardar: (anio: number, importe: number | null) => Promise<void>;
}) {
  const [anio, setAnio] = useState('');
  const [importe, setImporte] = useState('');
  const anioActual = new Date().getFullYear();

  return (
    <section className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <h3 className="font-semibold mb-1">Pérdidas pendientes de años anteriores</h3>
      <p className="text-xs text-[rgb(var(--muted))] mb-3">
        Introdúcelas desde tus declaraciones previas (la app no puede saber qué compensaste).
        Si las pones, sustituyen a la estimación automática. Caducan a los 4 años.
      </p>
      {perdidas.length > 0 ? (
        <table className="w-full text-sm mb-3">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-1 pr-2">Año origen</th>
              <th className="pr-2 text-right">Pendiente</th>
              <th className="pr-2">Caduca</th>
              <th></th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {perdidas.map((p) => (
              <tr key={p.ejercicio_origen} className="border-t border-[rgb(var(--border))]/30">
                <td className="py-1 pr-2">{p.ejercicio_origen}</td>
                <td className="pr-2 text-right">{fmtEUR(p.importe_eur, { maximumFractionDigits: 2 })}</td>
                <td className="pr-2 text-[rgb(var(--muted))]">{p.expira}</td>
                <td className="text-right">
                  <button
                    onClick={() => onGuardar(p.ejercicio_origen, null)}
                    className="text-xs text-rose-600 dark:text-rose-400 hover:underline"
                  >
                    eliminar
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="text-xs text-[rgb(var(--muted))] mb-3">
          Sin pérdidas pendientes introducidas — se usa la estimación automática.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={anio}
          onChange={(e) => setAnio(e.target.value)}
          placeholder={`Año (ej. ${anioActual - 1})`}
          inputMode="numeric"
          className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-32"
        />
        <input
          value={importe}
          onChange={(e) => setImporte(e.target.value)}
          placeholder="Importe pendiente €"
          inputMode="decimal"
          className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-40"
        />
        <button
          onClick={async () => {
            const a = parseInt(anio, 10);
            const i = importe.trim() ? parseFloat(importe.replace(',', '.')) : null;
            if (!Number.isFinite(a)) return;
            await onGuardar(a, i);
            setAnio(''); setImporte('');
          }}
          className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700"
        >
          Guardar
        </button>
      </div>
    </section>
  );
}

function Contenido({
  d,
  onPrecio,
}: {
  d: OptimizadorFiscal;
  onPrecio: (isin: string, precio: number | null) => Promise<void>;
}) {
  const realizada = parseFloat(d.gp_realizada_ytd);
  const compensable = parseFloat(d.compensable_ahora);
  return (
    <>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Card label="G/P realizada YTD" value={fmtEUR(d.gp_realizada_ytd, { maximumFractionDigits: 0 })}
          tono={realizada >= 0 ? 'ok' : 'warn'} sub="patrimonial (acciones) ya realizada" />
        <Card label="Pérdida latente cosechable" value={fmtEUR(d.perdida_latente_cosechable, { maximumFractionDigits: 0 })}
          tono="warn" sub="latente sin bloqueo 2M" />
        <Card label="Compensable ahora" value={fmtEUR(d.compensable_ahora, { maximumFractionDigits: 0 })}
          tono={compensable > 0 ? 'ok' : 'muted'} sub="realizando esas pérdidas" />
        <Card label="Bolsas años anteriores" value={fmtEUR(d.bolsas_pendientes, { maximumFractionDigits: 0 })}
          tono="muted" sub="arrastre 4 años disponible (declaraciones previas)" />
        <Card label="Pérdida a arrastrar (este año)" value={fmtEUR(d.perdida_a_arrastrar_anio, { maximumFractionDigits: 0 })}
          tono={parseFloat(d.perdida_a_arrastrar_anio) > 0 ? 'warn' : 'muted'}
          sub="generada este ejercicio · compensa 4 años" />
        <Card label="Diferidas 2M (latentes)" value={fmtEUR(d.diferidas_2m, { maximumFractionDigits: 0 })}
          tono="muted" sub="afloran al vender el lote recomprado" />
        <Card label="RCM YTD" value={fmtEUR(d.rcm_ytd, { maximumFractionDigits: 0 })}
          tono="muted" sub="dividendos + intereses netos" />
      </div>

      {compensable > 0 && (
        <div className="rounded-lg border border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-900/20 p-3 text-sm text-emerald-800 dark:text-emerald-200">
          Tienes <strong>{fmtEUR(d.gp_realizada_ytd, { maximumFractionDigits: 0 })}</strong> de plusvalía
          realizada. Realizando pérdidas latentes podrías compensar hasta{' '}
          <strong>{fmtEUR(d.compensable_ahora, { maximumFractionDigits: 0 })}</strong> antes del 31-dic
          (sin contar el bloqueo de la regla 2 meses, ya señalado abajo).
        </div>
      )}
      {realizada < 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3 text-sm text-[rgb(var(--muted))]">
          Este año vas en <strong>pérdida realizada</strong> ({fmtEUR(d.gp_realizada_ytd, { maximumFractionDigits: 0 })}):
          no hay plusvalía que compensar. Esa pérdida se arrastra y compensa en los próximos 4 años.
          Realizar más pérdidas latentes solo aumentaría el arrastre, no genera ahorro inmediato.
        </div>
      )}

      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
        <h3 className="font-semibold mb-3">Posiciones — G/P latente</h3>
        <table className="w-full text-xs">
          <thead className="text-[rgb(var(--muted))]">
            <tr className="text-left border-b border-[rgb(var(--border))]">
              <th className="py-2 pr-2">Posición</th>
              <th className="pr-2 text-right">Cantidad</th>
              <th className="pr-2 text-right">PM real</th>
              <th className="pr-2 text-right">Precio actual</th>
              <th className="pr-2 text-right">G/P latente</th>
              <th className="pr-2">Flags</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {d.latentes.map((l) => (
              <tr key={l.isin} className="border-t border-[rgb(var(--border))]/30">
                <td className="py-1 pr-2 font-sans">{l.nombre}</td>
                <td className="pr-2 text-right">{fmtNum(l.cantidad)}</td>
                <td className="pr-2 text-right">{fmtEUR(l.pm_real_eur, { maximumFractionDigits: 2 })}</td>
                <td className="pr-2 text-right">
                  <PrecioInput isin={l.isin} valor={l.precio_actual_eur} manual={l.precio_manual} onPrecio={onPrecio} />
                </td>
                <td className={`pr-2 text-right ${
                  l.gp_latente_eur == null ? 'text-[rgb(var(--muted))]'
                    : l.es_perdida ? 'text-rose-700 dark:text-rose-400' : 'text-emerald-700 dark:text-emerald-400'
                }`}>
                  {l.gp_latente_eur == null ? '—' : fmtEUR(l.gp_latente_eur, { maximumFractionDigits: 2 })}
                </td>
                <td className="pr-2 font-sans">
                  {l.bloqueo_2m && <Badge t="2M" color="amber" title="Tienes una COMPRA reciente (<2 meses) de este valor. Si lo vendieras ahora a pérdida, esa pérdida NO sería deducible (regla 2M, Art. 33.5.f). No significa que lo hayas vendido." />}
                  {l.precio_manual && <Badge t="manual" color="slate" />}
                  {l.sin_precio && <Badge t="sin precio" color="rose" />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-[rgb(var(--muted))] mt-3">
          <strong>2M</strong>: tienes una compra reciente (&lt;2 meses) de ese valor; si lo vendieras
          ahora a pérdida, no sería deducible (no indica que lo hayas vendido). ·{' '}
          <strong>manual</strong>: precio que has fijado tú. · <strong>sin precio</strong>: el feed
          no lo resolvió, edítalo a mano.
        </p>
        {d.no_resueltos.length > 0 && (
          <p className="text-xs text-[rgb(var(--muted))] mt-1">
            {d.no_resueltos.length} posiciones sin precio automático — edítalas en la columna «Precio actual».
          </p>
        )}
      </div>
    </>
  );
}

function PrecioInput({
  isin,
  valor,
  manual,
  onPrecio,
}: {
  isin: string;
  valor: string | null;
  manual: boolean;
  onPrecio: (isin: string, precio: number | null) => Promise<void>;
}) {
  const [v, setV] = useState(valor ?? '');
  useEffect(() => { setV(valor ?? ''); }, [valor]);
  return (
    <input
      value={v}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => {
        const n = v.trim() ? parseFloat(v.replace(',', '.')) : null;
        const actual = valor != null ? parseFloat(valor) : null;
        if (n !== actual) onPrecio(isin, n);
      }}
      placeholder="—"
      inputMode="decimal"
      className={`w-24 text-right px-1.5 py-0.5 rounded border bg-[rgb(var(--bg))] ${
        manual ? 'border-brand-400' : 'border-[rgb(var(--border))]'
      }`}
    />
  );
}

function Badge({ t, color, title }: { t: string; color: 'amber' | 'slate' | 'rose'; title?: string }) {
  const c = {
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
    slate: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
    rose: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
  }[color];
  return <span title={title} className={`inline-block ml-1 px-1.5 py-0.5 rounded text-[10px] ${c}`}>{t}</span>;
}

function Card({ label, value, tono, sub }: { label: string; value: string; tono: 'ok' | 'warn' | 'muted'; sub?: string }) {
  const css = {
    ok: 'text-emerald-700 dark:text-emerald-400',
    warn: 'text-amber-700 dark:text-amber-400',
    muted: 'text-[rgb(var(--fg))]',
  }[tono];
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${css}`}>{value}</div>
      {sub && <div className="text-xs text-[rgb(var(--muted))] mt-1">{sub}</div>}
    </div>
  );
}
