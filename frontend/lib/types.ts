// Tipos espejo de los Pydantic models del backend (cima/backend/app/routers/cartera.py).
// En el futuro: generar automáticamente con `openapi-typescript` desde el OpenAPI del backend.

export type CategoriaBase =
  | 'defensivo'
  | 'income'
  | 'growth'
  | 'aggressive'
  | 'satelite'
  | 'indice'
  | 'renta_fija'
  | 'cripto'
  | 'materias_primas'
  | 'colchon'
  | 'sin_clasificar';

export interface Bloque {
  nombre: string;
  categoria_base: CategoriaBase;
  peso_objetivo: string;   // Decimal serializado como string
  peso_actual: string;
  desviacion: string;
  valor_eur: string;
}

export interface PosicionResumen {
  isin: string;
  ticker: string;
  nombre: string;
  divisa_local: string;
  cantidad: string;
  pm_real_eur: string;
  pm_fiscal_es_eur: string;
  pm_opciones_total_eur: string;
  precio_actual_local: string;
  valor_eur: string;
  plusvalia_latente_eur: string;
  bloque: string | null;
  fecha_actualizacion: string;
}

// ── Estimaciones (Fase 2) ────────────────────────────────────────────────

export type TipoVal = 'PER' | 'P_FCF' | 'P_BV' | 'P_FRE';

export interface EstimacionItem {
  isin: string;
  nombre: string;
  tipo_val: TipoVal;
  divisa: string | null;
  precio_actual: string | null;
  eps_actual: string | null;
  multiplo_objetivo: string | null;
  metrica_base_4y: string | null;
  dividendo_share: string | null;
  precio_objetivo: string | null;
  crecimiento_pct: string | null;
  cagr4_pct: string | null;
  div_yield_pct: string | null;
  cagr4_div_pct: string | null;
  notas: string | null;
  // Consenso de analistas (referencia, no editable):
  eps_forward: string | null;
  eps_consenso_4y: string | null;
  eps_consenso_high: string | null;
  eps_consenso_low: string | null;
  num_analistas_eps: number | null;
  anio_consenso_4y: number | null;
  precio_obj_consenso: string | null;
  target_high: string | null;
  target_low: string | null;
  per_hist_medio: string | null;
  per_hist_mediano: string | null;
  mult_alerta: string | null;
}

export interface EstimacionesResumen {
  estimaciones: EstimacionItem[];
  yield_estimado_pct: string | null;
  cagr4_div_ponderado_pct: string | null;
  cobertura: string;
}

export interface SeguimientoItem {
  isin: string;
  ticker: string;
  nombre: string | null;
  divisa: string | null;
  notas: string | null;
  bloque_id: string | null;
  bloque_nombre: string | null;
  estimacion: EstimacionItem;
}

// ── Dashboard (pantalla Resumen) ─────────────────────────────────────────

export interface CompBloque {
  nombre: string;
  categoria_base: CategoriaBase;
  valor_eur: string;
  peso: string;
}

export interface PasoResumen {
  isin: string;
  nombre: string;
  decision: DecisionPlan;
  prioridad: PrioridadPlan;
}

export interface OpcionRiesgo {
  simbolo: string;
  tipo_op: string;
  strike: string;
  vencimiento: string;
  dias_a_vencer: number | null;
  moneyness: string | null;
  es_corta: boolean;
  riesgo_ejercicio: boolean;
}

export interface DashboardData {
  anio: number;
  fecha_calculo: string;
  capital_mercado_eur: string;
  gp_no_realizada_eur: string;
  gp_no_realizada_pct: string;
  liquidez_eur: string;
  progreso_if_pct: string;
  anios_if: string | null;
  retorno_if_pct: string;
  composicion: CompBloque[];
  yield_actual_pct: string;
  dividendos_brutos_anio: string;
  yield_estimado_pct: string | null;
  cagr_anual_pct: string | null;
  retorno_5y_pct: string | null;
  proximos_pasos: PasoResumen[];
  gp_realizada_anio: string;
  perdidas_por_aflorar: string;
  compensable_ahora: string;
  perdida_a_arrastrar: string;
  opciones_riesgo: OpcionRiesgo[];
  opciones_proximas_vencer: number;
  opciones_itm: number;
}

// ── Bloques de estrategia ────────────────────────────────────────────────

export interface BloqueDist {
  id: string;
  nombre: string;
  categoria_base: CategoriaBase;
  orden: number;
  es_base: boolean;
  en_estrategia: boolean;
  valor_eur: string;
  peso_actual: string;
  n_posiciones: number;
  liquidez_asignada_eur: string;
  rendimiento_pct: string | null;
  peso_objetivo: string | null;
  tolerancia: string;
  desviacion: string | null;
  fuera_tolerancia: boolean;
}

export interface DistribucionBloques {
  total_eur: string;
  liquidez_disponible_eur: string;
  bloques: BloqueDist[];
}

export interface PosicionBloque {
  isin: string;
  nombre: string;
  valor_eur: string;
  bloque_id: string | null;
}

export interface BloqueItem {
  id: string;
  nombre: string;
  categoria_base: CategoriaBase;
  orden: number;
  es_base: boolean;
}

export interface SugerenciaBloque {
  isin?: string | null;
  categoria_base: CategoriaBase;
  bloque_id: string | null;
  razonamiento: string;
  confianza: number;
  modelo: string;
  proveedor: string;
}

export interface CarteraResumen {
  cartera_id: string;
  nombre: string;
  capital_total_eur: string;
  progreso_if_pct: string;
  anos_estimados_if: string;
  yield_actual_pct: string;
  anio: number;
  dividendos_bruto_anio: string;
  opciones_neto_anio: string;
  gp_realizada_anio: string;
  aportacion_neta_anio: string;
  liquidez_eur: string;
  bloques: Bloque[];
  posiciones: PosicionResumen[];
  fecha_snapshot: string;
}

// ── Transacciones ─────────────────────────────────────────────────────

export type TipoTransaccion =
  | 'BUY'
  | 'SELL'
  | 'DIVIDEND'
  | 'INTEREST'
  | 'STAKING_REWARD'
  | 'CORPORATE_SPLIT'
  | 'CORPORATE_ISIN_CHANGE'
  | 'CORPORATE_SCRIP'
  | 'CORPORATE_RIGHTS'
  | 'CORPORATE_MERGER'
  | 'CORPORATE_OPA'
  | 'OTRO';

export type EstadoTransaccion = 'pendiente_confirmar' | 'confirmada' | 'descartada';

export interface TransaccionOut {
  id: string;
  cartera_id: string;
  broker_id: string | null;
  posicion_id: string;
  fecha: string;
  tipo: TipoTransaccion;
  cantidad: string;
  precio_local: string;
  divisa_local: string;
  importe_local: string;
  fx_rate: string;
  importe_eur: string;
  gastos_eur: string;
  tasas_externas_eur: string;
  retencion_eur: string;
  retencion_pais: string | null;
  estado: EstadoTransaccion;
  origen: string;
  external_id: string | null;
  notas: string | null;
  created_at: string;
  updated_at: string;
}

export interface TransaccionIn {
  isin: string;
  ticker?: string;
  nombre?: string;
  broker_id?: string;
  fecha: string;     // YYYY-MM-DD
  tipo: TipoTransaccion;
  cantidad: string;
  precio_local: string;
  divisa_local: string;
  importe_local: string;
  fx_rate: string;
  importe_eur: string;
  gastos_eur?: string;
  tasas_externas_eur?: string;
  retencion_eur?: string;
  retencion_pais?: string;
  notas?: string;
}

export interface ImportResultado {
  broker: string;
  insertadas: number;
  deduplicadas: number;
  reconciliadas: number;
  conflictos: number;
  huerfanas_manuales: number;
  opciones_insertadas: number;
  opciones_deduplicadas: number;
  avisos: string[];
}

// ── Fiscal ─────────────────────────────────────────────────────────────

export interface FifoMatch {
  isin: string;
  nombre: string;
  fecha_compra: string;
  fecha_venta: string;
  cantidad: string;
  coste_adquisicion: string;
  importe_transmision: string;
  gastos_venta: string;
  gastos_compra: string;
  ganancia_perdida: string;
  ejercicio_fiscal: number;
  regla_2_meses: boolean;
  regla_2_meses_detalle: string;
  es_scrip: boolean;
  es_corto: boolean;
  broker_compra: string;
  broker_venta: string;
  instrument_type: string;
  lote_id: number;
  perdida_diferida_aflorada_eur: string;
}

export interface PositionSummary {
  isin: string;
  nombre: string;
  cantidad_total: string;
  coste_total_eur: string;
  pm_ponderado_eur: string;
  num_lotes: number;
  lote_mas_antiguo: string;
  lote_mas_reciente: string;
  es_mixta: boolean;
}

export interface PerdidaDiferida {
  isin: string;
  nombre: string;
  importe_eur: string;
  cantidad_pendiente: string;
  fecha_venta_origen: string;
  ejercicio_origen: number;
  lote_id_recompra: number;
}

export interface OrphanSale {
  isin: string;
  nombre: string;
  fecha: string;
  cantidad: string;
  importe_eur: string;
  broker: string;
  parcial: boolean;
  cantidad_faltante: string;
}

export interface PerdidaPendiente {
  ejercicio_origen: number;
  importe_original_eur: string;
  compensado_eur: string;
  pendiente_eur: string;
  expira: number;
  detalle: string;
}

export interface Compensacion {
  ejercicio: number;
  gp_bruto: string;
  gp_no_deducible_2m: string;
  gp_deducible: string;
  rcm_neto: string;
  opciones_pl: string;
  gp_total: string;
  saldo_gp_tras_intra: string;
  cruce_gp_a_rcm: string;
  cruce_rcm_a_gp: string;
  saldo_gp_tras_cruce: string;
  saldo_rcm_tras_cruce: string;
  perdidas_anteriores: PerdidaPendiente[];
  aplicadas_de_anteriores: string;
  saldo_gp_final: string;
  nuevo_saldo_negativo: string;
  perdidas_actualizadas: PerdidaPendiente[];
  perdidas_expiradas: PerdidaPendiente[];
  perdidas_proximas_expirar: PerdidaPendiente[];
  base_ahorro_gp: string;
  base_ahorro_rcm: string;
}

export interface FiscalResumen {
  ejercicio: number;
  cartera_id: string;
  fecha_corte: string;
  fecha_calculo: string;
  gp_bruto: string;
  gp_no_deducible_2m: string;
  total_perdida_aflorada: string;
  rcm_neto: string;
  n_matches: number;
  matches: FifoMatch[];
  positions: PositionSummary[];
  perdidas_diferidas_latentes: PerdidaDiferida[];
  orphan_sales: OrphanSale[];
  warnings: string[];
  compensacion: Compensacion;
}

// ── Opciones ───────────────────────────────────────────────────────────

export interface ContratoOpcion {
  subyacente: string;
  tipo_op: string;
  strike: string;
  vencimiento: string;
  brokers: string;
  clasificacion: string;
  primas_cobradas: string;
  primas_pagadas: string;
  gastos: string;
  pl_bruto: string;
  pl_neto: string;
  contratos_vendidos: string;
  contratos_comprados: string;
  expiradas: number;
  n_ejercidas: number;
  n_net_abiertos: number;
}

export interface OpcionesResumen {
  ejercicio: number;
  fecha_calculo: string;
  n_opciones: number;
  n_contratos: number;
  pl_neto: string;
  pl_bruto: string;
  primas_cobradas: string;
  primas_pagadas: string;
  gastos: string;
  n_expiradas: number;
  ejercidas_prima_integrar: string;
  long_abiertas_coste: string;
  short_abiertas_prima: string;
  contratos: ContratoOpcion[];
}

// ── Posiciones (métricas) ───────────────────────────────────────────────

export interface ColumnaCatalogo {
  id: string;
  label: string;
  default: boolean;
  fija: boolean;
}

export interface PosicionMetricas {
  isin: string;
  nombre: string;
  cantidad: string;
  pm_real: string;
  precio_actual_eur: string;
  gp_no_realizada_eur: string;
  gp_no_realizada_pct: string;
  rentab_total_pct: string;
  pm_fiscal_es: string;
  opciones_ejercidas_anio: string;
  opciones_ejercidas_hist: string;
  dividendos_anio: string;
  dividendos_hist: string;
  pm_desc: string;
  importe_diferido_2m: string;
  gp_realizada_anio: string;
  decision: DecisionPlan;
  tipo_activo: 'STOCK' | 'ETF' | 'CRYPTO';
  precio_actual_local: string | null;
  divisa_cotizacion: string | null;
  umbral_rotacion_1y_pct: string | null;
  umbral_rotacion_2y_pct: string | null;
  umbral_rotacion_3y_pct: string | null;
  umbral_rotacion_4y_pct: string | null;
}

export interface OpcionAbierta {
  subyacente: string;
  tipo_op: string;        // 'C' / 'P'
  strike: string;
  vencimiento: string;
  contratos: number;
  es_corta: boolean;
  prima_neta_eur: string;
  dias_a_vencer: number | null;
  moneyness: string | null;   // 'ITM' | 'OTM' | null
  precio_subyacente: string | null;
  divisa_subyacente: string | null;
  gp_estimada_eur: string | null;
  gp_estimada_pct: string | null;
}

// ── Configuración ─────────────────────────────────────────────────────────

export interface BrokerConfig {
  broker_tipo: string;
  alias: string | null;
  saldo_reportado_eur: string | null;
  saldo_fecha: string | null;
}

export interface ConfigCartera {
  email: string;
  nombre_cartera: string;
  modo: string;            // 'saas' | 'owner'
  objetivo_if_eur: string;
  aportacion_mensual_eur: string;
  brokers: BrokerConfig[];
}

// ── Plan por valor ───────────────────────────────────────────────────────

export type DecisionPlan =
  | 'COMPRAR'
  | 'REFORZAR'
  | 'MANTENER'
  | 'MONITORIZAR'
  | 'RECORTAR'
  | 'VENDER'
  | 'ESPERAR';

export type PrioridadPlan = 'CRITICA' | 'ALTA' | 'MEDIA' | 'BAJA';
export type EstadoPlan = 'PENDIENTE' | 'EN_CURSO' | 'COMPLETADO' | 'CANCELADO';

export interface PasoPlan {
  id: string;
  isin: string;
  decision: DecisionPlan;
  prioridad: PrioridadPlan;
  estado: EstadoPlan;
  capital_objetivo_eur: string | null;
  razon: string | null;
  fecha_objetivo: string | null;
  notas: string | null;
  orden: number;
}

export interface PosicionPlan {
  isin: string;
  nombre: string;
  valor_eur: string;
  bloque_id: string | null;
  bloque_nombre: string | null;
  decision: DecisionPlan;
  capital_objetivo_eur: string | null;
  razon: string | null;
  prioridad: PrioridadPlan | null;
  paso_id: string | null;
  en_cartera: boolean;
}

// ── Hueco de asignación (plan de compra top-down) ─────────────────────────

export interface HuecoBloque {
  bloque_id: string;
  nombre: string;
  categoria_base: CategoriaBase;
  objetivo_pct: string | null;
  actual_pct: string;
  planeado_pct: string;
  proyectado_pct: string;
  deficit_pct: string | null;
  valor_actual_eur: string;
  planeado_eur: string;
  deficit_eur: string | null;
}

export interface HuecoAsignacion {
  total_actual_eur: string;
  total_planeado_eur: string;
  total_proyectado_eur: string;
  sin_clasificar_planeado_eur: string;
  bloques: HuecoBloque[];
}

export interface PasoPlanIn {
  isin: string;
  decision: DecisionPlan;
  prioridad?: PrioridadPlan;
  razon?: string | null;
  capital_objetivo_eur?: string | null;
  fecha_objetivo?: string | null;
  notas?: string | null;
}

export interface PosicionesResumen {
  anio: number;
  columnas_catalogo: ColumnaCatalogo[];
  columnas_seleccionadas: string[];
  posiciones: PosicionMetricas[];
  precios_actualizados: string | null;
}

// ── Dividendos ─────────────────────────────────────────────────────────

export interface EventoDividendo {
  fecha: string;
  broker: string;
  bruto: string;
  retencion: string;
}

export interface DividendoPorIsin {
  isin: string;
  nombre: string;
  pais: string;
  bruto: string;
  ret_origen: string;
  retencion_es: string;
  tasa_cdi: string | null;
  limite_cdi: string;
  recuperable: string;
  exceso: string;
  es_nacional: boolean;
  sin_cdi: boolean;
  sin_retencion_es: boolean;
  brokers: string;
  eventos: EventoDividendo[];
}

export interface DividendosResumen {
  ejercicio: number;
  fecha_calculo: string;
  n_pagadores: number;
  bruto_total: string;
  ret_origen_total: string;
  ret_es_total: string;
  cdi_recuperable_total: string;
  exceso_total: string;
  bruto_ext_con_ret: string;
  pagadores: DividendoPorIsin[];
}

// ── Resumen del ejercicio (cuadro IRPF integrado) ────────────────────────

export interface ResumenFiscal {
  ejercicio: number;
  fecha_calculo: string;
  gp_acciones: string;            // 0326-0340
  gp_derechos: string;            // 0341-0355
  gp_estructurados: string;       // 1624-1654
  perdidas_afloradas: string;     // las declara el usuario
  gp_no_deducible_2m: string;
  forex_realized: string;
  opciones_pl: string;            // casilla 1626
  dividendos_bruto: string;       // casilla 0029
  dividendos_ret_es: string;
  intereses_rcm: string;          // casilla 0023
  letras_rcm: string;
  intereses_debit: string;        // informativo no deducible
  cdi_recuperable: string;        // casilla 0588
  base_ahorro_gp: string;
  base_ahorro_rcm: string;
  base_ahorro_total: string;
  compensacion: Compensacion;
}

// ── Optimizador fiscal de cierre de año ──────────────────────────────────

export interface LatentePosicion {
  isin: string;
  nombre: string;
  cantidad: string;
  pm_real_eur: string;
  precio_actual_eur: string | null;
  valor_actual_eur: string | null;
  gp_latente_eur: string | null;
  es_perdida: boolean;
  bloqueo_2m: boolean;
  precio_manual: boolean;
  sin_precio: boolean;
}

export interface OptimizadorFiscal {
  ejercicio: number;
  fecha_calculo: string;
  gp_realizada_ytd: string;
  rcm_ytd: string;
  bolsas_pendientes: string;
  perdida_a_arrastrar_anio: string;
  diferidas_2m: string;
  perdida_latente_cosechable: string;
  ganancia_latente_total: string;
  compensable_ahora: string;
  latentes: LatentePosicion[];
  no_resueltos: string[];
}

export interface PerdidaPendienteManual {
  ejercicio_origen: number;
  importe_eur: string;
  expira: number;
}

// ── Filtro fiscal de rotación (umbrales R-U del modelo WG) ────────────────

export interface RotacionItem {
  isin: string;
  nombre: string;
  valor_eur: string;
  gp_latente_eur: string;
  coste_fiscal_eur: string;
  tipo_efectivo_pct: string | null;
  cagr4_div_origen_pct: string | null;
  umbral_1y_pct: string | null;
  umbral_2y_pct: string | null;
  umbral_3y_pct: string | null;
  umbral_4y_pct: string | null;
}

export interface RotacionFiscal {
  ejercicio: number;
  fecha_calculo: string;
  base_ahorro_actual_eur: string;
  items: RotacionItem[];
  sin_estimacion: string[];
}

// ── Serie temporal de dividendos (gráficas) ──────────────────────────────

export interface PuntoAnualDiv {
  anio: number;
  bruto: string;
  neto: string;
}

export interface PuntoMensualDiv {
  anio: number;
  mes: number;
  bruto: string;
}

export interface SerieDividendos {
  anual: PuntoAnualDiv[];
  mensual: PuntoMensualDiv[];
}

export interface TrozoDiv {
  clave: string;
  bruto: string;
}

export interface DiversificacionDividendos {
  anio: number | null;
  bruto_total: string;
  por_empresa: TrozoDiv[];
  por_pais: TrozoDiv[];
  por_sector: TrozoDiv[];
}

// ── Forex (Art. 33.5.e) ──────────────────────────────────────────────────

export interface ForexLinea {
  divisa: string;
  realized_eur: string;
  unrealized_eur: string;
}

export interface ForexResumen {
  ejercicio: number;
  fecha_calculo: string;
  realized_total: string;       // declarable
  unrealized_total: string;     // latente, informativo
  periodo_inicio: string | null;
  periodo_fin: string | null;
  lineas: ForexLinea[];
}

// ── Intereses (RCM 0023 / informativo) ───────────────────────────────────

export interface InteresLinea {
  fecha: string;
  tipo: string;                 // credit / debit / bond_interest
  casilla: string | null;
  descripcion: string;
  divisa: string;
  importe_eur: string;
  broker: string;
}

export interface InteresesResumen {
  ejercicio: number;
  fecha_calculo: string;
  rcm_total: string;            // casilla 0023
  debit_total: string;          // informativo no deducible
  neto_total: string;
  n_lineas: number;
  lineas: InteresLinea[];
}

// ── Bills / Letras del Tesoro (RCM) ──────────────────────────────────────

export interface BillLinea {
  simbolo: string;
  realized_eur: string;
}

export interface BillsResumen {
  ejercicio: number;
  fecha_calculo: string;
  realized_total: string;       // RCM
  periodo_inicio: string | null;
  periodo_fin: string | null;
  lineas: BillLinea[];
}

// ── Productos complejos (detección) ──────────────────────────────────────

export interface ComplejoLinea {
  fecha: string | null;
  simbolo: string;
  isin: string | null;
  nombre: string;
  asset_category: string;
  cantidad: string;
  importe_eur: string;
  broker: string;
}

export interface ComplejosResumen {
  ejercicio: number;
  fecha_calculo: string;
  n: number;
  lineas: ComplejoLinea[];
}
