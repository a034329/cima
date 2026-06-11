'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import {
  bootstrap,
  crearAportacion,
  crearOpcion,
  crearTransaccion,
  evaluarFriccion,
  fetchBrokersSoportados,
  fetchEstadoBrokers,
  importarExtracto,
  registrarFriccion,
} from '@/lib/api';


/** Importe en divisa local y FX coherentes para una operación manual.
 *  Antes se enviaba siempre importe_local = importe_eur con fx_rate = 1:
 *  para una compra en USD el "importe local" persistido estaba en EUR y el
 *  FX era falso (cantidad × precio_local ≠ importe_local) — auditoría Cima
 *  2026-06-11, F3. Para divisa EUR se mantiene la identidad. */
function importeLocalYFx(acc: { cantidad: string; precio_local: string; divisa_local: string; importe_eur: string }) {
  if ((acc.divisa_local || 'EUR') === 'EUR') {
    return { importe_local: acc.importe_eur, fx_rate: '1' };
  }
  const local = parseFloat(acc.cantidad) * parseFloat(acc.precio_local);
  const eur = parseFloat(acc.importe_eur);
  if (!Number.isFinite(local) || local <= 0 || !Number.isFinite(eur)) {
    return { importe_local: acc.importe_eur, fx_rate: '1' };
  }
  return { importe_local: String(local), fx_rate: String(eur / local) };
}
import type { BrokerEstado } from '@/lib/api';
import { FriccionDialog } from '@/components/FriccionDialog';
import { notificarDatosActualizados } from '@/lib/refetch';
import type { FriccionResultado, ImportResultado, TipoTransaccion } from '@/lib/types';

// Etiquetas humanas para los broker_tipo del backend. Si el backend devuelve
// uno desconocido, se muestra el slug en mayúsculas.
const BROKER_LABELS: Record<string, string> = {
  tr: 'Trade Republic',
  degiro: 'DEGIRO',
  ibkr: 'Interactive Brokers',
  trading212: 'Trading 212',
  ing: 'ING Broker Naranja',
  myinvestor: 'MyInvestor',
};

function labelBroker(slug: string): string {
  return BROKER_LABELS[slug] ?? slug.toUpperCase();
}

// Fecha ISO (YYYY-MM-DD) → dd/mm/yyyy, sin construir Date (evita desfase UTC).
function fmtFecha(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

interface Props {
  carteraVacia: boolean;
}

export function AccionesCartera({ carteraVacia }: Props) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ tipo: 'ok' | 'error'; mensaje: string } | null>(null);
  const [modal, setModal] = useState<'add' | 'import' | null>(null);

  function showOk(msg: string) {
    setToast({ tipo: 'ok', mensaje: msg });
    setTimeout(() => setToast(null), 4000);
  }
  function showErr(msg: string) {
    setToast({ tipo: 'error', mensaje: msg });
    setTimeout(() => setToast(null), 6000);
  }

  async function handleBootstrap() {
    setBusy(true);
    try {
      const r = await bootstrap();
      showOk(r.creado ? 'Cartera y brokers creados.' : 'Cartera ya existía.');
      router.refresh();
      notificarDatosActualizados();
    } catch (e) {
      showErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {carteraVacia && (
        <button
          onClick={handleBootstrap}
          disabled={busy}
          className="px-3 py-1.5 text-sm font-medium rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {busy ? 'Inicializando…' : 'Inicializar cartera'}
        </button>
      )}
      <button
        onClick={() => setModal('add')}
        disabled={busy}
        className="px-3 py-1.5 text-sm font-medium rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]"
      >
        + Operación
      </button>
      <button
        onClick={() => setModal('import')}
        disabled={busy}
        className="px-3 py-1.5 text-sm font-medium rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]"
      >
        Importar extracto
      </button>
      <BotonGenerarIRPF disabled={busy || carteraVacia} />

      {modal === 'add' && (
        <ModalAnadirTx
          onClose={() => setModal(null)}
          onSuccess={(msg) => {
            showOk(msg);
            setModal(null);
            router.refresh();
            notificarDatosActualizados();
          }}
          onError={showErr}
        />
      )}
      {modal === 'import' && (
        <ModalImportarExtracto
          onClose={() => {
            setModal(null);
            router.refresh();
            notificarDatosActualizados();
          }}
          onError={showErr}
        />
      )}

      {toast && (
        <div
          className={`fixed bottom-6 right-6 max-w-md p-3 rounded shadow-lg text-sm z-50 ${
            toast.tipo === 'ok'
              ? 'bg-emerald-600 text-white'
              : 'bg-rose-600 text-white'
          }`}
        >
          {toast.mensaje}
        </div>
      )}
    </div>
  );
}

// ── Modal: añadir transacción manual ──────────────────────────────────────

type ModoOp = 'accion' | 'dividendo' | 'opcion' | 'aportacion';

function ModalAnadirTx({
  onClose,
  onSuccess,
  onError,
}: {
  onClose: () => void;
  onSuccess: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const hoy = new Date().toISOString().slice(0, 10);
  const [modo, setModo] = useState<ModoOp>('accion');
  const [submitting, setSubmitting] = useState(false);
  // Fricción pendiente cuando una operación (vender o comprar fuera de plan)
  // dispara el aviso conductual. Si no es null, se muestra el diálogo y la
  // operación queda en espera hasta que el usuario decida.
  const [pendiente, setPendiente] = useState<{ decision: string; friccion: FriccionResultado } | null>(null);

  // Form acción/ETF (BUY/SELL).
  const [acc, setAcc] = useState({
    isin: '', ticker: '', nombre: '', fecha: hoy, tipo: 'BUY' as TipoTransaccion,
    cantidad: '', precio_local: '', divisa_local: 'EUR', importe_eur: '', gastos_eur: '0', notas: '',
  });
  // Posiciones existentes para el selector (autocompleta isin/nombre/divisa).
  // `cantidad` se guarda para mostrar "máx X" y ofrecer "vender todo" — caso
  // real Angel: vendió "1" acción de ACS pensando que era lote/proporción,
  // teniendo 50+; sin el aviso de máximo es fácil equivocarse.
  const [posicionesCartera, setPosicionesCartera] = useState<
    Array<{ isin: string; nombre: string; divisa_local: string; cantidad: string }>
  >([]);
  useEffect(() => {
    // El modal solo se monta cuando se abre → carga única al entrar.
    fetch(`${process.env.NEXT_PUBLIC_API_URL ?? ''}/api/posiciones`)
      .then((r) => r.ok ? r.json() : { posiciones: [] })
      .then((d) => {
        type Pos = { isin: string; nombre: string; divisa_cotizacion?: string | null; cantidad?: string };
        const lista = (d.posiciones || []) as Pos[];
        setPosicionesCartera(
          lista.filter((p) => parseFloat(p.cantidad ?? '0') > 0)
               .map((p) => ({ isin: p.isin, nombre: p.nombre,
                              divisa_local: p.divisa_cotizacion || 'EUR',
                              cantidad: p.cantidad ?? '0' })),
        );
      }).catch(() => setPosicionesCartera([]));
  }, []);
  // Form dividendo
  const [div, setDiv] = useState({
    isin: '', nombre: '', fecha: hoy, bruto: '', retencion: '0', retencion_pais: '', notas: '',
  });
  // Form opción
  const [opt, setOpt] = useState({
    fecha: hoy, subyacente: '', tipo_op: 'C', strike: '', vencimiento: '',
    accion: 'venta', cantidad: '1', importe_eur: '', gastos_eur: '0',
    expirada: false, ejercida: false,
  });
  // Form aportación
  const [ap, setAp] = useState({
    fecha: hoy, signo: 'aportacion', importe: '', descripcion: '',
  });

  function autoImporteAcc() {
    const c = parseFloat(acc.cantidad), p = parseFloat(acc.precio_local);
    if (!isNaN(c) && !isNaN(p) && acc.divisa_local === 'EUR' && !acc.importe_eur) {
      setAcc({ ...acc, importe_eur: (c * p).toFixed(2) });
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      if (modo === 'accion') {
        if (acc.isin.length !== 12) throw new Error('ISIN debe tener 12 caracteres');
        // Pilar psicológico: antes de aplicar, pregúntale al motor de fricción.
        // Si dispara, suspendemos el submit y mostramos el diálogo (rebatir/seguir).
        const decision = acc.tipo === 'SELL' ? 'VENDER' : 'COMPRAR';
        const f = await evaluarFriccion(acc.isin.toUpperCase(), decision).catch(() => null);
        if (f) {
          setPendiente({ decision, friccion: f });
          setSubmitting(false);
          return;
        }
        await crearTransaccion({
          isin: acc.isin.toUpperCase(), ticker: acc.ticker || undefined,
          nombre: acc.nombre || undefined, fecha: acc.fecha, tipo: acc.tipo,
          cantidad: acc.cantidad, precio_local: acc.precio_local,
          divisa_local: acc.divisa_local, ...importeLocalYFx(acc),
          importe_eur: acc.importe_eur, gastos_eur: acc.gastos_eur || '0',
          notas: acc.notas || undefined,
          confirmar_directo: true,             // se aplica al instante (FIFO + fiscal)
        });
        onSuccess(`${acc.tipo === 'SELL' ? 'Venta' : 'Compra'} aplicada (FIFO recalculado)`);
      } else if (modo === 'dividendo') {
        if (div.isin.length !== 12) throw new Error('ISIN debe tener 12 caracteres');
        await crearTransaccion({
          isin: div.isin.toUpperCase(), nombre: div.nombre || undefined,
          fecha: div.fecha, tipo: 'DIVIDEND', cantidad: '0', precio_local: '0',
          divisa_local: 'EUR', importe_local: div.bruto, fx_rate: '1',
          importe_eur: div.bruto, gastos_eur: '0',
          retencion_eur: div.retencion || '0',
          retencion_pais: div.retencion_pais || undefined,
          notas: div.notas || undefined,
        });
        onSuccess('Dividendo registrado');
      } else if (modo === 'opcion') {
        if (!opt.subyacente || !opt.strike || !opt.vencimiento || !opt.importe_eur) {
          throw new Error('Completa subyacente, strike, vencimiento e importe');
        }
        const r = await crearOpcion({
          fecha: opt.fecha, subyacente: opt.subyacente.toUpperCase(),
          tipo_op: opt.tipo_op, strike: opt.strike,
          vencimiento: opt.vencimiento.toUpperCase(), accion: opt.accion,
          cantidad: opt.cantidad, prima_unitaria: '0', importe_eur: opt.importe_eur,
          gastos_eur: opt.gastos_eur || '0', expirada: opt.expirada, ejercida: opt.ejercida,
        });
        onSuccess(r.insertadas ? `Opción registrada: ${r.simbolo}` : 'Ya existía (duplicada)');
      } else {
        // aportación
        const v = parseFloat(ap.importe);
        if (isNaN(v) || v <= 0) throw new Error('Importe inválido');
        const signed = ap.signo === 'retirada' ? -v : v;
        await crearAportacion({
          fecha: ap.fecha, importe_eur: String(signed),
          descripcion: ap.descripcion || undefined,
        });
        onSuccess(ap.signo === 'retirada' ? 'Retirada registrada' : 'Aportación registrada');
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const tabBtn = (m: ModoOp, label: string) => (
    <button
      type="button"
      onClick={() => setModo(m)}
      className={`px-3 py-1.5 text-sm rounded border ${
        modo === m
          ? 'bg-brand-600 text-white border-brand-600'
          : 'border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]'
      }`}
    >
      {label}
    </button>
  );

  return (
    <div
      className="fixed inset-0 z-40 bg-black/40 flex items-start justify-center pt-20 px-4"
      onClick={onClose}
    >
      <div
        className="bg-[rgb(var(--card))] rounded-lg shadow-xl w-full max-w-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <form onSubmit={submit}>
          <div className="px-5 py-4 border-b border-[rgb(var(--border))] flex items-center justify-between">
            <h3 className="font-semibold">Añadir operación manual</h3>
            <button type="button" onClick={onClose} className="text-[rgb(var(--muted))]">✕</button>
          </div>

          <div className="px-5 pt-4 flex gap-2 flex-wrap">
            {tabBtn('accion', 'Acción/ETF')}
            {tabBtn('opcion', 'Opción')}
            {tabBtn('dividendo', 'Dividendo')}
            {tabBtn('aportacion', 'Aportación')}
          </div>

          <div className="px-5 py-4 space-y-3 max-h-[65vh] overflow-y-auto">
            {modo === 'accion' && (
              <>
                {posicionesCartera.length > 0 && (
                  <label className="block">
                    <span className="block text-sm font-medium mb-1">Posición existente (opcional)</span>
                    <select
                      value={acc.isin}
                      onChange={(e) => {
                        const p = posicionesCartera.find((x) => x.isin === e.target.value);
                        if (p) setAcc({ ...acc, isin: p.isin, nombre: p.nombre, divisa_local: p.divisa_local });
                        else setAcc({ ...acc, isin: '', nombre: '', divisa_local: 'EUR' });
                      }}
                      className="w-full px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
                    >
                      <option value="">— Operación sobre valor nuevo —</option>
                      {posicionesCartera.map((p) => (
                        <option key={p.isin} value={p.isin}>
                          {p.nombre} ({p.isin}) · {p.divisa_local}
                        </option>
                      ))}
                    </select>
                    <span className="text-[11px] text-[rgb(var(--muted))]">
                      Al elegir, ISIN/nombre/divisa se autocompletan.
                    </span>
                  </label>
                )}
                <Campo label="ISIN (12 caracteres)" value={acc.isin}
                  onChange={(v) => setAcc({ ...acc, isin: v.toUpperCase() })}
                  placeholder="IE000U9J8HX9" required />
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Ticker" value={acc.ticker} onChange={(v) => setAcc({ ...acc, ticker: v })} placeholder="JEQP" />
                  <Campo label="Fecha" type="date" value={acc.fecha} onChange={(v) => setAcc({ ...acc, fecha: v })} required />
                </div>
                <Campo label="Nombre" value={acc.nombre} onChange={(v) => setAcc({ ...acc, nombre: v })} />
                <div className="grid grid-cols-2 gap-3">
                  <CampoSelect label="Tipo" value={acc.tipo}
                    onChange={(v) => setAcc({ ...acc, tipo: v as TipoTransaccion })}
                    opciones={[['BUY', 'Compra'], ['SELL', 'Venta'], ['INTEREST', 'Interés']]} required />
                  <Campo label="Divisa" value={acc.divisa_local} onChange={(v) => setAcc({ ...acc, divisa_local: v.toUpperCase() })} maxLength={3} />
                </div>
                {(() => {
                  // Si hay una posición existente seleccionada Y es venta, mostrar
                  // cuántas acciones tienes y un botón "vender todo". Caso real:
                  // Angel vendió "1" acción de ACS pensando que era proporción,
                  // teniendo decenas. Aviso explícito + atajo = bug imposible.
                  const sel = posicionesCartera.find((x) => x.isin === acc.isin);
                  const maxQty = sel ? parseFloat(sel.cantidad) : NaN;
                  const esVentaSobreCartera = acc.tipo === 'SELL' && sel != null;
                  const excede = esVentaSobreCartera && !isNaN(maxQty)
                    && parseFloat(acc.cantidad || '0') > maxQty;
                  return (
                    <>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <Campo label="Cantidad" type="number" step="any" value={acc.cantidad}
                            onChange={(v) => setAcc({ ...acc, cantidad: v })}
                            onBlur={autoImporteAcc} placeholder="10" required />
                          {esVentaSobreCartera && !isNaN(maxQty) && (
                            <div className="mt-1 flex items-center gap-2 text-[11px]">
                              <span className={excede ? 'text-rose-600 dark:text-rose-400 font-medium' : 'text-[rgb(var(--muted))]'}>
                                Tienes <strong>{maxQty}</strong> en cartera
                                {excede && ' · estás vendiendo más de lo que tienes'}
                              </span>
                              <button
                                type="button"
                                onClick={() => setAcc({ ...acc, cantidad: String(maxQty) })}
                                className="text-brand-600 dark:text-brand-400 hover:underline"
                              >
                                vender todo →
                              </button>
                            </div>
                          )}
                        </div>
                        <Campo label="Precio" type="number" step="any" value={acc.precio_local}
                          onChange={(v) => setAcc({ ...acc, precio_local: v })}
                          onBlur={autoImporteAcc} placeholder="20.50" required />
                      </div>
                    </>
                  );
                })()}
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Importe EUR" type="number" step="any" value={acc.importe_eur}
                    onChange={(v) => setAcc({ ...acc, importe_eur: v })} required />
                  <Campo label="Gastos EUR" type="number" step="any" value={acc.gastos_eur}
                    onChange={(v) => setAcc({ ...acc, gastos_eur: v })} />
                </div>
              </>
            )}

            {modo === 'dividendo' && (
              <>
                <Campo label="ISIN (12 caracteres)" value={div.isin}
                  onChange={(v) => setDiv({ ...div, isin: v.toUpperCase() })} placeholder="US5949181045" required />
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Nombre" value={div.nombre} onChange={(v) => setDiv({ ...div, nombre: v })} placeholder="Microsoft" />
                  <Campo label="Fecha" type="date" value={div.fecha} onChange={(v) => setDiv({ ...div, fecha: v })} required />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Bruto EUR" type="number" step="any" value={div.bruto}
                    onChange={(v) => setDiv({ ...div, bruto: v })} placeholder="50.00" required />
                  <Campo label="Retención EUR" type="number" step="any" value={div.retencion}
                    onChange={(v) => setDiv({ ...div, retencion: v })} />
                </div>
                <Campo label="País retención (ISO, ej. US/ES)" value={div.retencion_pais}
                  onChange={(v) => setDiv({ ...div, retencion_pais: v.toUpperCase().slice(0, 2) })} maxLength={2} />
                <p className="text-xs text-[rgb(var(--muted))]">
                  Bruto → casilla 0029. País ES = retención nacional; extranjero = base CDI 0588.
                </p>
              </>
            )}

            {modo === 'opcion' && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Subyacente (ticker)" value={opt.subyacente}
                    onChange={(v) => setOpt({ ...opt, subyacente: v.toUpperCase() })} placeholder="OWL" required />
                  <Campo label="Fecha" type="date" value={opt.fecha} onChange={(v) => setOpt({ ...opt, fecha: v })} required />
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <CampoSelect label="Tipo" value={opt.tipo_op}
                    onChange={(v) => setOpt({ ...opt, tipo_op: v })} opciones={[['C', 'CALL'], ['P', 'PUT']]} required />
                  <Campo label="Strike" value={opt.strike} onChange={(v) => setOpt({ ...opt, strike: v })} placeholder="15.5" required />
                  <Campo label="Vencim. (19JUN26)" value={opt.vencimiento}
                    onChange={(v) => setOpt({ ...opt, vencimiento: v.toUpperCase() })} placeholder="19JUN26" required />
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <CampoSelect label="Acción" value={opt.accion}
                    onChange={(v) => setOpt({ ...opt, accion: v })} opciones={[['venta', 'Venta (prima cobrada)'], ['compra', 'Compra (prima pagada)']]} required />
                  <Campo label="Contratos" type="number" step="any" value={opt.cantidad}
                    onChange={(v) => setOpt({ ...opt, cantidad: v })} required />
                  <Campo label="Prima total EUR" type="number" step="any" value={opt.importe_eur}
                    onChange={(v) => setOpt({ ...opt, importe_eur: v })} placeholder="42.65" required />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Campo label="Gastos EUR" type="number" step="any" value={opt.gastos_eur}
                    onChange={(v) => setOpt({ ...opt, gastos_eur: v })} />
                  <div className="flex items-end gap-4 text-sm">
                    <label className="flex items-center gap-1">
                      <input type="checkbox" checked={opt.expirada}
                        onChange={(e) => setOpt({ ...opt, expirada: e.target.checked, ejercida: false })} />
                      Expirada
                    </label>
                    <label className="flex items-center gap-1">
                      <input type="checkbox" checked={opt.ejercida}
                        onChange={(e) => setOpt({ ...opt, ejercida: e.target.checked, expirada: false })} />
                      Ejercida
                    </label>
                  </div>
                </div>
                <p className="text-xs text-[rgb(var(--muted))]">
                  Abierta → diferida. Cerrada/expirada → casilla 1626. Ejercida → prima al subyacente.
                </p>
              </>
            )}

            {modo === 'aportacion' && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <CampoSelect label="Tipo" value={ap.signo}
                    onChange={(v) => setAp({ ...ap, signo: v })}
                    opciones={[['aportacion', 'Aportación (entra)'], ['retirada', 'Retirada (sale)']]} required />
                  <Campo label="Fecha" type="date" value={ap.fecha}
                    onChange={(v) => setAp({ ...ap, fecha: v })} required />
                </div>
                <Campo label="Importe EUR" type="number" step="any" value={ap.importe}
                  onChange={(v) => setAp({ ...ap, importe: v })} placeholder="12000.00" required />
                <Campo label="Descripción (opcional)" value={ap.descripcion}
                  onChange={(v) => setAp({ ...ap, descripcion: v })} placeholder="Transferencia DEGIRO enero" />
                <p className="text-xs text-[rgb(var(--muted))]">
                  Dinero de tu bolsillo (transferencia desde tu banco). IBKR y TR se
                  detectan solos al importar; DEGIRO se registra aquí (no está en su CSV).
                </p>
              </>
            )}
          </div>

          <div className="px-5 py-3 border-t border-[rgb(var(--border))] flex justify-end gap-2">
            <button type="button" onClick={onClose}
              className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))]">
              Cancelar
            </button>
            <button type="submit" disabled={submitting}
              className="px-3 py-1.5 text-sm font-medium rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50">
              {submitting ? 'Guardando…' : 'Registrar'}
            </button>
          </div>
        </form>
      </div>

      {pendiente && (
        <FriccionDialog
          friccion={pendiente.friccion}
          etiquetaProceder={
            pendiente.decision === 'VENDER' ? 'Vender de todos modos' : 'Comprar de todos modos'
          }
          onReconsiderar={() => setPendiente(null)}
          onProceder={async (motivo) => {
            try {
              setSubmitting(true);
              // 1) Captura el override (qué se hizo aun con la fricción).
              await registrarFriccion(
                acc.isin.toUpperCase(), pendiente.decision,
                pendiente.friccion.severidad, motivo.trim() || null,
              );
              // 2) Ejecuta la operación al instante (FIFO + fiscal + plan).
              await crearTransaccion({
                isin: acc.isin.toUpperCase(), ticker: acc.ticker || undefined,
                nombre: acc.nombre || undefined, fecha: acc.fecha, tipo: acc.tipo,
                cantidad: acc.cantidad, precio_local: acc.precio_local,
                divisa_local: acc.divisa_local, ...importeLocalYFx(acc),
                importe_eur: acc.importe_eur, gastos_eur: acc.gastos_eur || '0',
                notas: acc.notas || undefined, confirmar_directo: true,
              });
              setPendiente(null);
              onSuccess(`${acc.tipo === 'SELL' ? 'Venta' : 'Compra'} aplicada tras rebatir la fricción`);
            } catch (e) {
              onError(e instanceof Error ? e.message : String(e));
            } finally {
              setSubmitting(false);
            }
          }}
        />
      )}
    </div>
  );
}

// ── Modal: importar extracto ──────────────────────────────────────────────

function ModalImportarExtracto({
  onClose,
  onError,
}: {
  onClose: () => void;
  onError: (msg: string) => void;
}) {
  const [brokers, setBrokers] = useState<string[]>([]);
  const [broker, setBroker] = useState<string>('');
  const [fichero, setFichero] = useState<File | null>(null);
  const [ficheroCuenta, setFicheroCuenta] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [resultado, setResultado] = useState<ImportResultado | null>(null);
  const [loadingBrokers, setLoadingBrokers] = useState(true);
  const [estado, setEstado] = useState<BrokerEstado[]>([]);
  // Ejercicio fiscal del extracto. Si se informa, el backend guarda el CSV
  // original para usarlo en Generar IRPF (Roadmap 1.9). El default es el año
  // anterior (campaña típica). Vacío = no guardar (modo BD-only).
  const ahora = new Date().getFullYear();
  const ejerciciosPosibles = Array.from({ length: 6 }, (_, i) => ahora - i);
  const [ejercicio, setEjercicio] = useState<string>(String(ahora - 1));

  // onError vía ref: la prop se recrea en cada render del padre (es una
  // función inline) y con `[onError]` como dependencia cada toast re-disparaba
  // este efecto → re-fetch + setBroker(bs[0]) machacando la selección del
  // usuario; con el backend caído, el catch→toast→render→refetch entraba en
  // bucle infinito de peticiones (auditoría Cima 2026-06-11, F1). El fetch
  // de brokers es de montaje: se ejecuta UNA vez.
  const onErrorRef = useRef(onError);
  useEffect(() => { onErrorRef.current = onError; });
  useEffect(() => {
    fetchBrokersSoportados()
      .then((bs) => {
        setBrokers(bs);
        setBroker(bs[0] ?? '');
      })
      .catch((e) => {
        onErrorRef.current(`No se pudieron cargar brokers: ${e instanceof Error ? e.message : e}`);
      })
      .finally(() => setLoadingBrokers(false));
    fetchEstadoBrokers()
      .then(setEstado)
      .catch(() => { /* el panel es informativo; si falla, no molestamos */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const estadoBroker = estado.find((e) => e.broker_tipo === broker);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!fichero) {
      onError('Selecciona el CSV principal');
      return;
    }
    setSubmitting(true);
    try {
      const ej = ejercicio ? Number(ejercicio) : null;
      const r = await importarExtracto(broker, fichero, ficheroCuenta, ej);
      setResultado(r);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const labelPrincipal =
    broker === 'degiro'
      ? 'Transacciones CSV'
      : broker === 'ibkr'
        ? 'Activity Statement CSV'
        : 'Fichero CSV';
  const hintPrincipal =
    broker === 'degiro'
      ? 'DeGiro_Transacciones_*.csv (vale el histórico multi-año entero).'
      : broker === 'ibkr'
        ? 'IBKR Activity Statement (CSV, Base Currency = EUR). Un solo fichero con trades, dividendos, retenciones, intereses y corporate actions.'
        : 'CSV exportado del broker.';

  return (
    <div
      className="fixed inset-0 z-40 bg-black/40 flex items-start justify-center pt-20 px-4"
      onClick={onClose}
    >
      <div
        className="bg-[rgb(var(--card))] rounded-lg shadow-xl w-full max-w-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-[rgb(var(--border))] flex items-center justify-between">
          <h3 className="font-semibold">
            {resultado ? 'Resultado de la importación' : 'Importar extracto de broker'}
          </h3>
          <button type="button" onClick={onClose} className="text-[rgb(var(--muted))]">
            ✕
          </button>
        </div>

        {!resultado ? (
          <form onSubmit={submit}>
            <div className="px-5 py-4 space-y-4">
              {loadingBrokers ? (
                <p className="text-sm text-[rgb(var(--muted))]">Cargando brokers…</p>
              ) : brokers.length === 0 ? (
                <p className="text-sm text-rose-600">
                  No hay brokers disponibles. Comprueba el backend.
                </p>
              ) : (
                <CampoSelect
                  label="Broker"
                  value={broker}
                  onChange={(v) => {
                    setBroker(v);
                    setFicheroCuenta(null);
                  }}
                  opciones={brokers.map((b) => [b, labelBroker(b)])}
                />
              )}

              {estadoBroker && (
                estadoBroker.ultima_fecha ? (
                  <div className="rounded-md border border-[rgb(var(--border))] bg-[rgb(var(--bg))] px-3 py-2 text-xs text-[rgb(var(--muted))]">
                    Último registro importado de <strong>{labelBroker(broker)}</strong>:{' '}
                    <span className="font-mono text-[rgb(var(--fg))]">{fmtFecha(estadoBroker.ultima_fecha)}</span>
                    {' '}({estadoBroker.num_registros} registros). Pide el extracto{' '}
                    <strong>desde el día siguiente</strong> para no solaparte; los duplicados
                    se descartan igualmente.
                  </div>
                ) : (
                  <div className="rounded-md border border-[rgb(var(--border))] bg-[rgb(var(--bg))] px-3 py-2 text-xs text-[rgb(var(--muted))]">
                    Aún no hay registros importados de <strong>{labelBroker(broker)}</strong>.
                    Sube el histórico completo.
                  </div>
                )
              )}

              <div>
                <label className="block text-xs font-medium text-[rgb(var(--muted))] mb-1">
                  {labelPrincipal}{' '}
                  <span className="text-rose-600">*</span>
                </label>
                <input
                  type="file"
                  accept=".csv"
                  onChange={(e) => setFichero(e.target.files?.[0] || null)}
                  className="block w-full text-sm"
                  required
                />
                <p className="text-xs text-[rgb(var(--muted))] mt-1">{hintPrincipal}</p>
              </div>

              {broker === 'degiro' && (
                <div>
                  <label className="block text-xs font-medium text-[rgb(var(--muted))] mb-1">
                    Cuenta CSV{' '}
                    <span className="text-[rgb(var(--muted))]">(opcional, recomendado)</span>
                  </label>
                  <input
                    type="file"
                    accept=".csv"
                    onChange={(e) => setFicheroCuenta(e.target.files?.[0] || null)}
                    className="block w-full text-sm"
                  />
                  <p className="text-xs text-[rgb(var(--muted))] mt-1">
                    <code className="font-mono">DeGiro_Cuenta_*.csv</code> — añade
                    dividendos consolidados con retención, tasas externas (UK Stamp Duty,
                    French FTT) por orden, y la información necesaria para detectar
                    spin-offs, rights y otros eventos.
                  </p>
                </div>
              )}

              <div>
                <label className="block text-xs font-medium text-[rgb(var(--muted))] mb-1">
                  Ejercicio fiscal del extracto{' '}
                  <span className="text-[rgb(var(--muted))]">(para Generar IRPF)</span>
                </label>
                <select
                  value={ejercicio}
                  onChange={(e) => setEjercicio(e.target.value)}
                  className="w-full px-2 py-1.5 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-sm"
                >
                  <option value="">— No guardar para IRPF —</option>
                  {ejerciciosPosibles.map((y) => (
                    <option key={y} value={y}>{y}</option>
                  ))}
                </select>
                <p className="text-xs text-[rgb(var(--muted))] mt-1">
                  Si lo indicas, Cima <strong>guarda el CSV original</strong> y lo usa al
                  generar la declaración con el motor de Cuádrate. Si lo dejas vacío,
                  el extracto se procesa a la BD pero el CSV no se conserva.
                </p>
              </div>

              <p className="text-xs text-[rgb(var(--muted))]">
                El re-import es idempotente (no duplica filas en BD). Subir de nuevo
                el mismo ejercicio+broker reemplaza el CSV guardado.
              </p>
            </div>

            <div className="px-5 py-3 border-t border-[rgb(var(--border))] flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))]"
              >
                Cancelar
              </button>
              <button
                type="submit"
                disabled={submitting || !fichero || !broker}
                className="px-3 py-1.5 text-sm font-medium rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
              >
                {submitting ? 'Procesando…' : 'Importar'}
              </button>
            </div>
          </form>
        ) : (
          <ResultadoPanel resultado={resultado} onClose={onClose} />
        )}
      </div>
    </div>
  );
}

function ResultadoPanel({
  resultado,
  onClose,
}: {
  resultado: ImportResultado;
  onClose: () => void;
}) {
  const total =
    resultado.insertadas +
    resultado.reconciliadas +
    resultado.deduplicadas;
  const fifoAvisos = resultado.avisos.filter((a) => a.startsWith('[FIFO]'));
  const otrosAvisos = resultado.avisos.filter((a) => !a.startsWith('[FIFO]'));

  return (
    <div className="flex flex-col max-h-[80vh]">
      <div className="px-5 py-4 space-y-4 overflow-y-auto">
        <div className="text-sm text-[rgb(var(--muted))]">
          Broker: <span className="font-medium text-[rgb(var(--fg))]">
            {labelBroker(resultado.broker)}
          </span>
          {' · '}
          {total} {total === 1 ? 'fila procesada' : 'filas procesadas'}
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
          <Metrica label="Insertadas" valor={resultado.insertadas} tono="ok" />
          <Metrica label="Reconciliadas" valor={resultado.reconciliadas} tono="ok" />
          <Metrica
            label="Duplicadas"
            valor={resultado.deduplicadas}
            tono="muted"
            tooltip="Ya existían (mismo external_id). No se duplicaron."
          />
          <Metrica
            label="Conflictos"
            valor={resultado.conflictos}
            tono={resultado.conflictos > 0 ? 'warn' : 'muted'}
          />
          <Metrica
            label="Huérfanas"
            valor={resultado.huerfanas_manuales}
            tono={resultado.huerfanas_manuales > 0 ? 'warn' : 'muted'}
            tooltip="Manuales registradas hace >30 días sin aparecer en el extracto."
          />
        </div>

        {(resultado.opciones_insertadas > 0 || resultado.opciones_deduplicadas > 0) && (
          <div className="grid grid-cols-2 gap-2">
            <Metrica
              label="Opciones insertadas"
              valor={resultado.opciones_insertadas}
              tono="ok"
              tooltip="Operaciones de opciones añadidas. Ver pestaña Opciones."
            />
            <Metrica
              label="Opciones duplicadas"
              valor={resultado.opciones_deduplicadas}
              tono="muted"
            />
          </div>
        )}

        {fifoAvisos.length > 0 && (
          <div className="rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-3">
            <p className="text-xs font-semibold text-amber-700 dark:text-amber-300 mb-1">
              FIFO — rebuild cross-broker ({fifoAvisos.length})
            </p>
            <ul className="text-xs space-y-1 text-amber-900 dark:text-amber-200 max-h-48 overflow-y-auto pr-1">
              {fifoAvisos.map((a, i) => (
                <li key={i} className="font-mono">
                  {a.replace('[FIFO] ', '')}
                </li>
              ))}
            </ul>
            <p className="text-xs text-amber-700 dark:text-amber-400 mt-2 italic">
              Probablemente falta importar otro broker con compras anteriores. Al
              subirlo, el rebuild las intercalará y los avisos desaparecerán.
            </p>
          </div>
        )}

        {otrosAvisos.length > 0 && (
          <div className="rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-3">
            <p className="text-xs font-semibold mb-1">Avisos</p>
            <ul className="text-xs space-y-1">
              {otrosAvisos.map((a, i) => (
                <li key={i} className="font-mono">{a}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
      <div className="px-5 py-3 border-t border-[rgb(var(--border))] flex justify-end">
        <button
          onClick={onClose}
          className="px-3 py-1.5 text-sm font-medium rounded bg-brand-600 text-white hover:bg-brand-700"
        >
          Cerrar
        </button>
      </div>
    </div>
  );
}

function Metrica({
  label,
  valor,
  tono,
  tooltip,
}: {
  label: string;
  valor: number;
  tono: 'ok' | 'warn' | 'muted';
  tooltip?: string;
}) {
  const tonoCss: Record<typeof tono, string> = {
    ok: 'text-emerald-700 dark:text-emerald-400',
    warn: 'text-amber-700 dark:text-amber-400',
    muted: 'text-[rgb(var(--muted))]',
  };
  return (
    <div
      className="rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] px-2 py-2 text-center"
      title={tooltip}
    >
      <div className={`text-xl font-semibold ${tonoCss[tono]}`}>{valor}</div>
      <div className="text-[10px] uppercase tracking-wide text-[rgb(var(--muted))]">
        {label}
      </div>
    </div>
  );
}

// ── Inputs reutilizables ──────────────────────────────────────────────────

type CampoProps = {
  label: string;
  value: string;
  onChange: (v: string) => void;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'value'>;

function Campo({
  label,
  value,
  onChange,
  ...rest
}: CampoProps) {
  return (
    <div>
      <label className="block text-xs font-medium text-[rgb(var(--muted))] mb-1">
        {label}
      </label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-2 py-1.5 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-sm"
        {...rest}
      />
    </div>
  );
}

function CampoSelect({
  label,
  value,
  onChange,
  opciones,
  required,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  opciones: [string, string][];
  required?: boolean;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-[rgb(var(--muted))] mb-1">
        {label}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        className="w-full px-2 py-1.5 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-sm"
      >
        {opciones.map(([k, v]) => (
          <option key={k} value={k}>
            {v}
          </option>
        ))}
      </select>
    </div>
  );
}

// ── Generar declaración IRPF (Roadmap 1.9) ─────────────────────────────────
//
// Botón con selector de ejercicio. Al confirmar, llama al endpoint que invoca
// el `generar_irpf.py` de Cuádrate sobre los CSVs guardados por el usuario y
// devuelve un ZIP con XLSX maestro + 4 informes (corporativas/dividendos/
// opciones/fx) + sidecars JSON. Paridad total con Cuádrate.
//
// Antes de generar muestra los extractos guardados del ejercicio elegido —
// si no hay ninguno, deshabilita el botón y avisa.

type ExtractoGuardado = {
  id: string;
  ejercicio: number;
  kind: string;
  filename_original: string;
  size_bytes: number;
  uploaded_at: string;
};

const KIND_LABEL: Record<string, string> = {
  degiro_transacciones: 'DEGIRO Transacciones',
  degiro_cuenta:        'DEGIRO Cuenta',
  ibkr:                 'IBKR Activity Statement',
  tr:                   'Trade Republic',
};

function BotonGenerarIRPF({ disabled }: { disabled: boolean }) {
  const [abierto, setAbierto] = useState(false);
  const [descargando, setDescargando] = useState(false);
  const ahora = new Date().getFullYear();
  // Campaña típica: ejercicio anterior + 4 años hacia atrás.
  const [ejercicio, setEjercicio] = useState(String(ahora - 1));
  const [extractos, setExtractos] = useState<ExtractoGuardado[] | null>(null);
  const [cargandoExtractos, setCargandoExtractos] = useState(false);
  const ejercicios = Array.from({ length: 5 }, (_, i) => ahora - i);

  // Al abrir o cambiar de ejercicio, refrescamos qué extractos hay guardados
  // para informar al usuario si puede generar o le falta subir CSVs.
  useEffect(() => {
    if (!abierto) return;
    let vigente = true;
    setCargandoExtractos(true);
    fetch(`/api/import/extractos?ejercicio=${ejercicio}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => { if (vigente) setExtractos(data); })
      .catch(() => { if (vigente) setExtractos([]); })
      .finally(() => { if (vigente) setCargandoExtractos(false); });
    return () => { vigente = false; };
  }, [abierto, ejercicio]);

  async function descargar() {
    setDescargando(true);
    try {
      const url = `/api/cuadrate/irpf/${ejercicio}.zip`;
      const r = await fetch(url);
      if (!r.ok) {
        const txt = await r.text();
        let mensaje = txt;
        try { mensaje = JSON.parse(txt).detail ?? txt; } catch { /* no JSON */ }
        throw new Error(`HTTP ${r.status}: ${mensaje.slice(0, 400)}`);
      }
      const blob = await r.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = `cartera_irpf_${ejercicio}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(blobUrl);
      setAbierto(false);
    } catch (e) {
      alert(`No se pudo generar la declaración: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDescargando(false);
    }
  }

  const sinExtractos = extractos != null && extractos.length === 0;

  return (
    <div className="relative">
      <button
        onClick={() => setAbierto((v) => !v)}
        disabled={disabled || descargando}
        className="px-3 py-1.5 text-sm font-medium rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))] disabled:opacity-50"
        title="Genera la declaración IRPF completa (XLSX + informes) usando los CSVs guardados"
      >
        {descargando ? 'Generando…' : 'Generar IRPF'}
      </button>

      {abierto && !descargando && (
        <div className="absolute right-0 mt-1 z-10 rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] shadow-lg p-3 space-y-2 min-w-[300px]">
          <div className="text-xs font-semibold uppercase tracking-wider text-[rgb(var(--muted))]">
            Ejercicio fiscal
          </div>
          <select
            value={ejercicio}
            onChange={(e) => setEjercicio(e.target.value)}
            className="w-full px-2 py-1.5 rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-sm"
          >
            {ejercicios.map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>

          <div className="text-xs">
            {cargandoExtractos && (
              <span className="text-[rgb(var(--muted))]">Verificando extractos…</span>
            )}
            {extractos != null && extractos.length > 0 && (
              <div>
                <div className="text-[rgb(var(--muted))] mb-0.5">
                  Extractos guardados para {ejercicio}:
                </div>
                <ul className="text-[11px] space-y-0.5">
                  {extractos.map((e) => (
                    <li key={e.id} className="flex items-center gap-1">
                      <span className="text-emerald-600 dark:text-emerald-400">●</span>
                      <span>{KIND_LABEL[e.kind] ?? e.kind}</span>
                      <span className="text-[rgb(var(--muted))]">
                        · {(e.size_bytes / 1024).toFixed(1)} KB
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {sinExtractos && (
              <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-700 p-2">
                <span className="font-semibold text-amber-700 dark:text-amber-300">
                  Sin extractos guardados para {ejercicio}.
                </span>
                <p className="text-[11px] mt-0.5">
                  Sube el CSV del broker en <strong>Importar extracto</strong> indicando el ejercicio.
                </p>
              </div>
            )}
          </div>

          <div className="flex gap-2">
            <button
              onClick={descargar}
              disabled={sinExtractos || cargandoExtractos}
              className="flex-1 px-2 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
            >
              Descargar declaración (ZIP)
            </button>
            <button
              onClick={() => setAbierto(false)}
              className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]"
            >
              Cancelar
            </button>
          </div>
          <p className="text-[10px] text-[rgb(var(--muted))] leading-snug">
            XLSX maestro editable + <strong>PDF fiscal</strong> con guía RentaWEB +
            informes (corporativas, dividendos, opciones, FX) + sidecars JSON.
            Paridad total con Cuádrate.
          </p>
        </div>
      )}
    </div>
  );
}
