'use client';

import { useEffect, useState } from 'react';
import { fetchSaludDatos } from '@/lib/api';
import type { SaludDatos } from '@/lib/types';

// Umbrales de frescura del precio: verde < 6h (TTL de la caché), ámbar < 48h,
// rojo a partir de ahí (probablemente nadie ha pulsado "refrescar" en días).
const H = 3600_000;

function edad(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Date.now() - t : null;
}

function fmtEdad(ms: number): string {
  if (ms < H) return `hace ${Math.max(1, Math.round(ms / 60_000))} min`;
  if (ms < 48 * H) return `hace ${Math.round(ms / H)} h`;
  return `hace ${Math.round(ms / (24 * H))} días`;
}

/** Punto de color + tooltip con la frescura de precios/FX/fundamentales e
 *  imports. El usuario sabe de un vistazo si decide con datos de hace una
 *  hora o de la semana pasada. */
export function SaludDatosBadge() {
  const [s, setS] = useState<SaludDatos | null>(null);

  useEffect(() => {
    let vivo = true;
    fetchSaludDatos()
      .then((d) => { if (vivo) setS(d); })
      .catch(() => { /* sin cartera aún o backend caído: no molestar */ });
    return () => { vivo = false; };
  }, []);

  if (!s) return null;
  const e = edad(s.precios_ts);
  const color =
    e == null ? 'bg-zinc-400'
      : e < 6 * H ? 'bg-emerald-500'
        : e < 48 * H ? 'bg-amber-500'
          : 'bg-rose-500';

  const linea = (label: string, iso: string | null) => {
    const ms = edad(iso);
    return `${label}: ${ms == null ? 'sin datos' : fmtEdad(ms)}`;
  };
  const tooltip = [
    linea('Precios', s.precios_ts),
    linea('Tipos de cambio', s.fx_ts),
    linea('Fundamentales', s.fundamentales_ts),
    s.ultimo_import_ts
      ? `Último import: ${fmtEdad(edad(s.ultimo_import_ts)!)} — ${s.ultimo_import_desc ?? ''}`
      : 'Último import: ninguno',
    s.ultima_transaccion ? `Última transacción: ${s.ultima_transaccion}` : null,
  ].filter(Boolean).join('\n');

  return (
    <span
      title={tooltip}
      aria-label="Frescura de los datos"
      className="inline-flex items-center gap-1.5 cursor-help text-[11px] text-[rgb(var(--muted))]"
    >
      <span className={`inline-block w-2 h-2 rounded-full ${color}`} />
      <span className="hidden md:inline">{e == null ? 'sin precios' : `datos ${fmtEdad(e)}`}</span>
    </span>
  );
}
