import type { DecisionPlan, PrioridadPlan } from './types';

export const DECISIONES: DecisionPlan[] = [
  'COMPRAR', 'REFORZAR', 'MANTENER', 'MONITORIZAR', 'RECORTAR', 'VENDER', 'ESPERAR',
];

export const PRIORIDADES: PrioridadPlan[] = ['CRITICA', 'ALTA', 'MEDIA', 'BAJA'];

export const DECISION_LABEL: Record<DecisionPlan, string> = {
  COMPRAR: 'Comprar',
  REFORZAR: 'Reforzar (DCA)',
  MANTENER: 'Mantener',
  MONITORIZAR: 'Monitorizar',
  RECORTAR: 'Recortar',
  VENDER: 'Vender',
  ESPERAR: 'Esperar',
};

export const DECISION_COLOR: Record<DecisionPlan, string> = {
  COMPRAR: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
  REFORZAR: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
  MANTENER: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  MONITORIZAR: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  RECORTAR: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  VENDER: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
  ESPERAR: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400',
};

export const PRIORIDAD_LABEL: Record<PrioridadPlan, string> = {
  CRITICA: 'Crítica',
  ALTA: 'Alta',
  MEDIA: 'Media',
  BAJA: 'Baja',
};
