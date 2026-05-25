// Fuente única de etiquetas y colores por categoría base de bloque.
// Espejo de las FICHAS del backend (cima/backend/app/adapters/ia/prompt.py).
import type { CategoriaBase } from './types';

export const CAT_LABEL: Record<CategoriaBase, string> = {
  growth: 'Compounders',
  income: 'Dividend Growth',
  defensivo: 'Estable',
  aggressive: 'High Yield',
  satelite: 'Satélite',
  indice: 'Índice',
  renta_fija: 'Renta Fija',
  cripto: 'Cripto',
  materias_primas: 'Materias primas',
  colchon: 'Colchón',
  sin_clasificar: 'Sin clasificar',
};

// Clases Tailwind para badges.
export const CAT_COLOR: Record<CategoriaBase, string> = {
  growth: 'bg-brand-100 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300',
  income: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
  defensivo: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  aggressive: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  satelite: 'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-400',
  indice: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
  renta_fija: 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  cripto: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  materias_primas: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-500',
  colchon: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400',
  sin_clasificar: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
};

// Hex sólidos para gráficas (donut del dashboard, barras).
export const CAT_HEX: Record<CategoriaBase, string> = {
  growth: '#E6B763',        // oro
  income: '#5B9279',        // verde salvia
  defensivo: '#8C8577',     // taupe
  aggressive: '#C2693B',    // terracota
  satelite: '#8E6FB0',      // violeta
  indice: '#5C8AB4',        // azul
  renta_fija: '#4F9D9D',    // teal
  cripto: '#D08A3E',        // naranja cripto
  materias_primas: '#C9A227', // oro mate
  colchon: '#7E8AA2',       // azul pizarra
  sin_clasificar: '#57514A',
};

// Categorías que el usuario puede elegir al crear un bloque (base 6 + 2 opcionales).
export const CAT_ASIGNABLES: CategoriaBase[] = [
  'growth', 'income', 'defensivo', 'aggressive', 'satelite', 'colchon',
  'indice', 'renta_fija', 'cripto', 'materias_primas',
];
