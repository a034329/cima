'use client';

import { useCallback, useEffect, useState } from 'react';
import { fetchOpcionesAbiertas, fetchPosiciones } from '@/lib/api';
import { onDatosActualizados } from '@/lib/refetch';
import type { OpcionAbierta, PosicionesResumen } from '@/lib/types';
import { PosicionesEnriquecidas } from '@/components/PosicionesEnriquecidas';
import { CriptoTable } from '@/components/CriptoTable';
import { OpcionesAbiertas } from '@/components/OpcionesAbiertas';

// Orquesta la página Posiciones: un solo fetch de /api/posiciones (pesado,
// calcula rotación) repartido en secciones por tipo de activo + opciones.
export function PosicionesVista() {
  const [data, setData] = useState<PosicionesResumen | null>(null);
  const [opciones, setOpciones] = useState<OpcionAbierta[]>([]);
  const [error, setError] = useState<string | null>(null);

  const cargar = useCallback(() => {
    fetchPosiciones()
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    fetchOpcionesAbiertas().then(setOpciones).catch(() => setOpciones([]));
  }, []);

  useEffect(() => {
    cargar();
    return onDatosActualizados(cargar);
  }, [cargar]);

  if (error) {
    return (
      <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4">
        <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
      </div>
    );
  }
  if (!data) return <p className="text-sm text-[rgb(var(--muted))]">Cargando posiciones…</p>;

  const cripto = data.posiciones.filter((p) => p.tipo_activo === 'CRYPTO');

  return (
    <div className="space-y-6">
      <PosicionesEnriquecidas data={data} onData={setData} />
      <CriptoTable posiciones={cripto} />
      <OpcionesAbiertas opciones={opciones} />
    </div>
  );
}
