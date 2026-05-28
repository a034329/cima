'use client';

import { useState } from 'react';
import { ChatAsesor } from '@/components/ChatAsesor';

/** Burbuja de chat global (esquina inferior derecha). Disponible en toda la app:
 *  permite consultar al asesor sin salir de la pestaña en la que estás. */
export function ChatWidget() {
  const [abierto, setAbierto] = useState(false);
  // Se monta al abrir por primera vez y NO se desmonta al cerrar: solo se oculta
  // con CSS, para no perder la conversación ni una respuesta en vuelo.
  const [montado, setMontado] = useState(false);

  const abrir = () => { setMontado(true); setAbierto((v) => !v); };

  return (
    <>
      {montado && (
        <div className={`fixed bottom-20 right-4 sm:right-6 z-50 flex-col
          w-[calc(100vw-2rem)] sm:w-[400px] h-[70vh] sm:h-[560px] max-h-[calc(100vh-7rem)]
          rounded-xl border border-[rgb(var(--border))] bg-[rgb(var(--bg))] shadow-2xl
          ${abierto ? 'flex' : 'hidden'}`}>
          <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-[rgb(var(--border))]">
            <div>
              <div className="text-sm font-semibold">Asesor</div>
              <div className="text-[11px] text-[rgb(var(--muted))]">Tu cartera y estrategia en contexto</div>
            </div>
            <button onClick={() => setAbierto(false)}
              aria-label="Cerrar chat"
              className="text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] text-xl leading-none px-1">
              ×
            </button>
          </div>
          <div className="flex-1 min-h-0 p-3">
            <ChatAsesor />
          </div>
        </div>
      )}

      <button
        onClick={abrir}
        aria-label={abierto ? 'Cerrar asesor' : 'Abrir asesor'}
        className="fixed bottom-4 right-4 sm:right-6 z-50 h-14 w-14 rounded-full bg-brand-600 text-white
          shadow-lg hover:bg-brand-700 flex items-center justify-center transition-transform hover:scale-105"
      >
        {abierto ? (
          <span className="text-2xl leading-none">×</span>
        ) : (
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
          </svg>
        )}
      </button>
    </>
  );
}
