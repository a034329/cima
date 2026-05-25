import Link from 'next/link';
import { fetchResumenFiscal, fetchResumenFiscalAcumulado, fmtEUR } from '@/lib/api';
import type { ResumenFiscal } from '@/lib/types';

export default async function ResumenFiscalPage({ params }: { params: { ano: string } }) {
  const esAcumulado = params.ano === 'acumulado';
  const ejercicio = esAcumulado ? null : parseInt(params.ano, 10);
  const ano = params.ano;

  let data: ResumenFiscal | null = null;
  let error: string | null = null;
  if (!esAcumulado && !Number.isFinite(ejercicio as number)) {
    error = `Ejercicio inválido: ${params.ano}`;
  } else {
    try {
      data = esAcumulado ? await fetchResumenFiscalAcumulado() : await fetchResumenFiscal(ejercicio as number);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  if (error) {
    return (
      <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-4">
        <p className="text-sm text-rose-700 dark:text-rose-300">{error}</p>
      </div>
    );
  }
  if (!data) return null;

  const c = data.compensacion;
  const etiqueta = esAcumulado ? 'acumulado' : ano;

  return (
    <div className="space-y-5">
      <p className="text-sm text-[rgb(var(--muted))]">
        Cuadro IRPF integrado del ejercicio {etiqueta} — base imponible del ahorro.
        Cada concepto enlaza a su detalle.
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Ganancias / pérdidas patrimoniales */}
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
          <h3 className="font-semibold mb-3">Ganancias y pérdidas patrimoniales</h3>
          <div className="space-y-1 text-sm">
            <Linea ano={ano} seg="acciones" label="Acciones / ETFs · 0326-0340" valor={data.gp_acciones} />
            {parseFloat(data.gp_derechos) !== 0 && (
              <Linea ano={ano} seg="acciones" label="Derechos de suscripción · 0341-0355" valor={data.gp_derechos} />
            )}
            {parseFloat(data.gp_estructurados) !== 0 && (
              <Linea ano={ano} seg="acciones" label="Derivados estructurados · 1624-1654" valor={data.gp_estructurados} />
            )}
            <Linea ano={ano} seg="forex" label="Forex realizado · 33.5.e" valor={data.forex_realized} />
            <Linea ano={ano} seg="opciones" label="Opciones · casilla 1626" valor={data.opciones_pl} />
            {parseFloat(data.gp_no_deducible_2m) > 0 && (
              <Row label="(−) No deducible regla 2M" valor={`(${fmtEUR(data.gp_no_deducible_2m, { maximumFractionDigits: 2 })})`} dim />
            )}
            <Divisor />
            <Row label="G/P que computa" valor={fmtEUR(c.gp_total, { maximumFractionDigits: 2 })} bold />
            {parseFloat(data.perdidas_afloradas) > 0 && (
              <div className="mt-3 rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-2.5">
                <div className="flex justify-between gap-4 text-amber-800 dark:text-amber-200 font-medium">
                  <span>Pérdidas afloradas a declarar</span>
                  <span className="font-mono tabular-nums">
                    −{fmtEUR(data.perdidas_afloradas, { maximumFractionDigits: 2 })}
                  </span>
                </div>
                <p className="text-[11px] text-amber-700 dark:text-amber-300 mt-1">
                  Pérdidas diferidas (regla 2M) que afloran este año al vender el lote recomprado.
                  Hacienda NO las aplica sola: súmalas tú a la casilla de G/P. Ya incluidas en el
                  «G/P que computa» de arriba.
                </p>
              </div>
            )}
          </div>
        </div>

        {/* RCM */}
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
          <h3 className="font-semibold mb-3">Rendimientos del capital mobiliario</h3>
          <div className="space-y-1 text-sm">
            <Linea ano={ano} seg="dividendos" label="Dividendos brutos · 0029" valor={data.dividendos_bruto} />
            {parseFloat(data.dividendos_ret_es) > 0 && (
              <Row label="(−) Retención pagadores ES" valor={`(${fmtEUR(data.dividendos_ret_es, { maximumFractionDigits: 2 })})`} dim />
            )}
            <Linea ano={ano} seg="intereses" label="Intereses crédito/cupón · 0023" valor={data.intereses_rcm} />
            <Linea ano={ano} seg="letras" label="Letras del Tesoro" valor={data.letras_rcm} />
            <Divisor />
            <Row label="RCM neto" valor={fmtEUR(c.rcm_neto, { maximumFractionDigits: 2 })} bold />
          </div>
        </div>
      </div>

      {/* Compensación cruzada + bolsas */}
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
        <h3 className="font-semibold mb-3">Compensación (cruzada 25 % + bolsas 4 años)</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-1 text-sm">
          <Row label="Cruce G/P → RCM (máx 25 %)" valor={fmtEUR(c.cruce_gp_a_rcm, { maximumFractionDigits: 2 })} dim={parseFloat(c.cruce_gp_a_rcm) === 0} />
          <Row label="Cruce RCM → G/P (máx 25 %)" valor={fmtEUR(c.cruce_rcm_a_gp, { maximumFractionDigits: 2 })} dim={parseFloat(c.cruce_rcm_a_gp) === 0} />
          <Row label="Aplicado de pérdidas anteriores" valor={fmtEUR(c.aplicadas_de_anteriores, { maximumFractionDigits: 2 })} dim={parseFloat(c.aplicadas_de_anteriores) === 0} />
          <Row label="Nuevo saldo negativo a arrastrar" valor={fmtEUR(c.nuevo_saldo_negativo, { maximumFractionDigits: 2 })} dim={parseFloat(c.nuevo_saldo_negativo) === 0} />
        </div>
      </div>

      {/* Bases a tributar */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <BaseBox label="Base ahorro G/P" valor={data.base_ahorro_gp} />
        <BaseBox label="Base ahorro RCM" valor={data.base_ahorro_rcm} />
        <BaseBox label="Base del ahorro TOTAL" valor={data.base_ahorro_total} destacado />
      </div>

      {/* Deducciones e informativos */}
      <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-4 text-sm space-y-1">
        <Row
          label="Deducción doble imposición CDI · casilla 0588"
          valor={fmtEUR(data.cdi_recuperable, { maximumFractionDigits: 2 })}
        />
        {parseFloat(data.intereses_debit) !== 0 && (
          <Row
            label="Interés de débito (informativo · no deducible)"
            valor={fmtEUR(data.intereses_debit, { maximumFractionDigits: 2 })}
            dim
          />
        )}
      </div>

      <div className="rounded-lg border border-dashed border-[rgb(var(--border))] p-4 flex items-center justify-between flex-wrap gap-3">
        <p className="text-xs text-[rgb(var(--muted))]">
          La cuota final la calcula tu declaración. Cima integra la base; el detalle de
          cada concepto está en sus pestañas. Cálculo: {data.fecha_calculo}.
        </p>
        <span className="text-sm px-3 py-1.5 rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))]">
          Generar declaración con Cuádrate (próx.)
        </span>
      </div>
    </div>
  );
}

function Linea({ ano, seg, label, valor }: { ano: string; seg: string; label: string; valor: string }) {
  const v = parseFloat(valor);
  return (
    <Link
      href={`/fiscal/${ano}/${seg}`}
      className="flex justify-between gap-4 hover:bg-[rgb(var(--bg))] -mx-1 px-1 rounded"
    >
      <span className="text-[rgb(var(--fg))] underline-offset-2 hover:underline">{label}</span>
      <span className={`font-mono tabular-nums ${v >= 0 ? '' : 'text-rose-700 dark:text-rose-400'}`}>
        {fmtEUR(valor, { maximumFractionDigits: 2 })}
      </span>
    </Link>
  );
}

function Row({ label, valor, bold, dim }: { label: string; valor: string; bold?: boolean; dim?: boolean }) {
  return (
    <div className={`flex justify-between gap-4 ${bold ? 'font-semibold' : ''} ${dim ? 'text-[rgb(var(--muted))]' : ''}`}>
      <span>{label}</span>
      <span className="font-mono tabular-nums">{valor}</span>
    </div>
  );
}

function Divisor() {
  return <div className="border-t border-[rgb(var(--border))] my-1" />;
}

function BaseBox({ label, valor, destacado }: { label: string; valor: string; destacado?: boolean }) {
  const v = parseFloat(valor);
  return (
    <div
      className={`rounded-lg border p-4 text-center ${
        destacado
          ? 'border-brand-400 bg-brand-50/40 dark:bg-brand-900/10'
          : 'border-[rgb(var(--border))] bg-[rgb(var(--card))]'
      }`}
    >
      <div className="text-[11px] uppercase tracking-wide text-[rgb(var(--muted))]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${v >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}>
        {fmtEUR(valor, { maximumFractionDigits: 2 })}
      </div>
    </div>
  );
}
