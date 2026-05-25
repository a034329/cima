import { Fragment } from 'react';
import { fetchOpciones, fetchOpcionesAcumulado, fmtEUR } from '@/lib/api';
import type { ContratoOpcion, OpcionesResumen } from '@/lib/types';

export default async function OpcionesPage({
  params,
}: {
  params: { ano: string };
}) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);

  let data: OpcionesResumen | null = null;
  let error: string | null = null;

  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado
        ? await fetchOpcionesAcumulado()
        : await fetchOpciones(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div>
      <p className="text-sm text-[rgb(var(--muted))] mb-4">
        Casilla 1626 (otros elementos patrimoniales) · DGT V2172-21 · clasificación por contrato
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4 mb-4">
          <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
        </div>
      )}

      {data && data.n_opciones === 0 && (
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-8 text-center">
          <p className="text-[rgb(var(--muted))]">
            {esAcumulado
              ? 'No hay operaciones de opciones en BD.'
              : `Sin operaciones de opciones en ${ejercicio}.`}
          </p>
          <p className="text-xs text-[rgb(var(--muted))] mt-2">
            Importa un extracto de DEGIRO (Transacciones + Cuenta) o IBKR con operaciones de opciones.
          </p>
        </div>
      )}

      {data && data.n_opciones > 0 && <OpcionesContenido d={data} esAcumulado={esAcumulado} />}
    </div>
  );
}

function OpcionesContenido({ d, esAcumulado }: { d: OpcionesResumen; esAcumulado: boolean }) {
  const plNeto = parseFloat(d.pl_neto);
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card
          label="P&L neto (casilla 1626)"
          value={fmtEUR(d.pl_neto, { maximumFractionDigits: 0 })}
          tono={plNeto >= 0 ? 'ok' : 'err'}
          subtext={`${d.n_contratos} contratos · ${d.n_opciones} ops`}
        />
        <Card
          label="Primas cobradas"
          value={fmtEUR(d.primas_cobradas, { maximumFractionDigits: 0 })}
          tono="ok"
          subtext={`${d.n_expiradas} expiradas`}
        />
        <Card
          label="Primas pagadas"
          value={fmtEUR(d.primas_pagadas, { maximumFractionDigits: 0 })}
          tono="muted"
          subtext="Compras (buy-to-close / long)"
        />
        <Card
          label="Ejercidas → acciones"
          value={fmtEUR(d.ejercidas_prima_integrar, { maximumFractionDigits: 0 })}
          tono="muted"
          subtext="Prima integra en coste subyacente (no 1626)"
        />
      </div>

      {/* Diferidas */}
      {(parseFloat(d.short_abiertas_prima) !== 0 || parseFloat(d.long_abiertas_coste) !== 0) && (
        <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-4 text-sm">
          <p className="font-semibold text-amber-700 dark:text-amber-300 mb-2">
            Diferido al año de cierre (no entra en este ejercicio)
          </p>
          <div className="grid grid-cols-2 gap-4">
            <div className="flex justify-between">
              <span>Short abiertas al 31/12 (prima diferida)</span>
              <span className="font-mono">{fmtEUR(d.short_abiertas_prima)}</span>
            </div>
            <div className="flex justify-between">
              <span>Long abiertas (coste diferido)</span>
              <span className="font-mono">{fmtEUR(d.long_abiertas_coste)}</span>
            </div>
          </div>
          <p className="text-xs text-amber-700 dark:text-amber-400 mt-2 italic">
            DGT V2172-21 / Art. 14.1.c LIRPF: la alteración patrimonial se imputa cuando la opción
            se cierra, expira o ejerce — no mientras sigue abierta.
          </p>
        </div>
      )}

      <ContratosTable contratos={d.contratos} />

      <p className="text-xs text-[rgb(var(--muted))]">
        {esAcumulado
          ? 'Todas las opciones en BD.'
          : 'Filtrado por año del trade (aproximación al criterio año-a-año de Cuádrate; el diferimiento fino entre años es un refinamiento futuro).'}{' '}
        Calculado por calcular_resumen_opciones de Cuádrate. Cálculo: {d.fecha_calculo}.
      </p>
    </div>
  );
}

// Grupo fiscal (espeja el Excel de Cuádrate: 3 bloques).
function grupoDe(c: ContratoOpcion): 'declarable' | 'opc' | 'diferida' {
  if (c.clasificacion === 'ejercida' || c.clasificacion === 'mixta') return 'opc';
  if (
    c.clasificacion === 'long_abierta' ||
    c.clasificacion === 'short_abierta' ||
    c.clasificacion === 'roll_abierta'
  )
    return 'diferida';
  return 'declarable'; // normal: cerrada/expirada
}

function estadoLabel(c: ContratoOpcion): string {
  switch (c.clasificacion) {
    case 'mixta':
      return 'MIXTA (parte 1626 + parte OPC)';
    case 'ejercida':
      return 'EJERCIDA (prima en acciones)';
    case 'long_abierta':
      return 'ABIERTA long — diferida';
    case 'short_abierta':
      return 'ABIERTA short — diferida';
    case 'roll_abierta':
      return 'ABIERTA roll — parte en año';
    default:
      return c.expiradas > 0 ? 'EXPIRADA' : 'CERRADA';
  }
}

const GRUPOS: {
  id: 'declarable' | 'opc' | 'diferida';
  titulo: string;
  nota: string;
  fill: string;
}[] = [
  {
    id: 'declarable',
    titulo: 'DECLARABLES (cerradas/expiradas) → casilla 1626',
    nota: 'Suman al P&L que tributa este ejercicio.',
    fill: 'bg-emerald-50 dark:bg-emerald-900/20 text-emerald-800 dark:text-emerald-300',
  },
  {
    id: 'opc',
    titulo: 'EJERCIDAS / MIXTAS (informativo — prima ya en el subyacente)',
    nota: 'No van a 1626: la prima ajusta el coste/precio de las acciones.',
    fill: 'bg-blue-50 dark:bg-blue-900/20 text-blue-800 dark:text-blue-300',
  },
  {
    id: 'diferida',
    titulo: 'ABIERTAS al 31/12 (diferidas al año de extinción)',
    nota: 'DGT V2172-21 / Art. 14.1.c: se imputan cuando cierran/expiran/ejercen.',
    fill: 'bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300',
  },
];

function ContratosTable({ contratos }: { contratos: ContratoOpcion[] }) {
  const porGrupo = {
    declarable: [] as ContratoOpcion[],
    opc: [] as ContratoOpcion[],
    diferida: [] as ContratoOpcion[],
  };
  for (const c of contratos) porGrupo[grupoDe(c)].push(c);
  for (const g of Object.values(porGrupo)) {
    g.sort((a, b) =>
      a.subyacente === b.subyacente
        ? a.vencimiento.localeCompare(b.vencimiento)
        : a.subyacente.localeCompare(b.subyacente),
    );
  }
  const sub = (arr: ContratoOpcion[]) =>
    arr.reduce((acc, c) => acc + parseFloat(c.pl_neto), 0);

  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4 overflow-x-auto">
      <h3 className="font-semibold mb-3">Contratos ({contratos.length})</h3>
      <table className="w-full text-xs">
        <thead className="text-[rgb(var(--muted))]">
          <tr className="text-left border-b border-[rgb(var(--border))]">
            <th className="py-2 pr-2">Subyacente</th>
            <th className="pr-2">Tipo</th>
            <th className="pr-2 text-right">Strike</th>
            <th className="pr-2">Vencim.</th>
            <th className="pr-2">Estado</th>
            <th className="pr-2 text-right">P. cobradas</th>
            <th className="pr-2 text-right">P. pagadas</th>
            <th className="pr-2 text-right">P&L neto</th>
            <th className="pr-2">Broker</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {GRUPOS.map((g) => {
            const filas = porGrupo[g.id];
            if (filas.length === 0) return null;
            const subtotal = sub(filas);
            return (
              <Fragment key={g.id}>
                <tr>
                  <td colSpan={9} className={`px-2 py-1.5 font-sans font-semibold text-[11px] ${g.fill}`}>
                    {g.titulo}
                    <span className="font-normal opacity-80"> — {g.nota}</span>
                  </td>
                </tr>
                {filas.map((c, i) => {
                  const pl = parseFloat(c.pl_neto);
                  return (
                    <tr key={`${g.id}-${i}`} className="border-t border-[rgb(var(--border))]/30">
                      <td className="py-1 pr-2">{c.subyacente}</td>
                      <td className="pr-2">{c.tipo_op === 'C' ? 'CALL' : c.tipo_op === 'P' ? 'PUT' : c.tipo_op}</td>
                      <td className="pr-2 text-right">{c.strike}</td>
                      <td className="pr-2">{c.vencimiento}</td>
                      <td className="pr-2 text-[rgb(var(--muted))]">{estadoLabel(c)}</td>
                      <td className="pr-2 text-right">{fmtEUR(c.primas_cobradas, { maximumFractionDigits: 2 })}</td>
                      <td className="pr-2 text-right">{fmtEUR(c.primas_pagadas, { maximumFractionDigits: 2 })}</td>
                      <td className={`pr-2 text-right font-semibold ${pl >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}>
                        {fmtEUR(c.pl_neto, { maximumFractionDigits: 2 })}
                      </td>
                      <td className="pr-2 text-[rgb(var(--muted))]">{c.brokers}</td>
                    </tr>
                  );
                })}
                <tr className="border-t border-[rgb(var(--border))]">
                  <td colSpan={7} className="pr-2 py-1 text-right font-sans text-[rgb(var(--muted))]">
                    {g.id === 'declarable'
                      ? 'TOTAL P&L declarables (casilla 1626)'
                      : g.id === 'opc'
                        ? 'Subtotal ejercidas/mixtas (informativo)'
                        : 'Subtotal diferidas (informativo)'}
                  </td>
                  <td className={`pr-2 py-1 text-right font-bold ${subtotal >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}>
                    {fmtEUR(subtotal.toString(), { maximumFractionDigits: 2 })}
                  </td>
                  <td />
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Card({
  label,
  value,
  tono,
  subtext,
}: {
  label: string;
  value: string;
  tono: 'ok' | 'warn' | 'err' | 'muted';
  subtext?: string;
}) {
  const tonoCss: Record<typeof tono, string> = {
    ok: 'text-emerald-700 dark:text-emerald-400',
    warn: 'text-amber-700 dark:text-amber-400',
    err: 'text-rose-700 dark:text-rose-400',
    muted: 'text-[rgb(var(--muted))]',
  };
  return (
    <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">
        {label}
      </div>
      <div className={`text-2xl font-semibold mt-1 ${tonoCss[tono]}`}>{value}</div>
      {subtext && <div className="text-xs text-[rgb(var(--muted))] mt-1">{subtext}</div>}
    </div>
  );
}
