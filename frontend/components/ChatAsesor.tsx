'use client';

import { useEffect, useRef, useState } from 'react';
import { AccionCard } from '@/components/AccionCard';
import {
  enviarMensajeAsesor,
  fetchHistorialAsesor,
  limpiarAsesor,
} from '@/lib/api';
import type { AccionPropuesta, MensajeAsesor } from '@/lib/types';

const SUGERENCIAS = [
  '¿Cómo voy hacia la IF?',
  '¿Cuáles son mis próximos pasos?',
  '¿Hay algo que debería vender o reforzar?',
  '¿Están bien mis estimaciones?',
];

/** Chat del asesor. Reutilizable en la página dedicada y en el widget flotante;
 *  el padre controla la altura (contenedor `flex flex-col h-full`). */
export function ChatAsesor({ subtitulo = true }: { subtitulo?: boolean }) {
  const [mensajes, setMensajes] = useState<MensajeAsesor[]>([]);
  const [acciones, setAcciones] = useState<AccionPropuesta[]>([]);
  const [input, setInput] = useState('');
  const [pensando, setPensando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const finRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchHistorialAsesor().then(setMensajes).catch(() => {});
  }, []);

  useEffect(() => {
    finRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [mensajes, pensando]);

  // Toggle 🌐 "buscar en internet": fuerza el modo `investigar` (web) en el
  // backend. La heurística de keywords no cubre todo (p.ej. "investiga X" /
  // "qué herramientas tienes"); con este flag el usuario es explícito y la
  // respuesta usa siempre WebSearch.
  const [forzarWeb, setForzarWeb] = useState(false);
  // AbortController de la petición en vuelo. Permite cancelar el "pensando..."
  // cuando el backend tarda demasiado (sobre todo en modo web, hasta 3 min).
  const abortRef = useRef<AbortController | null>(null);

  const cancelar = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPensando(false);
  };

  const enviar = async (texto: string, porVoz = false) => {
    const t = texto.trim();
    if (!t || pensando) return;
    setInput('');
    setError(null);
    setMensajes((m) => [...m, { rol: 'user', contenido: t, created_at: new Date().toISOString() }]);
    setAcciones([]);
    setPensando(true);
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const r = await enviarMensajeAsesor(t, porVoz, forzarWeb, ac.signal);
      setMensajes((m) => [...m, r.mensaje]);
      setAcciones(r.acciones);
    } catch (e) {
      // Si el usuario canceló, no lo trato como error; lo silencio.
      if (e instanceof DOMException && e.name === 'AbortError') {
        // nada — `cancelar` ya cerró el estado
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      abortRef.current = null;
      setPensando(false);
    }
  };

  const limpiar = async () => {
    if (!window.confirm('¿Borrar toda la conversación?')) return;
    try { await limpiarAsesor(); setMensajes([]); setAcciones([]); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  // ── Voz (Web Speech API del navegador, gratis, sin backend) ──────────────
  const [escuchando, setEscuchando] = useState(false);
  const [lectura, setLectura] = useState(false);
  const recogRef = useRef<unknown>(null);
  const enviarRef = useRef(enviar);
  const ultimoLeidoRef = useRef<string | null>(null);
  // Si el último mensaje entró por VOZ, la próxima respuesta se lee aunque la
  // lectura general esté off (flujo "manos libres" desde el móvil).
  const proximaPorVozRef = useRef(false);
  useEffect(() => { enviarRef.current = enviar; });

  const puedeEscuchar = typeof window !== 'undefined' &&
    !!((window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown }).SpeechRecognition
       || (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition);
  const puedeHablar = typeof window !== 'undefined' && 'speechSynthesis' in window;

  useEffect(() => {
    if (!puedeEscuchar) return;
    const w = window as unknown as { SpeechRecognition?: new () => unknown; webkitSpeechRecognition?: new () => unknown };
    const SR = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!SR) return;
    type SR_Event = { results: ArrayLike<ArrayLike<{ transcript: string }>> };
    const r = new SR() as { lang: string; continuous: boolean; interimResults: boolean;
      onresult: (e: SR_Event) => void;
      onend: () => void; onerror: () => void; start: () => void; stop: () => void; abort?: () => void };
    r.lang = 'es-ES';
    r.continuous = false;
    r.interimResults = false;
    r.onresult = (e) => {
      const t = e.results[0][0].transcript;
      setEscuchando(false);
      if (t.trim()) {
        proximaPorVozRef.current = true;                 // marca: la próxima respuesta va por voz
        enviarRef.current(t, true);                       // dictar → auto-envía con flag por_voz
      }
    };
    r.onend = () => setEscuchando(false);
    r.onerror = () => setEscuchando(false);
    recogRef.current = r;
    return () => { try { r.abort?.(); } catch { /* ignore */ } };
  }, [puedeEscuchar]);

  const toggleEscuchar = () => {
    const r = recogRef.current as { start: () => void; stop: () => void } | null;
    if (!r) return;
    if (escuchando) { try { r.stop(); } catch { /* ignore */ } setEscuchando(false); }
    else { try { r.start(); setEscuchando(true); } catch { /* ya iniciado */ } }
  };

  // Limpia residuos de markdown/URLs por si la IA los cuela aunque le pidamos voz.
  const limpiarParaVoz = (s: string): string =>
    s
      .replace(/```[\s\S]*?```/g, '')                          // bloques de código
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')                 // [texto](url) → texto
      .replace(/https?:\/\/\S+/g, '')                          // URLs sueltas
      .replace(/`([^`]+)`/g, '$1')                             // `código`
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')                      // # títulos
      .replace(/(\*{1,3}|_{1,3})([^*_]+)\1/g, '$2')            // negrita/cursiva
      .replace(/^\s*[-•*]\s+/gm, '')                           // viñetas
      .replace(/^\s*\d+[.)]\s+/gm, '')                          // listas numeradas
      .replace(/[•‣◦⁃]/g, '')              // bullets unicode
      .replace(/\n{2,}/g, '. ')                                // párrafos → pausa
      .replace(/\s{2,}/g, ' ')
      .trim();

  const hablar = (texto: string): void => {
    if (!puedeHablar || !texto) return;
    const limpio = limpiarParaVoz(texto);
    if (!limpio) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(limpio);
      u.lang = 'es-ES';
      u.rate = 1.02;
      window.speechSynthesis.speak(u);
    } catch { /* ignore */ }
  };

  // Lee en voz la última respuesta cuando (a) la lectura general está ON, o
  // (b) la pregunta vino por voz (flujo manos libres → contesta hablando aunque
  // el toggle esté off; se "consume" tras leer una respuesta).
  useEffect(() => {
    if (!puedeHablar) return;
    const last = mensajes[mensajes.length - 1];
    if (!last || last.rol !== 'assistant' || !last.contenido) return;
    const sig = last.created_at + last.contenido.slice(0, 40);
    if (sig === ultimoLeidoRef.current) return;
    if (!lectura && !proximaPorVozRef.current) return;
    ultimoLeidoRef.current = sig;
    proximaPorVozRef.current = false;
    hablar(last.contenido);
  }, [mensajes, lectura, puedeHablar]);

  // Al apagar la lectura, corta lo que esté hablando.
  useEffect(() => {
    if (!lectura && puedeHablar) {
      try { window.speechSynthesis.cancel(); } catch { /* ignore */ }
    }
  }, [lectura, puedeHablar]);

  return (
    <div className="flex flex-col h-full min-h-0">
      {subtitulo && (puedeHablar || mensajes.length > 0) && (
        <div className="flex items-center justify-end gap-3 mb-2">
          {puedeHablar && (
            <button
              onClick={() => setLectura((v) => !v)}
              title={lectura ? 'Dejar de leer las respuestas' : 'Leer las respuestas en voz alta'}
              className={`text-xs inline-flex items-center gap-1 ${
                lectura ? 'text-brand-600 dark:text-brand-400'
                        : 'text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]'}`}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
                {lectura && <><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /></>}
              </svg>
              {lectura ? 'lectura: ON' : 'leer respuestas'}
            </button>
          )}
          {mensajes.length > 0 && (
            <button onClick={limpiar} className="text-xs text-[rgb(var(--muted))] hover:text-rose-600">
              limpiar
            </button>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-auto rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 space-y-3">
        {mensajes.length === 0 && !pensando && (
          <div className="text-sm text-[rgb(var(--muted))] space-y-3">
            <p>Pregúntame por tu cartera. Por ejemplo:</p>
            <div className="flex flex-wrap gap-2">
              {SUGERENCIAS.map((s) => (
                <button key={s} onClick={() => enviar(s)}
                  className="px-2.5 py-1 text-xs rounded-full border border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {mensajes.map((m, i) => (
          <div key={i} className={`flex ${m.rol === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
              m.rol === 'user'
                ? 'bg-brand-600 text-white'
                : 'bg-[rgb(var(--bg))] border border-[rgb(var(--border))]'
            }`}>
              {m.contenido}
            </div>
          </div>
        ))}
        {pensando && (
          <div className="flex justify-start items-center gap-2">
            <div className="rounded-lg px-3 py-2 text-sm text-[rgb(var(--muted))] bg-[rgb(var(--bg))] border border-[rgb(var(--border))] animate-pulse">
              pensando{forzarWeb ? ' (buscando en internet, hasta 3 min)' : ''}…
            </div>
            <button
              onClick={cancelar}
              className="text-xs text-rose-600 dark:text-rose-400 hover:underline"
              title="Cancelar la espera"
            >
              cancelar
            </button>
          </div>
        )}
        {acciones.length > 0 && !pensando && (
          <div className="space-y-2 pt-1">
            <div className="text-[11px] uppercase tracking-wider text-[rgb(var(--muted))]">
              Acciones propuestas (confirma para ejecutar)
            </div>
            {acciones.map((a, i) => <AccionCard key={i} a={a} onError={setError} />)}
          </div>
        )}
        <div ref={finRef} />
      </div>

      {error && <p className="mt-2 text-sm text-rose-600 dark:text-rose-400">{error}</p>}

      <div className="mt-3 flex items-end gap-2">
        <button
          onClick={() => setForzarWeb((v) => !v)}
          disabled={pensando}
          title={forzarWeb
            ? 'Buscar en internet: ON (la próxima pregunta usa WebSearch)'
            : 'Activar búsqueda en internet para la próxima pregunta'}
          aria-label={forzarWeb ? 'Búsqueda web activada' : 'Activar búsqueda web'}
          aria-pressed={forzarWeb}
          className={`h-[42px] w-[42px] rounded inline-flex items-center justify-center border transition-colors disabled:opacity-50 ${
            forzarWeb
              ? 'bg-emerald-600 text-white border-emerald-700'
              : 'border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]'
          }`}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <circle cx="12" cy="12" r="10" />
            <line x1="2" y1="12" x2="22" y2="12" />
            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
          </svg>
        </button>
        {puedeEscuchar && (
          <button
            onClick={toggleEscuchar}
            disabled={pensando}
            title={escuchando ? 'Detener dictado' : 'Dictar por voz (es-ES)'}
            aria-label={escuchando ? 'Detener dictado' : 'Dictar por voz'}
            className={`h-[42px] w-[42px] rounded inline-flex items-center justify-center border transition-colors ${
              escuchando
                ? 'bg-rose-600 text-white border-rose-700 animate-pulse'
                : 'border-[rgb(var(--border))] bg-[rgb(var(--bg))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]'}
              disabled:opacity-50`}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M12 1a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          </button>
        )}
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar(input); } }}
          placeholder={escuchando ? 'Escuchando… habla' : 'Escribe tu pregunta… (Enter envía)'}
          rows={2}
          className="flex-1 px-3 py-2 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] resize-none"
        />
        <button
          onClick={() => enviar(input)}
          disabled={pensando || !input.trim()}
          className="px-4 py-2 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
        >
          Enviar
        </button>
      </div>
      <p className="mt-1 text-[11px] text-[rgb(var(--muted))]">
        Orientativo. La decisión final es tuya.
        {forzarWeb && (
          <span className="ml-2 text-emerald-600 dark:text-emerald-400">
            · Búsqueda en internet activada (la próxima respuesta puede tardar minutos)
          </span>
        )}
      </p>
    </div>
  );
}
