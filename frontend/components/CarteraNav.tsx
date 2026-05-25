'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const TABS = [
  { href: '/', label: 'Resumen' },
  { href: '/posiciones', label: 'Posiciones' },
  { href: '/dividendos', label: 'Dividendos' },
];

export function CarteraNav() {
  const pathname = usePathname();
  return (
    <div className="mb-6">
      <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <h2 className="text-2xl font-semibold tracking-tight">Cartera</h2>
      </div>
      <nav className="flex flex-wrap gap-1 border-b border-[rgb(var(--border))]">
        {TABS.map((t) => {
          const activa = pathname === t.href;
          return (
            <Link
              key={t.href}
              href={t.href}
              className={`px-3 py-2 text-sm -mb-px border-b-2 ${
                activa
                  ? 'border-brand-600 text-[rgb(var(--fg))] font-medium'
                  : 'border-transparent text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]'
              }`}
            >
              {t.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
