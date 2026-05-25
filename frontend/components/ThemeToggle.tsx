'use client';

import { useEffect, useState } from 'react';

export function ThemeToggle() {
  const [dark, setDark] = useState(true);

  useEffect(() => {
    setDark(document.documentElement.classList.contains('dark'));
  }, []);

  const toggle = () => {
    const root = document.documentElement;
    const nuevo = !root.classList.contains('dark');
    root.classList.toggle('dark', nuevo);
    root.style.colorScheme = nuevo ? 'dark' : 'light';
    try {
      localStorage.setItem('cima-theme', nuevo ? 'dark' : 'light');
    } catch {
      /* ignore */
    }
    setDark(nuevo);
  };

  return (
    <button
      onClick={toggle}
      aria-label={dark ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro'}
      title={dark ? 'Tema claro' : 'Tema oscuro'}
      className="text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] transition-colors"
    >
      {dark ? '☀' : '☾'}
    </button>
  );
}
