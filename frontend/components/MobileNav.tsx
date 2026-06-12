'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

/** Menú de navegación para pantallas pequeñas (hamburguesa). En escritorio
 *  la nav inline del header sigue siendo la principal (este componente va
 *  oculto con `md:hidden`). */
export function MobileNav({ items }: { items: { href: string; label: string }[] }) {
  const [abierto, setAbierto] = useState(false);
  const pathname = usePathname();

  // Cierra al navegar (el panel no debe quedarse sobre la página nueva).
  useEffect(() => { setAbierto(false); }, [pathname]);

  return (
    <div className="md:hidden">
      <button
        aria-label={abierto ? 'Cerrar menú' : 'Abrir menú'}
        aria-expanded={abierto}
        onClick={() => setAbierto((v) => !v)}
        className="p-2 -mr-2 text-[rgb(var(--fg))]"
      >
        {abierto ? (
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M5 5l10 10M15 5L5 15" />
          </svg>
        ) : (
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M3 5h14M3 10h14M3 15h14" />
          </svg>
        )}
      </button>
      {abierto && (
        <nav className="absolute left-0 right-0 top-full border-b border-[rgb(var(--border))] bg-[rgb(var(--card))] shadow-lg">
          <ul className="px-6 py-2">
            {items.map((n) => (
              <li key={n.href}>
                <Link
                  href={n.href}
                  className="block py-2.5 text-sm text-[rgb(var(--fg))] border-b border-[rgb(var(--border))]/40 last:border-0"
                >
                  {n.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  );
}
