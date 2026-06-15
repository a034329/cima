'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { fetchSaludDatos, refrescarSaludDatos } from '@/lib/api';
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

/** Punto de color + frescura de precios/FX/fundamentales e imports. Al pulsarlo
 *  refresca precios + tipos de cambio desde el feed (sin tener que ir a
 *  Estimaciones). El tooltip detalla la antigüedad de cada cosa. */
export function SaludDatosBadge() {
  const [s, setS] = useState<SaludDatos | null>(null);
  const [cargando, setCargando] = useState(false);
  const router = useRouter();

  useEffect(() => {
    let vivo = true;
    fetchSaludDatos()
      .then((d) => { if (vivo) setS(d); })
      .catch(() => { /* sin cartera aún o backend caído: no molestar */ });
    return () => { vivo = false; };
  }, []);

  const refrescar = async () => {
    if (cargando) return;
    setCargando(true);
    try {
      setS(await refrescarSaludDatos());
      // Repinta las vistas server-rendered con los precios nuevos.
      router.refresh();
    } catch {
      /* sin cartera o feed caído: dejamos el badge como estaba */
    } finally {
      setCargando(false);
    }
  };

  if (!s) return null;
  const e = edad(s.precios_ts);
  const color =
    cargando ? 'bg-brand-500 animate-pulse'
      : e == null ? 'bg-zinc-400'
        : e < 6 * H ? 'bg-emerald-500'
          : e < 48 * H ? 'bg-amber-500'
            : 'bg-rose-500';

  const linea = (label: string, iso: string | null) => {
    const ms = edad(iso);
    return `${label}: ${ms == null ? 'sin datos' : fmtEdad(ms)}`;
  };
  const tooltip = cargando
    ? 'Actualizando precios desde el feed…'
    : [
        'Pulsa para actualizar precios y tipos de cambio',
        '',
        linea('Precios', s.precios_ts),
        linea('Tipos de cambio', s.fx_ts),
        linea('Fundamentales', s.fundamentales_ts),
        s.ultimo_import_ts
          ? `Último import: ${fmtEdad(edad(s.ultimo_import_ts)!)} — ${s.ultimo_import_desc ?? ''}`
          : 'Último import: ninguno',
        s.ultima_transaccion ? `Última transacción: ${s.ultima_transaccion}` : null,
      ].filter((l) => l !== null).join('\n');

  return (
    <button
      type="button"
      onClick={refrescar}
      disabled={cargando}
      title={tooltip}
      aria-label="Actualizar precios desde el feed"
      className="inline-flex items-center gap-1.5 text-[11px] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] transition-colors disabled:opacity-70"
    >
      <span className={`inline-block w-2 h-2 rounded-full ${color}`} />
      <span className="hidden md:inline">
        {cargando ? 'actualizando…' : e == null ? 'sin precios' : `datos ${fmtEdad(e)}`}
      </span>
    </button>
  );
}
