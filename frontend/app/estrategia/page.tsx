'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import {
  asignarBloque,
  autoclasificarCartera,
  crearBloque,
  editarBloque,
  eliminarBloque,
  fetchDistribucionBloques,
  fetchHueco,
  fetchPosicionesBloque,
  fetchRegimen,
  fmtEUR,
  fmtPct,
  guardarRegimen,
  sugerirBloque,
} from '@/lib/api';
import type {
  BloqueDist,
  CategoriaBase,
  DistribucionBloques,
  HuecoAsignacion,
  HuecoBloque,
  PosicionBloque,
  RegimenEstado,
  SugerenciaBloque,
} from '@/lib/types';

import { CAT_ASIGNABLES, CAT_COLOR, CAT_HEX, CAT_LABEL } from '@/lib/categorias';
import { RegimenPanel } from '@/components/RegimenPanel';

export default function EstrategiaPage() {
  const [dist, setDist] = useState<DistribucionBloques | null>(null);
  const [hueco, setHueco] = useState<HuecoAsignacion | null>(null);
  const [regimen, setRegimen] = useState<RegimenEstado | null>(null);
  const [posiciones, setPosiciones] = useState<PosicionBloque[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nuevoNombre, setNuevoNombre] = useState('');
  const [nuevaCat, setNuevaCat] = useState<CategoriaBase>('growth');
  // Sugerencias IA por ISIN: 'cargando' | error | la sugerencia.
  const [sugerencias, setSugerencias] = useState<
    Record<string, SugerenciaBloque | 'cargando' | { error: string }>
  >({});
  // Bloques vacíos que el usuario ha "activado" (clic en Disponibles) para configurarlos.
  const [activados, setActivados] = useState<Set<string>>(new Set());

  const cargar = useCallback(async () => {
    try {
      const [d, p, h, r] = await Promise.all([
        fetchDistribucionBloques(), fetchPosicionesBloque(), fetchHueco(), fetchRegimen(),
      ]);
      setDist(d);
      setPosiciones(p);
      setHueco(h);
      setRegimen(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    cargar();
  }, [cargar]);

  const onAsignar = async (isin: string, bloqueId: string) => {
    await asignarBloque(isin, bloqueId === 'sin_clasificar' ? null : bloqueId);
    await cargar();
  };

  const onGuardarRegimen = async (ind: RegimenEstado['indicadores']) => {
    setRegimen(await guardarRegimen(ind));   // recalibra los tramos de la guía de compra
  };

  const onSugerir = async (isin: string) => {
    setSugerencias((s) => ({ ...s, [isin]: 'cargando' }));
    try {
      const sug = await sugerirBloque(isin);
      setSugerencias((s) => ({ ...s, [isin]: sug }));
    } catch (e) {
      setSugerencias((s) => ({
        ...s,
        [isin]: { error: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  const descartarSugerencia = (isin: string) =>
    setSugerencias((s) => {
      const { [isin]: _omit, ...resto } = s;
      return resto;
    });

  const aplicarSugerencia = async (isin: string, bloqueId: string | null) => {
    await asignarBloque(isin, bloqueId);
    descartarSugerencia(isin);
    await cargar();
  };

  const LOTE = 8;   // empresas por batch: lo bastante pequeño para ver progreso
  const [autoProgreso, setAutoProgreso] = useState<{ hechas: number; total: number } | null>(null);
  const onAutoclasificar = async () => {
    // Las sin clasificar las conoce ya el frontend (posiciones con bloque_id null).
    const pendientes = (posiciones ?? [])
      .filter((p) => p.bloque_id == null)
      .map((p) => p.isin);
    if (pendientes.length === 0) return;
    setAutoProgreso({ hechas: 0, total: pendientes.length });
    setError(null);
    try {
      for (let i = 0; i < pendientes.length; i += LOTE) {
        const batch = pendientes.slice(i, i + LOTE);
        const sugs = await autoclasificarCartera({ isines: batch });
        setSugerencias((s) => {
          const next = { ...s };
          for (const sug of sugs) if (sug.isin) next[sug.isin] = sug;
          return next;
        });
        setAutoProgreso({ hechas: Math.min(i + LOTE, pendientes.length), total: pendientes.length });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAutoProgreso(null);
    }
  };

  const onCrear = async () => {
    if (!nuevoNombre.trim()) return;
    try {
      await crearBloque(nuevoNombre.trim(), nuevaCat);
      setNuevoNombre('');
      await cargar();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onEliminar = async (id: string) => {
    await eliminarBloque(id);
    await cargar();
  };

  // Bloques reales (excluye el saco sin_clasificar para selects y borrado)
  const bloquesReales = (dist?.bloques ?? []).filter((b) => b.id !== 'sin_clasificar');

  // Hueco de asignación (objetivo/actual/planeado/déficit) indexado por bloque.
  const huecoPorBloque: Record<string, HuecoBloque> = {};
  for (const h of hueco?.bloques ?? []) huecoPorBloque[h.bloque_id] = h;
  // Escala común de las barras: el mayor de objetivo/proyectado entre bloques.
  const escalaHueco = Math.max(
    0.0001,
    ...(hueco?.bloques ?? []).map((h) =>
      Math.max(parseFloat(h.objetivo_pct ?? '0'), parseFloat(h.proyectado_pct))),
  );

  // Un bloque está "vacío" (disponible) si no tiene nada: ni posiciones, ni valor,
  // ni objetivo, ni efectivo, ni compra planeada — y el usuario no lo ha activado.
  const esVacio = (b: BloqueDist) =>
    b.id !== 'sin_clasificar' &&
    b.n_posiciones === 0 &&
    parseFloat(b.valor_eur) === 0 &&
    (b.peso_objetivo == null || parseFloat(b.peso_objetivo) === 0) &&
    parseFloat(b.liquidez_asignada_eur) === 0 &&
    parseFloat(huecoPorBloque[b.id]?.planeado_eur ?? '0') === 0 &&
    !activados.has(b.id);

  const _bloques = dist?.bloques ?? [];
  const disponibles = _bloques.filter(esVacio);
  const activos = _bloques.filter((b) => !esVacio(b));
  const grupoEstrategia = activos.filter((b) => b.en_estrategia);
  const grupoFuera = activos.filter((b) => !b.en_estrategia);
  const activar = (id: string) => setActivados((s) => new Set(s).add(id));

  const renderCard = (b: BloqueDist) => {
    const peso = parseFloat(b.peso_actual);
    const h = huecoPorBloque[b.id];
    const w = (v: string) => `${Math.min(100, (parseFloat(v) / escalaHueco) * 100)}%`;
    const def = h?.deficit_eur != null ? parseFloat(h.deficit_eur) : null;
    return (
      <div key={b.id} className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-3">
        <div className="flex items-center justify-between gap-3 mb-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{b.nombre}</span>
            <span className={`text-xs px-2 py-0.5 rounded ${CAT_COLOR[b.categoria_base]}`}>
              {CAT_LABEL[b.categoria_base]}
            </span>
            <span className="text-xs text-[rgb(var(--muted))]">{b.n_posiciones} pos.</span>
            {b.cagr4_div_pct != null && (() => {
              const completa = parseFloat(b.cobertura_estimacion ?? '1') >= 0.999;
              return (
                <span
                  className={`text-xs px-2 py-0.5 rounded ${
                    completa
                      ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
                      : 'bg-[rgb(var(--border))]/50 text-[rgb(var(--muted))]'
                  }`}
                  title={completa
                    ? 'CAGR4+Div proyectado, ponderado por valor del bloque'
                    : `Solo ${b.n_con_estimacion}/${b.n_posiciones} posiciones tienen estimación — interpreta con cautela`}
                >
                  CAGR proy. {fmtPct(b.cagr4_div_pct)}
                  {!completa && ` · ${b.n_con_estimacion}/${b.n_posiciones}`}
                </span>
              );
            })()}
            {b.fuera_tolerancia && (
              <span className="text-xs px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                fuera de tolerancia
              </span>
            )}
            {b.id !== 'sin_clasificar' && (
              <button
                onClick={async () => {
                  await editarBloque(b.id, { en_estrategia: !b.en_estrategia });
                  await cargar();
                }}
                className="text-[10px] px-1.5 py-0.5 rounded border border-[rgb(var(--border))] text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))]"
                title={b.en_estrategia
                  ? 'Cuenta para el objetivo de IF. Clic para sacarlo.'
                  : 'Fuera de la estrategia IF. Clic para incluirlo.'}
              >
                {b.en_estrategia ? 'en IF' : 'fuera de IF'}
              </button>
            )}
            {!b.es_base && b.id !== 'sin_clasificar' && (
              <button
                onClick={() => onEliminar(b.id)}
                className="text-xs text-rose-600 dark:text-rose-400 hover:underline"
              >
                eliminar
              </button>
            )}
          </div>
          <div className="text-right text-sm">
            <span className="font-semibold">{fmtEUR(b.valor_eur)}</span>
            <span className="text-[rgb(var(--muted))] ml-2">{fmtPct(b.peso_actual)}</span>
            {b.peso_objetivo && (
              <span className="text-[rgb(var(--muted))] ml-1">/ obj {fmtPct(b.peso_objetivo)}</span>
            )}
            {b.rendimiento_pct && parseFloat(b.rendimiento_pct) > 0 && (
              <span className="ml-2 text-emerald-600 dark:text-emerald-400">
                rinde {fmtPct(b.rendimiento_pct, 2)}
              </span>
            )}
          </div>
        </div>
        {!h ? (
          <div className="relative h-2.5 bg-[rgb(var(--border))] rounded overflow-hidden">
            <div
              className={`h-full ${b.id === 'sin_clasificar' ? 'bg-rose-400' : 'bg-brand-500'}`}
              style={{ width: `${Math.min(peso * 100, 100)}%` }}
            />
          </div>
        ) : (
          <div className="relative h-2.5 bg-[rgb(var(--border))]/40 rounded overflow-hidden">
            <div className="absolute inset-y-0 left-0 flex">
              <div style={{ width: w(h.actual_pct), background: CAT_HEX[b.categoria_base] }} />
              <div style={{ width: w(h.planeado_pct), background: CAT_HEX[b.categoria_base], opacity: 0.4 }} />
            </div>
            {h.objetivo_pct && parseFloat(h.objetivo_pct) > 0 && (
              <div
                className="absolute inset-y-0 w-0.5 bg-[rgb(var(--fg))]"
                style={{ left: w(h.objetivo_pct) }}
                title={`Objetivo ${fmtPct(h.objetivo_pct)}`}
              />
            )}
          </div>
        )}
        {h && h.objetivo_pct != null && (
          <div className="mt-1 text-[11px] text-[rgb(var(--muted))] font-mono tabular-nums">
            planeado {parseFloat(h.planeado_pct) > 0 ? `+${fmtPct(h.planeado_pct)}` : '—'}
            {def != null && def > 0.5 && (
              <span className="text-emerald-600 dark:text-emerald-400">
                {' '}· faltan {fmtEUR(h.deficit_eur ?? '0')}
              </span>
            )}
            {def != null && def < -0.5 && <span>{' '}· sobreponderado</span>}
          </div>
        )}
        {/* Guía de compra: qué criterios buscar para llenar el déficit (a nivel de
            bloque, sin nombrar valores). El usuario busca el candidato en Seguimiento. */}
        {h && b.en_estrategia && def != null && def > 0.5 && h.criterios && (
          <div className="mt-2 rounded-md border border-dashed border-[rgb(var(--border))] bg-[rgb(var(--bg))] p-2 text-[11px] leading-snug">
            <div className="text-[rgb(var(--muted))]">
              <span className="font-medium text-[rgb(var(--fg))]">Criterios:</span> {h.criterios}
            </div>
            {regimen && def != null && (() => {
              const nMin = Math.max(1, Math.ceil(def / regimen.tramo_max));
              const nMax = Math.max(nMin, Math.ceil(def / regimen.tramo_min));
              const tramos = nMin === nMax ? `${nMin} tramo${nMin > 1 ? 's' : ''}` : `${nMin}–${nMax} tramos`;
              const corr = regimen.correccion;
              return (
                <>
                  <div className="mt-1 text-[rgb(var(--muted))]">
                    <span className="font-medium text-[rgb(var(--fg))]">Ritmo ({regimen.regimen}):</span>{' '}
                    ≈ {tramos} de {fmtEUR(regimen.tramo_min, { maximumFractionDigits: 0 })}–{fmtEUR(regimen.tramo_max, { maximumFractionDigits: 0 })} cada {regimen.espaciado}
                  </div>
                  {corr?.activa && corr.escalado_min != null && corr.escalado_max != null && (
                    <div className="mt-0.5 text-emerald-700 dark:text-emerald-400">
                      ↑ Ventana −14%: puedes escalar a {fmtEUR(corr.escalado_min, { maximumFractionDigits: 0 })}–{fmtEUR(corr.escalado_max, { maximumFractionDigits: 0 })} si el valor es <strong>coyuntural</strong> y su CAGR no ha empeorado.
                    </div>
                  )}
                </>
              );
            })()}
            <Link
              href={`/estrategia/seguimiento?bloque=${b.categoria_base}`}
              className="mt-1 inline-block font-medium text-brand-600 hover:underline dark:text-brand-400"
            >
              Buscar candidato →
            </Link>
          </div>
        )}
        {b.id !== 'sin_clasificar' && (
          <ObjetivoEditor
            bloque={b}
            onGuardar={async (objetivo) => {
              await editarBloque(b.id, { peso_objetivo: objetivo });
              await cargar();
            }}
          />
        )}
        {b.categoria_base === 'colchon' && (
          <ColchonEditor
            bloque={b}
            liquidezDisponible={dist?.liquidez_disponible_eur ?? '0'}
            onGuardar={async (efectivo, rendimiento) => {
              await editarBloque(b.id, {
                liquidez_asignada_eur: efectivo,
                rendimiento_pct: rendimiento,
              });
              await cargar();
            }}
          />
        )}
      </div>
    );
  };

  return (
    <div className="space-y-8">
      <p className="text-sm text-[rgb(var(--muted))]">
        Reparte tu cartera en bloques y fija un objetivo de peso para cada uno. La barra muestra
        lo actual (sólido) más lo que ya tienes planeado comprar (atenuado) frente al objetivo
        (marcador); el déficit te dice cuánto falta. El <strong>régimen macro</strong> de abajo
        calibra el tamaño y ritmo del tramo de compra.
      </p>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 dark:bg-rose-900/20 dark:border-rose-800 p-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      {regimen && (
        <RegimenPanel
          estado={regimen}
          onGuardar={onGuardarRegimen}
          onAutoFirmada={setRegimen}
        />
      )}

      {/* Distribución */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-lg font-semibold">Distribución por bloque</h3>
          {dist && (
            <span className="text-sm text-[rgb(var(--muted))]">
              {fmtEUR(dist.total_eur)} invertido
              {hueco && parseFloat(hueco.total_planeado_eur) > 0 && (
                <> · {fmtEUR(hueco.total_planeado_eur)} planeado · {fmtEUR(hueco.total_proyectado_eur)} proyectado</>
              )}
              {' '}· {fmtEUR(dist.liquidez_disponible_eur)} liquidez
            </span>
          )}
        </div>
        {/* Estrategia IF */}
        <div className="space-y-2">
          {grupoEstrategia.map(renderCard)}
        </div>

        {/* Fuera de la estrategia IF (Colchón, cripto a largo…) */}
        {grupoFuera.length > 0 && (
          <div className="mt-5">
            <h4 className="text-sm font-semibold mb-2">
              Fuera de la estrategia IF{' '}
              <span className="font-normal text-[rgb(var(--muted))]">· no cuentan para el progreso a la IF</span>
            </h4>
            <div className="space-y-2">{grupoFuera.map(renderCard)}</div>
          </div>
        )}

        {/* Disponibles: bloques vacíos, compactos. Un clic los activa. */}
        {disponibles.length > 0 && (
          <div className="mt-5">
            <h4 className="text-sm font-semibold mb-2">
              Disponibles{' '}
              <span className="font-normal text-[rgb(var(--muted))]">· fija un objetivo o asigna posiciones para activarlos</span>
            </h4>
            <div className="flex flex-wrap gap-2">
              {disponibles.map((b) => (
                <button
                  key={b.id}
                  onClick={() => activar(b.id)}
                  title="Activar para configurarlo"
                  className={`text-xs px-2 py-1 rounded border border-dashed border-[rgb(var(--border))] hover:border-brand-400 ${CAT_COLOR[b.categoria_base]}`}
                >
                  {b.nombre} +
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Alta de bloque */}
        {bloquesReales.length < 12 && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              value={nuevoNombre}
              onChange={(e) => setNuevoNombre(e.target.value)}
              placeholder="Nuevo bloque (ej. Compounders)"
              className="px-3 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            />
            <select
              value={nuevaCat}
              onChange={(e) => setNuevaCat(e.target.value as CategoriaBase)}
              className="px-2 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            >
              {CAT_ASIGNABLES.map((c) => (
                <option key={c} value={c}>{CAT_LABEL[c]}</option>
              ))}
            </select>
            <button
              onClick={onCrear}
              className="px-3 py-1.5 text-sm rounded bg-brand-600 text-white hover:bg-brand-700"
            >
              + Añadir bloque
            </button>
          </div>
        )}
      </section>

      {/* Asignación de posiciones */}
      <section>
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <h3 className="text-lg font-semibold">Asignar posiciones</h3>
          <button
            onClick={onAutoclasificar}
            disabled={autoProgreso !== null}
            className="px-3 py-1.5 text-sm rounded border border-brand-300 text-brand-700 dark:text-brand-300 hover:bg-brand-50 dark:hover:bg-brand-900/20 disabled:opacity-50"
            title="Sugiere un bloque para las posiciones sin clasificar por lotes (menos preciso, más barato). Las sugerencias van apareciendo por batch; revisas y aplicas."
          >
            {autoProgreso
              ? `Autoclasificando… ${autoProgreso.hechas}/${autoProgreso.total}`
              : 'Autoclasificar sin clasificar (IA)'}
          </button>
        </div>
        <div className="rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-[rgb(var(--muted))] border-b border-[rgb(var(--border))]">
              <tr className="text-left">
                <th className="py-2 px-3">Posición</th>
                <th className="px-3 text-right">Valor</th>
                <th className="px-3">Bloque</th>
                <th className="px-3">Sugerencia IA</th>
              </tr>
            </thead>
            <tbody>
              {posiciones?.map((p) => {
                const sug = sugerencias[p.isin];
                return (
                <tr key={p.isin} className="border-t border-[rgb(var(--border))]/40 align-top">
                  <td className="py-1.5 px-3">{p.nombre}</td>
                  <td className="px-3 text-right font-mono tabular-nums">{fmtEUR(p.valor_eur)}</td>
                  <td className="px-3">
                    <select
                      value={p.bloque_id ?? 'sin_clasificar'}
                      onChange={(e) => onAsignar(p.isin, e.target.value)}
                      className="px-2 py-1 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
                    >
                      <option value="sin_clasificar">— Sin clasificar —</option>
                      {bloquesReales.map((b) => (
                        <option key={b.id} value={b.id}>{b.nombre}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-1.5 max-w-sm">
                    {!sug && (
                      <button
                        onClick={() => onSugerir(p.isin)}
                        className="px-2 py-1 text-xs rounded border border-brand-300 text-brand-700 dark:text-brand-300 hover:bg-brand-50 dark:hover:bg-brand-900/20"
                      >
                        Sugerir IA
                      </button>
                    )}
                    {sug === 'cargando' && (
                      <span className="text-xs text-[rgb(var(--muted))]">Analizando…</span>
                    )}
                    {sug && sug !== 'cargando' && 'error' in sug && (
                      <div className="text-xs text-rose-600 dark:text-rose-400">
                        {sug.error}{' '}
                        <button onClick={() => onSugerir(p.isin)} className="underline">reintentar</button>
                      </div>
                    )}
                    {sug && sug !== 'cargando' && !('error' in sug) && (
                      <div className="text-xs space-y-1">
                        <div className="flex items-center gap-2">
                          <span className={`px-1.5 py-0.5 rounded ${CAT_COLOR[sug.categoria_base]}`}>
                            {CAT_LABEL[sug.categoria_base]}
                          </span>
                          <span className="text-[rgb(var(--muted))]">
                            confianza {Math.round(sug.confianza * 100)}%
                          </span>
                        </div>
                        <p className="text-[rgb(var(--muted))]">{sug.razonamiento}</p>
                        <div className="flex gap-2">
                          <button
                            onClick={() => aplicarSugerencia(p.isin, sug.bloque_id)}
                            disabled={!sug.bloque_id}
                            className="px-2 py-0.5 rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-40"
                            title={sug.bloque_id ? '' : 'No hay bloque de esa categoría en tu cartera'}
                          >
                            Aplicar
                          </button>
                          <button
                            onClick={() => descartarSugerencia(p.isin)}
                            className="px-2 py-0.5 rounded border border-[rgb(var(--border))]"
                          >
                            Descartar
                          </button>
                        </div>
                        <p className="text-[10px] text-[rgb(var(--muted))]">
                          vía {sug.proveedor} · decides tú
                        </p>
                      </div>
                    )}
                  </td>
                </tr>
                );
              })}
              {posiciones && posiciones.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-4 px-3 text-center text-[rgb(var(--muted))]">
                    No hay posiciones abiertas. Importa un extracto en Cartera.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {!posiciones && !error && <p className="text-sm text-[rgb(var(--muted))] mt-2">Cargando…</p>}
      </section>

      {/* Próximamente */}
      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Proximo titulo="Objetivos y plan" desc="Pesos objetivo por bloque, desviaciones ±5% y el plan firmado para cumplirlos." etiqueta="próximo" />
        <Proximo titulo="Asistente IA" desc="Régimen macro automático y los filtros de la doctrina (la clasificación de bloques por IA ya está disponible arriba)." etiqueta="Fase 3" />
      </section>
    </div>
  );
}

function ObjetivoEditor({
  bloque,
  onGuardar,
}: {
  bloque: BloqueDist;
  onGuardar: (objetivo: number | null) => Promise<void>;
}) {
  const [val, setVal] = useState(
    bloque.peso_objetivo ? (parseFloat(bloque.peso_objetivo) * 100).toString() : '',
  );
  const [guardando, setGuardando] = useState(false);

  const guardar = async () => {
    setGuardando(true);
    try {
      const o = val.trim() ? parseFloat(val.replace(',', '.')) / 100 : null;
      await onGuardar(o);
    } finally {
      setGuardando(false);
    }
  };

  return (
    <div className="mt-2 flex items-center gap-2 text-xs text-[rgb(var(--muted))]">
      <span>Objetivo</span>
      <input
        value={val}
        onChange={(e) => setVal(e.target.value)}
        placeholder="%"
        inputMode="decimal"
        className="px-2 py-0.5 text-xs rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-16"
      />
      <span>%</span>
      <button
        onClick={guardar}
        disabled={guardando}
        className="px-3 py-1 text-xs rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
      >
        Guardar
      </button>
    </div>
  );
}

function ColchonEditor({
  bloque,
  liquidezDisponible,
  onGuardar,
}: {
  bloque: BloqueDist;
  liquidezDisponible: string;
  onGuardar: (efectivo: number | null, rendimiento: number | null) => Promise<void>;
}) {
  const [efectivo, setEfectivo] = useState(
    parseFloat(bloque.liquidez_asignada_eur) > 0 ? bloque.liquidez_asignada_eur : '',
  );
  // rendimiento se guarda como fracción; se muestra/edita en %.
  const [rendPct, setRendPct] = useState(
    bloque.rendimiento_pct ? (parseFloat(bloque.rendimiento_pct) * 100).toString() : '',
  );
  const [guardando, setGuardando] = useState(false);

  const guardar = async () => {
    setGuardando(true);
    try {
      const ef = efectivo.trim() ? parseFloat(efectivo.replace(',', '.')) : null;
      const rp = rendPct.trim() ? parseFloat(rendPct.replace(',', '.')) / 100 : null;
      await onGuardar(ef, rp);
    } finally {
      setGuardando(false);
    }
  };

  return (
    <div className="mt-3 pt-3 border-t border-[rgb(var(--border))]/50 flex flex-wrap items-end gap-2">
      <label className="text-xs text-[rgb(var(--muted))]">
        Efectivo (letras / cuenta remunerada)
        <div className="flex items-center gap-1 mt-0.5">
          <input
            value={efectivo}
            onChange={(e) => setEfectivo(e.target.value)}
            placeholder="€"
            inputMode="decimal"
            className="px-2 py-1 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-28"
          />
          <button
            type="button"
            onClick={() => setEfectivo(liquidezDisponible)}
            className="text-[11px] text-brand-600 dark:text-brand-400 hover:underline whitespace-nowrap"
            title={`Usar la liquidez disponible (${fmtEUR(liquidezDisponible)})`}
          >
            usar disponible
          </button>
        </div>
      </label>
      <label className="text-xs text-[rgb(var(--muted))]">
        Rendimiento anual
        <div className="flex items-center gap-1 mt-0.5">
          <input
            value={rendPct}
            onChange={(e) => setRendPct(e.target.value)}
            placeholder="%"
            inputMode="decimal"
            className="px-2 py-1 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))] w-20"
          />
          <span className="text-xs text-[rgb(var(--muted))]">%</span>
        </div>
      </label>
      <button
        onClick={guardar}
        disabled={guardando}
        className="px-3 py-1 text-sm rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
      >
        Guardar
      </button>
    </div>
  );
}

function Proximo({ titulo, desc, etiqueta }: { titulo: string; desc: string; etiqueta: string }) {
  return (
    <div className="rounded-lg border border-dashed border-[rgb(var(--border))] bg-[rgb(var(--card))] p-4">
      <div className="flex items-center justify-between">
        <h4 className="font-semibold">{titulo}</h4>
        <span className="text-[11px] uppercase tracking-wide px-2 py-0.5 rounded bg-[rgb(var(--bg))] text-[rgb(var(--muted))]">
          {etiqueta}
        </span>
      </div>
      <p className="text-sm text-[rgb(var(--muted))] mt-2">{desc}</p>
    </div>
  );
}
