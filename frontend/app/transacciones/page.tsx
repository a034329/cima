import { fetchTransacciones } from '@/lib/api';
import { TablaTransacciones } from '@/components/TablaTransacciones';
import type { EstadoTransaccion, TransaccionOut } from '@/lib/types';
import Link from 'next/link';

export default async function TransaccionesPage({
  searchParams,
}: {
  searchParams: { estado?: string };
}) {
  let transacciones: TransaccionOut[] = [];
  let error: string | null = null;

  try {
    transacciones = await fetchTransacciones({
      estado: searchParams.estado as EstadoTransaccion | undefined,
      limit: 500,
    });
  } catch (e) {
    error = e instanceof Error ? e.message : 'Error desconocido';
    transacciones = [];
  }

  const counts = {
    todas: transacciones.length,
  };

  return (
    <div>
      <div className="mb-6 flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Transacciones</h2>
          <p className="text-sm text-[rgb(var(--muted))]">
            {counts.todas} {counts.todas === 1 ? 'operación' : 'operaciones'} en este filtro
          </p>
        </div>
        <Link
          href={`/informe/${new Date().getFullYear()}/${new Date().getMonth() + 1}`}
          className="text-xs text-brand-600 dark:text-brand-300 hover:underline"
        >
          cierre de mes →
        </Link>
      </div>

      {/* Filtros */}
      <div className="mb-4 flex gap-2">
        <FiltroEstado
          actual={searchParams.estado}
          valor={undefined}
          label="Todas"
        />
        <FiltroEstado
          actual={searchParams.estado}
          valor="confirmada"
          label="Confirmadas"
        />
        <FiltroEstado
          actual={searchParams.estado}
          valor="pendiente_confirmar"
          label="Pendientes"
        />
        <FiltroEstado
          actual={searchParams.estado}
          valor="descartada"
          label="Descartadas"
        />
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      <TablaTransacciones transacciones={transacciones} />
    </div>
  );
}

function FiltroEstado({
  actual,
  valor,
  label,
}: {
  actual: string | undefined;
  valor: string | undefined;
  label: string;
}) {
  const activo = actual === valor;
  const href = valor ? `/transacciones?estado=${valor}` : '/transacciones';
  return (
    <Link
      href={href}
      className={`px-3 py-1.5 text-sm rounded border ${
        activo
          ? 'bg-brand-600 text-white border-brand-600'
          : 'border-[rgb(var(--border))] hover:bg-[rgb(var(--bg))]'
      }`}
    >
      {label}
    </Link>
  );
}
