import type {
  BillsResumen,
  CarteraResumen,
  ComplejosResumen,
  DividendosResumen,
  EstadoTransaccion,
  FiscalResumen,
  ForexResumen,
  ImportResultado,
  InteresesResumen,
  OpcionesResumen,
  PosicionesResumen,
  ResumenFiscal,
  TransaccionIn,
  TransaccionOut,
} from './types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

/** Error de API con el `status` HTTP accesible para que la UI decida tono/acción. */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Construye un error legible a partir de una respuesta no-OK.
 *  Prioriza el campo `detail` de FastAPI (string o lista de errores de validación). */
async function apiError(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '');
  let msg = '';
  if (text) {
    try {
      const j = JSON.parse(text);
      const d = j?.detail;
      if (typeof d === 'string') {
        msg = d;
      } else if (Array.isArray(d)) {
        // Errores de validación de pydantic: [{loc, msg, ...}]
        msg = d.map((e) => e?.msg ?? JSON.stringify(e)).join('; ');
      } else if (d != null) {
        msg = typeof d === 'object' ? JSON.stringify(d) : String(d);
      } else if (typeof j?.message === 'string') {
        msg = j.message;
      }
    } catch {
      msg = text; // no era JSON: usa el cuerpo tal cual
    }
  }
  if (!msg) msg = `No se pudo completar la operación (error ${res.status}).`;
  return new ApiError(msg, res.status);
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    ...init,
  });
  if (!res.ok) {
    throw await apiError(res);
  }
  return (await res.json()) as T;
}

export async function fetchCartera(): Promise<CarteraResumen> {
  return fetchJson<CarteraResumen>('/api/cartera');
}

export async function fetchDashboard(): Promise<import('./types').DashboardData> {
  return fetchJson('/api/dashboard');
}

export async function fetchTransacciones(opts: {
  estado?: EstadoTransaccion;
  isin?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<TransaccionOut[]> {
  const params = new URLSearchParams();
  if (opts.estado) params.set('estado', opts.estado);
  if (opts.isin) params.set('isin', opts.isin);
  if (opts.limit !== undefined) params.set('limit', String(opts.limit));
  if (opts.offset !== undefined) params.set('offset', String(opts.offset));
  const q = params.toString();
  return fetchJson<TransaccionOut[]>(`/api/transacciones${q ? `?${q}` : ''}`);
}

export async function crearTransaccion(payload: TransaccionIn): Promise<TransaccionOut> {
  return fetchJson<TransaccionOut>('/api/transacciones', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface OpcionIn {
  fecha: string;
  subyacente: string;
  tipo_op: string;          // 'C' | 'P'
  strike: string;
  vencimiento: string;      // '19JUN26'
  accion: string;           // 'compra' | 'venta'
  cantidad: string;
  prima_unitaria: string;
  importe_eur: string;
  gastos_eur?: string;
  expirada?: boolean;
  ejercida?: boolean;
  subyacente_isin?: string;
}

export async function crearOpcion(payload: OpcionIn): Promise<{
  insertadas: number;
  deduplicadas: number;
  simbolo: string;
}> {
  return fetchJson('/api/opciones', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface AportacionIn {
  fecha: string;
  importe_eur: string;       // + aportación / − retirada
  descripcion?: string;
  broker_id?: string;
}

export async function crearAportacion(payload: AportacionIn): Promise<unknown> {
  return fetchJson('/api/aportaciones', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function descartarTransaccion(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/transacciones/${id}`, {
    method: 'DELETE',
  });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

export async function importarExtracto(
  brokerTipo: string,
  fichero: File,
  ficheroCuenta?: File | null,
): Promise<ImportResultado> {
  const form = new FormData();
  form.set('broker_tipo', brokerTipo);
  form.set('fichero', fichero);
  if (ficheroCuenta) {
    form.set('fichero_cuenta', ficheroCuenta);
  }
  const res = await fetch(`${API_BASE}/api/import`, {
    method: 'POST',
    body: form,
  });
  if (!res.ok) {
    throw await apiError(res);
  }
  return (await res.json()) as ImportResultado;
}

export async function fetchBrokersSoportados(): Promise<string[]> {
  const r = await fetchJson<{ brokers: string[] }>('/api/import/brokers');
  return r.brokers;
}

export interface BrokerEstado {
  broker_tipo: string;
  ultima_fecha: string | null;
  num_registros: number;
  saldo_reportado_eur: string | null;
  saldo_fecha: string | null;
}

export async function fetchEstadoBrokers(): Promise<BrokerEstado[]> {
  return fetchJson<BrokerEstado[]>('/api/import/estado');
}

export async function fetchFiscal(ejercicio: number): Promise<FiscalResumen> {
  return fetchJson<FiscalResumen>(`/api/fiscal/${ejercicio}`);
}

export async function fetchFiscalAcumulado(): Promise<FiscalResumen> {
  return fetchJson<FiscalResumen>('/api/fiscal/acumulado');
}

export async function fetchResumenFiscal(ejercicio: number): Promise<ResumenFiscal> {
  return fetchJson<ResumenFiscal>(`/api/fiscal/resumen/${ejercicio}`);
}

export async function fetchResumenFiscalAcumulado(): Promise<ResumenFiscal> {
  return fetchJson<ResumenFiscal>('/api/fiscal/resumen/acumulado');
}

export async function fetchOptimizador(
  ejercicio: number,
): Promise<import('./types').OptimizadorFiscal> {
  return fetchJson(`/api/fiscal/optimizador/${ejercicio}`);
}

export async function fetchSerieDividendos(): Promise<
  import('./types').SerieDividendos
> {
  return fetchJson('/api/dividendos/serie');
}

export async function fetchDiversificacionDividendos(
  anio?: number,
): Promise<import('./types').DiversificacionDividendos> {
  const q = anio ? `?anio=${anio}` : '';
  return fetchJson(`/api/dividendos/diversificacion${q}`);
}

export async function fetchRotacion(
  ejercicio: number,
): Promise<import('./types').RotacionFiscal> {
  return fetchJson(`/api/fiscal/rotacion/${ejercicio}`);
}

export async function fijarPrecioManual(
  isin: string,
  precioEur: number | null,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/fiscal/optimizador/precio`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ isin, precio_eur: precioEur }),
  });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

export async function fetchPerdidasPendientes(): Promise<
  import('./types').PerdidaPendienteManual[]
> {
  return fetchJson('/api/fiscal/perdidas-pendientes');
}

export async function setPerdidaPendiente(
  ejercicioOrigen: number,
  importeEur: number | null,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/fiscal/perdidas-pendientes`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ejercicio_origen: ejercicioOrigen, importe_eur: importeEur }),
  });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

export async function fetchOpciones(ejercicio: number): Promise<OpcionesResumen> {
  return fetchJson<OpcionesResumen>(`/api/opciones/${ejercicio}`);
}

export async function fetchOpcionesAcumulado(): Promise<OpcionesResumen> {
  return fetchJson<OpcionesResumen>('/api/opciones/acumulado');
}

export async function fetchDividendos(ejercicio: number): Promise<DividendosResumen> {
  return fetchJson<DividendosResumen>(`/api/dividendos/${ejercicio}`);
}

export async function fetchDividendosAcumulado(): Promise<DividendosResumen> {
  return fetchJson<DividendosResumen>('/api/dividendos/acumulado');
}

export async function fetchForex(ejercicio: number): Promise<ForexResumen> {
  return fetchJson<ForexResumen>(`/api/forex/${ejercicio}`);
}

export async function fetchForexAcumulado(): Promise<ForexResumen> {
  return fetchJson<ForexResumen>('/api/forex/acumulado');
}

export async function fetchIntereses(ejercicio: number): Promise<InteresesResumen> {
  return fetchJson<InteresesResumen>(`/api/intereses/${ejercicio}`);
}

export async function fetchInteresesAcumulado(): Promise<InteresesResumen> {
  return fetchJson<InteresesResumen>('/api/intereses/acumulado');
}

export async function fetchBills(ejercicio: number): Promise<BillsResumen> {
  return fetchJson<BillsResumen>(`/api/bills/${ejercicio}`);
}

export async function fetchBillsAcumulado(): Promise<BillsResumen> {
  return fetchJson<BillsResumen>('/api/bills/acumulado');
}

export async function fetchComplejos(ejercicio: number): Promise<ComplejosResumen> {
  return fetchJson<ComplejosResumen>(`/api/complejos/${ejercicio}`);
}

export async function fetchComplejosAcumulado(): Promise<ComplejosResumen> {
  return fetchJson<ComplejosResumen>('/api/complejos/acumulado');
}

export async function fetchPosiciones(): Promise<PosicionesResumen> {
  return fetchJson<PosicionesResumen>('/api/posiciones');
}

export async function fetchOpcionesAbiertas(): Promise<import('./types').OpcionAbierta[]> {
  return fetchJson('/api/opciones/abiertas');
}

export async function fetchConfig(): Promise<import('./types').ConfigCartera> {
  return fetchJson('/api/config');
}

export async function guardarConfig(
  cambios: { nombre_cartera?: string; objetivo_if_eur?: number; aportacion_mensual_eur?: number },
): Promise<import('./types').ConfigCartera> {
  return fetchJson('/api/config', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cambios),
  });
}

export async function guardarColumnasPosiciones(
  columnas: string[],
): Promise<PosicionesResumen> {
  return fetchJson<PosicionesResumen>('/api/posiciones/columnas', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ columnas }),
  });
}

// ── Bloques de estrategia ────────────────────────────────────────────────

export async function fetchDistribucionBloques(): Promise<
  import('./types').DistribucionBloques
> {
  return fetchJson('/api/bloques');
}

export async function fetchPosicionesBloque(): Promise<
  import('./types').PosicionBloque[]
> {
  return fetchJson('/api/bloques/posiciones');
}

export async function asignarBloque(
  isin: string,
  bloqueId: string | null,
  opts: { categoriaSugerida?: string; confianzaIa?: number; razon?: string } = {},
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/bloques/asignar`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      isin,
      bloque_id: bloqueId,
      categoria_sugerida: opts.categoriaSugerida ?? null,
      confianza_ia: opts.confianzaIa ?? null,
      razon: opts.razon ?? null,
    }),
  });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

export async function sugerirBloque(
  isin: string,
): Promise<import('./types').SugerenciaBloque> {
  return fetchJson('/api/bloques/sugerir', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ isin }),
  });
}

export async function autoclasificarCartera(
  opts: { soloSinClasificar?: boolean; isines?: string[] } = {},
): Promise<import('./types').SugerenciaBloque[]> {
  return fetchJson('/api/bloques/autoclasificar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      solo_sin_clasificar: opts.soloSinClasificar ?? true,
      isines: opts.isines ?? null,
    }),
  });
}

export async function crearBloque(
  nombre: string,
  categoriaBase: string,
): Promise<import('./types').BloqueItem> {
  return fetchJson('/api/bloques', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nombre, categoria_base: categoriaBase }),
  });
}

export async function editarBloque(
  id: string,
  campos: Partial<{
    nombre: string;
    categoria_base: string;
    liquidez_asignada_eur: number | null;
    rendimiento_pct: number | null;
    peso_objetivo: number | null;
    tolerancia: number | null;
    en_estrategia: boolean;
  }>,
): Promise<import('./types').BloqueItem> {
  return fetchJson(`/api/bloques/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(campos),
  });
}

export async function eliminarBloque(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/bloques/${id}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

// ── Plan por valor ───────────────────────────────────────────────────────

export async function fetchPlanPasos(
  estado?: string,
): Promise<import('./types').PasoPlan[]> {
  const q = estado ? `?estado=${estado}` : '';
  return fetchJson(`/api/plan${q}`);
}

export async function fetchPosicionesPlan(): Promise<
  import('./types').PosicionPlan[]
> {
  return fetchJson('/api/plan/posiciones');
}

export async function fetchHueco(): Promise<import('./types').HuecoAsignacion> {
  return fetchJson('/api/plan/hueco');
}

export async function crearPaso(
  payload: import('./types').PasoPlanIn,
): Promise<import('./types').PasoPlan> {
  return fetchJson('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function actualizarPaso(
  id: string,
  campos: Partial<{
    decision: string;
    prioridad: string;
    estado: string;
    razon: string | null;
    capital_objetivo_eur: string | null;
    fecha_objetivo: string | null;
    notas: string | null;
  }>,
): Promise<import('./types').PasoPlan> {
  return fetchJson(`/api/plan/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(campos),
  });
}

export async function eliminarPaso(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/plan/${id}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) {
    throw await apiError(res);
  }
}

// ── Estimaciones ─────────────────────────────────────────────────────────

export async function fetchEstimaciones(): Promise<
  import('./types').EstimacionesResumen
> {
  return fetchJson('/api/estimaciones');
}

export async function editarEstimacion(
  isin: string,
  campos: Partial<{
    tipo_val: string;
    eps_actual: number | null;
    multiplo_objetivo: number | null;
    metrica_base_4y: number | null;
    dividendo_share: number | null;
    notas: string | null;
  }>,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/estimaciones/${isin}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(campos),
  });
  if (!res.ok && res.status !== 204) throw await apiError(res);
}

export async function prefillEstimaciones(): Promise<{ actualizadas: number }> {
  return fetchJson('/api/estimaciones/prefill', { method: 'POST', body: '{}' });
}

export async function fetchSeguimiento(): Promise<
  import('./types').SeguimientoItem[]
> {
  return fetchJson('/api/seguimiento');
}

export async function anadirSeguimiento(
  ticker: string,
  notas?: string | null,
): Promise<import('./types').SeguimientoItem> {
  return fetchJson('/api/seguimiento', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, notas: notas ?? null }),
  });
}

export async function quitarSeguimiento(isin: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/seguimiento/${isin}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) throw await apiError(res);
}

export async function bootstrap(): Promise<{
  user_id: string;
  cartera_id: string;
  brokers: Record<string, string>;
  creado: boolean;
}> {
  return fetchJson('/api/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
}

// Utilidades de formateo (la API devuelve Decimal como string)

export function fmtEUR(n: string | number, opts: Intl.NumberFormatOptions = {}): string {
  const v = typeof n === 'string' ? parseFloat(n) : n;
  return new Intl.NumberFormat('es-ES', {
    style: 'currency',
    currency: 'EUR',
    maximumFractionDigits: 0,
    ...opts,
  }).format(v);
}

export function fmtPct(n: string | number, decimals = 1): string {
  const v = typeof n === 'string' ? parseFloat(n) : n;
  return `${(v * 100).toFixed(decimals)}%`;
}

export function fmtNum(n: string | number, opts: Intl.NumberFormatOptions = {}): string {
  const v = typeof n === 'string' ? parseFloat(n) : n;
  return new Intl.NumberFormat('es-ES', {
    maximumFractionDigits: 4,
    ...opts,
  }).format(v);
}
