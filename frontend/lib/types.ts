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

export type TipoVal = 'PER' | 'P_FCF' | 'P_BV' | 'P_FRE' | 'SOTP';

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
  // Desglose de la métrica neta (explicabilidad del CAGR4+Div):
  cagr4_div_bruto_pct: string | null;
  div_yield_neto_pct: string | null;
  div_horizonte_pct: string | null;
  tipo_efectivo_div_pct: string | null;
  crecimiento_div_pct: string | null;   // g_div aplicado (campo o derivado)
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
  cagr4_div_pct: string | null;
  cobertura: string | null;
}

export interface PosicionPeso {
  nombre: string;
  isin: string;
  categoria_base: CategoriaBase | null;
  valor_eur: string;
  peso: string;
}

export interface Escenario {
  nombre: string;
  multiplo: number;
  metrica_base_4y: number;
  precio_objetivo: number;
  cagr4_pct: number | null;
  razon: string;
  // Guardias post-cálculo (bug BAM 5-jun-2026)
  alertas?: string[];
  bloqueado?: boolean;
  desglose?: { etiqueta: string; valor: string; calc: string }[];
}

export interface Valoracion {
  isin: string;
  nombre: string;
  tipo_val: string;
  precio_actual: number | null;
  anclas: Record<string, number | string | null>;
  escenarios: Escenario[];
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
}

export interface AlertaVigilancia {
  isin: string;
  nombre: string;
  precio_anterior: string;
  precio_actual: string;
  cambio_pct: string;
  nivel: 'ALERTA' | 'CRITICA';
  modo?: 'baseline' | 'intradia';
}

export interface AlertaPlanPrecio {
  isin: string;
  nombre: string;
  decision: string;
  precio_alerta_eur: string;
  precio_actual_eur: string;
  paso_id: string;
  razon: string | null;
}

export interface Vigilancia {
  alertas: AlertaVigilancia[];                     // vs último "visto" (baseline)
  alertas_intradia?: AlertaVigilancia[];           // vs cierre de ayer (intra-día)
  alertas_plan?: AlertaPlanPrecio[];               // pasos habilitados por precio (V4)
  desde: string | null;
}

export interface MensajeAsesor {
  rol: 'user' | 'assistant';
  contenido: string;
  created_at: string;
}

export interface AccionPropuesta {
  tipo: 'crear_paso' | 'ajustar_estimacion';
  isin: string;
  descripcion: string;
  params: Record<string, unknown>;
}

export interface RespuestaAsesor {
  mensaje: MensajeAsesor;
  acciones: AccionPropuesta[];
}

export type EstadoAnalisis = 'ninguno' | 'en_curso' | 'ok' | 'error';

export interface OnePagerEstado {
  estado: EstadoAnalisis;
  error: string | null;
  resultado: OnePager | null;
}

export interface ValoracionEstado {
  estado: EstadoAnalisis;
  error: string | null;
  resultado: Valoracion | null;
}

export interface GapBloque {
  categoria_base: string;
  nombre: string;
  peso_actual: number;
  peso_objetivo: number;
  deficit_eur: number;          // >0 = falta invertir; <0 = exceso
  n_posiciones: number;
}

export interface PasoPropuesto {
  isin: string;
  nombre: string;
  decision: string;
  prioridad: string;
  capital_objetivo_eur: number | null;
  razon: string;
  en_cartera: boolean;
}

export interface HojaRuta {
  capital_eur: number;
  liquidez_eur: number;
  deficit: GapBloque[];
  pasos: PasoPropuesto[];
  huecos: string[];
  resumen: string;
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
}

export interface HojaRutaEstado {
  estado: EstadoAnalisis;
  error: string | null;
  resultado: HojaRuta | null;
}

export interface Peer {
  nombre: string;
  ticker: string;
  per: number | null;
  ev_ebitda: number | null;
  p_fcf: number | null;
  yield_pct: number | null;
  crecimiento_pct: number | null;
  roic_pct: number | null;
  es_objetivo: boolean;
}

export interface Comps {
  isin: string;
  nombre: string;
  sector: string;
  peers: Peer[];
  lectura: string;
  fuentes: string[];
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
}

export interface CompsEstado {
  estado: EstadoAnalisis;
  error: string | null;
  resultado: Comps | null;
}

export interface OnePager {
  isin: string;
  nombre: string;
  que_hace: string;
  tesis: string;
  riesgos: string;
  valoracion: string;
  encaje: string;
  veredicto: string;
  clasificacion: string;        // COYUNTURAL | GRIS | ESTRUCTURAL | ''
  fuentes: string[];
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
}

export interface AnalisisContexto {
  isin: string;
  nombre: string;
  resumen: string;
  clasificacion: 'COYUNTURAL' | 'GRIS' | 'ESTRUCTURAL' | 'SIN_DATOS';
  preguntas: { pregunta: string; respuesta: string; senal?: string }[];
  riesgo_principal: string;
  fuentes: string[];
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
  requiere_0b: boolean;
  motivo_0b: string;
}

export interface AnalisisCausaRaiz {
  isin: string;
  nombre: string;
  causa_exacta: string;
  profundidad: 'LIGERA' | 'MEDIA' | 'GRAVE' | 'SIN_DATOS';
  horizonte_resolucion: string;
  segmentos_afectados: { nombre: string; peso_pct: number; impacto: string }[];
  evidencias: string[];
  conclusion: string;
  nueva_clasificacion: 'COYUNTURAL' | 'GRIS' | 'ESTRUCTURAL' | '';
  fuentes: string[];
  fecha: string;
  proveedor: string;
  disclaimer: string | null;
}

export interface Chequeo {
  filtro: string;
  estado: 'OK' | 'AVISO' | 'INFO' | 'VERIFICAR';
  titulo: string;
  detalle: string;
}

export interface Auditoria {
  isin: string;
  nombre: string;
  decision: string;
  bloque_objetivo: string | null;
  chequeos: Chequeo[];
  resumen: string;
}

export type SenalMacro = 'VERDE' | 'AMARILLA' | 'ROJA';

export interface CorreccionVentana {
  sp_drawdown: number | null;     // fracción negativa
  vix: number | null;
  activa: boolean;
  escalado_min: number | null;
  escalado_max: number | null;
  nota: string;
}

export interface RegimenEstado {
  indicadores: { ciclo: SenalMacro; inflacion: SenalMacro; geopolitica: SenalMacro; mercado: SenalMacro };
  regimen: 'VERDE' | 'AMARILLO' | 'ROJO';
  tramo_min: number;
  tramo_max: number;
  espaciado: string;
  actualizado: string | null;
  correccion: CorreccionVentana | null;
}

export type ClaveIndicadorMacro = 'ciclo' | 'inflacion' | 'geopolitica' | 'mercado';

export interface IndicadorPropuesta {
  senal: SenalMacro;
  razon: string;
  fuentes: string[];
  datos: Record<string, number | string | null>;
}

export interface PropuestaRegimen {
  indicadores: Record<ClaveIndicadorMacro, IndicadorPropuesta>;
  regimen: 'VERDE' | 'AMARILLO' | 'ROJO';
  datos_objetivos: Record<string, number | null>;
  proveedor: string;
  modelo: string;
  created_at: string;
}

export interface RegimenAutoEstado {
  estado: 'ninguno' | 'en_curso' | 'ok' | 'error';
  error: string | null;
  propuesta: PropuestaRegimen | null;
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
  liquidez_eur: string;                       // disponible = total − fuera_estrategia
  liquidez_total_eur: string;
  liquidez_fuera_estrategia_eur: string;
  progreso_if_pct: string;
  anios_if: string | null;
  retorno_if_pct: string;
  composicion: CompBloque[];
  posiciones_peso: PosicionPeso[];
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
  cagr4_div_pct: string | null;          // CAGR4+Div proyectado ponderado del bloque
  cobertura_estimacion: string | null;   // fracción del valor del bloque con estimación
  n_con_estimacion: number;
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
  isin?: string | null;
  posicion_nombre?: string | null;
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
  posicion_id?: string;
  confirmar_directo?: boolean;
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
  cagr4_div_pct: string | null;
  rentab_total_hist_pct: string;
  primas_opc_anio: string;
  primas_opc_hist: string;
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
  precio_alerta_eur: string | null;   // gatillo de alerta plan↔precio (V4)
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
  fecha_objetivo: string | null;           // deadline (manual)
  proximo_tramo_fecha: string | null;      // DCA en curso: estimación próximo tramo
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
  criterios: string;
}

export interface CriterioCheck {
  etiqueta: string;
  valor: number | null;
  valor_txt: string;
  objetivo_txt: string;
  cumple: boolean | null;   // null = dato no disponible
}

export interface EvaluacionCandidato {
  isin: string;
  nombre: string;
  categoria_sugerida: CategoriaBase;
  confianza: number;
  razonamiento: string;
  criterios_texto: string;
  checks: CriterioCheck[];
  n_cumplidos: number;
  n_medibles: number;
  veredicto: string;
  cualitativo: string;
  target_categoria: CategoriaBase | null;
  cubre_target: boolean | null;
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
  precio_alerta_eur?: string | null;
  // Datos para auto-añadir al watchlist si el ISIN no está en cartera ni en
  // seguimiento y la decisión es de compra/hold (watchlist-first).
  nombre?: string | null;
  ticker?: string | null;
  friccion_severidad?: string | null;
  friccion_motivo?: string | null;
}

// ── Onboarding IA (diseña tu estrategia y fírmala) ────────────────────────

export interface PerfilOnboarding {
  objetivo_if_eur?: string | number | null;
  horizonte_anios?: number | null;
  aportacion_mensual_eur?: string | number | null;
  tolerancia?: string | null;     // conservador | moderado | agresivo
  fase?: string | null;           // acumulacion | preservacion
}

export interface PropuestaBloque {
  categoria_base: CategoriaBase;
  peso_objetivo: number;          // fracción 0..1
  razon: string;
}

export interface Viabilidad {
  capital_actual_eur: number;
  aportaciones_eur: number;
  cagr_requerido_pct: number | null;
  viable: boolean;
  veredicto: string;
}

export interface PropuestaEstrategia {
  bloques: PropuestaBloque[];
  resumen: string;
  disclaimer: string | null;
  viabilidad: Viabilidad | null;
}

export interface PlanFirmado {
  version: number;
  perfil: Record<string, unknown>;
  objetivos: Record<string, number>;
  resumen: string | null;
  fecha: string;
}

// ── Fricción conductual (avisa, rebate 2 veces, te deja) ──────────────────

export interface FriccionResultado {
  severidad: 'ALTA' | 'MEDIA';
  titulo: string;
  rebate1: string;
  rebate2: string;
  etiquetas: string[];
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
  delta_anios_if: string | null;   // años de IF que retrasa (+) el coste fiscal
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
  neto: string;
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

// ── Fugas fiscales (exceso CDI no recuperable) ──────────────────────────────

export interface FugaAnio {
  ejercicio: number;
  exceso_eur: string;
  dentro_plazo: boolean;
  limite: string | null;
  reclamado: boolean;
}

export interface FugaPosicion {
  isin: string;
  nombre: string;
  pais: string;
  exceso_pct: string;                       // fracción (0.20 = 20 puntos)
  div_anual_estimado_eur: string | null;
  fuga_anual_estimada_eur: string | null;
  exceso_real_total_eur: string;
}

export interface FugaPais {
  pais: string;
  exceso_pct: string;
  fuga_anual_estimada_eur: string;
  reclamable_pendiente_eur: string;
  reclamado_eur: string;
  fuera_plazo_eur: string;
  plazo_anios: number;
  plazo_verificado: boolean;
  mecanismo: string;
  anios: FugaAnio[];
  posiciones: FugaPosicion[];
}

export interface FugasResumen {
  ejercicio: number;
  ventana_anios: number;
  total_fuga_anual_estimada_eur: string;
  total_reclamable_pendiente_eur: string;
  por_pais: FugaPais[];
}

// ── Salud / frescura de datos ───────────────────────────────────────────────

export interface SaludDatos {
  precios_ts: string | null;
  fx_ts: string | null;
  fundamentales_ts: string | null;
  ultimo_import_ts: string | null;
  ultimo_import_desc: string | null;
  ultima_transaccion: string | null;
}

// ── Salud del dividendo (V6) ────────────────────────────────────────────────

export interface SaludDividendo {
  isin: string;
  nivel: 'ALTA' | 'MEDIA' | 'RIESGO' | 'SIN_DATOS';
  motivo: string;
  fcf_cobertura: number | null;
  payout: number | null;
}

// ── Informe mensual (V3) ────────────────────────────────────────────────────

export interface MovimientoDestacado {
  fecha: string;
  tipo: string;
  nombre: string;
  importe_eur: string;
}

export interface VentaRealizadaMes {
  nombre: string;
  isin: string;
  gp_eur: string;
}

export interface InformeMensual {
  anio: number;
  mes: number;
  compras_eur: string;
  n_compras: number;
  ventas_eur: string;
  n_ventas: number;
  gastos_eur: string;
  aportaciones_eur: string;
  dividendos_bruto_eur: string;
  dividendos_retencion_eur: string;
  dividendos_neto_eur: string;
  intereses_eur: string;
  gp_realizada_eur: string;
  capital_estrategia_eur: string | null;
  progreso_if_pct: string | null;
  anios_if: string | null;
  destacados: MovimientoDestacado[];
  ventas_detalle: VentaRealizadaMes[];
}
