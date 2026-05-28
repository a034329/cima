import type { Auditoria } from '@/lib/types';

const ICON: Record<string, string> = { OK: '✓', AVISO: '⚠', INFO: '·', VERIFICAR: '◻' };
const COLOR: Record<string, string> = {
  OK: 'text-emerald-600 dark:text-emerald-400',
  AVISO: 'text-amber-600 dark:text-amber-400',
  INFO: 'text-[rgb(var(--muted))]',
  VERIFICAR: 'text-sky-600 dark:text-sky-400',
};

/** Render compartido del veredicto de auditoría (compra y venta). */
export function AuditoriaVista({ a }: { a: Auditoria }) {
  const hayAviso = a.chequeos.some((c) => c.estado === 'AVISO');
  return (
    <div className="rounded-md border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-2 text-xs">
      <div className={`mb-1.5 font-medium ${hayAviso ? 'text-amber-700 dark:text-amber-400' : 'text-emerald-700 dark:text-emerald-400'}`}>
        Auditoría: {a.resumen}
      </div>
      <ul className="space-y-1">
        {a.chequeos.map((c, i) => (
          <li key={c.filtro + i} className="flex gap-2">
            <span className={`${COLOR[c.estado]} shrink-0`}>{ICON[c.estado]}</span>
            <span>
              <span className="font-medium">{c.filtro}:</span> {c.titulo}
              {c.detalle && <span className="text-[rgb(var(--muted))]"> — {c.detalle}</span>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
