'use client';

/** Botón del header que abre la paleta de comandos (la paleta escucha el
 *  evento `cima:cmdk`; el atajo cmd+K/ctrl+K funciona igual sin el botón). */
export function CmdKButton() {
  return (
    <button
      onClick={() => window.dispatchEvent(new Event('cima:cmdk'))}
      title="Paleta de comandos (cmd+K / ctrl+K)"
      aria-label="Abrir paleta de comandos"
      className="hidden md:inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] hover:bg-[rgb(var(--bg))] transition-colors font-mono"
    >
      ⌘K
    </button>
  );
}
