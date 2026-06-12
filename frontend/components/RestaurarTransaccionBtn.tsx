'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { restaurarTransaccion } from '@/lib/api';

/** Botón "restaurar" de la papelera: descartada → confirmada (+rebuild FIFO). */
export function RestaurarTransaccionBtn({ txId }: { txId: string }) {
  const router = useRouter();
  const [estado, setEstado] = useState<'idle' | 'enviando' | 'error'>('idle');

  const onClick = async () => {
    setEstado('enviando');
    try {
      await restaurarTransaccion(txId);
      router.refresh();
    } catch {
      setEstado('error');
    }
  };

  return (
    <button
      onClick={onClick}
      disabled={estado === 'enviando'}
      title="Devolver a confirmadas y recalcular la posición"
      className="text-xs px-1.5 py-0.5 rounded border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))] disabled:opacity-50"
    >
      {estado === 'enviando' ? '…' : estado === 'error' ? 'error — reintentar' : 'restaurar'}
    </button>
  );
}
