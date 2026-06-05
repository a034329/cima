"""Generación de la declaración IRPF completa estilo Cuádrate (Roadmap 1.9).

Cima invoca `generar_irpf.py` (vendorizado) como subprocess sobre los CSVs
ORIGINALES del usuario guardados por `storage_extractos`. Eso entrega TODO
lo que Cuádrate hace hoy: XLSX maestro + 4 informes (corporativas/dividendos/
opciones/fx) + sidecars JSON (corp_events, shorts pendientes, totals,
no_soportadas), con paridad total — sin reconstruir cada hoja desde la BD.

Output: un ZIP por ejercicio con todos los ficheros generados. El llamador
(router) lo streamea y borra el tempdir.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.adapters import cuadrate as cuadrate_adapter
from app.services import storage_extractos


# Timeout del subprocess: ejercicios típicos tardan < 30s con CSVs reales;
# 5 min cubre histórico denso (10+ años de DEGIRO con muchos splits).
_TIMEOUT_S = 300


class SinExtractosError(RuntimeError):
    """No hay CSVs guardados para el ejercicio solicitado."""


class GenerarIRPFError(RuntimeError):
    """`generar_irpf.py` falló o no produjo el XLSX maestro."""


@dataclass
class IRPFResultado:
    """Resultado de la generación: ZIP + metadatos para el router."""
    zip_path: Path                 # ZIP listo para descargar (en tempdir)
    work_dir: Path                 # tempdir con CSVs + outputs (a limpiar)
    ejercicio: int
    kinds_usados: list[str]        # ['degiro_transacciones', 'ibkr', …]
    ficheros_generados: list[str]  # nombres de los outputs incluidos en el ZIP
    stdout_tail: str               # últimas líneas del subprocess (para diagnóstico)


def _ruta_generar_irpf() -> Path:
    """Path absoluto al `generar_irpf.py` vendorizado."""
    return Path(cuadrate_adapter._CUADRATE_IRPF) / "generar_irpf.py"


def _es_output_cuadrate(nombre: str, ejercicio: int) -> bool:
    """¿El fichero del tempdir es un output de Cuádrate del ejercicio?

    Cuádrate genera (todos del ejercicio en el nombre):
      - cartera_valores_irpf_{ej}.xlsx     (XLSX maestro)
      - cartera_valores_irpf_{ej}.csv      (CSV plano, legacy)
      - cartera_valores_irpf_{ej}.<x>.json (sidecars: corp_events, totals,
        no_soportadas)
      - informe_corporativas_{ej}.txt
      - informe_dividendos_{ej}.txt
      - informe_opciones_{ej}.txt
      - informe_fx_{ej}.txt
      - shorts_pendientes_{ej}.json
      - informe_fiscal_{ej}.pdf            (PDF del pdf_generator de Cuádrate)
    """
    ej = str(ejercicio)
    if nombre.startswith("cartera_valores_irpf_") and ej in nombre:
        return True
    if nombre.startswith("informe_") and ej in nombre and nombre.endswith(".txt"):
        return True
    if nombre.startswith("informe_") and ej in nombre and nombre.endswith(".pdf"):
        return True
    if nombre.startswith("shorts_pendientes_") and ej in nombre:
        return True
    return False


def _generar_pdf_fiscal(work_dir: Path, ejercicio: int) -> Path | None:
    """Genera el PDF fiscal de Cuádrate dentro del tempdir.

    Reconstruye `fifo_results` desde el XLSX recién generado (motor lee XLSX
    indistintamente como input) y parsea los .txt de dividendos y opciones
    para alimentar al template. Devuelve la ruta del PDF o None si la
    generación falla (no es bloqueante: el ZIP sale igual sin el PDF y con
    aviso en stdout_tail).
    """
    xlsx = work_dir / f"cartera_valores_irpf_{ejercicio}.xlsx"
    if not xlsx.exists():
        return None

    motor = cuadrate_adapter.get_motor_fiscal()
    pdf_gen = cuadrate_adapter.get_pdf_generator()

    try:
        results = motor.calcular_fifo([str(xlsx)])
    except Exception:
        return None

    # Los .txt son opcionales — si no existen para este ejercicio (e.g. sólo
    # subiste el CSV de transacciones DEGIRO sin cuenta), el PDF se renderiza
    # con secciones vacías para dividendos/opciones.
    div_txt = work_dir / f"informe_dividendos_{ejercicio}.txt"
    opt_txt = work_dir / f"informe_opciones_{ejercicio}.txt"
    dividendos = None
    opciones = None
    try:
        if div_txt.exists():
            dividendos = pdf_gen.parse_dividendos_txt(str(div_txt))
        if opt_txt.exists():
            opciones = pdf_gen.parse_opciones_txt(str(opt_txt))
    except Exception:
        # Parse defensivo: si un .txt está corrupto, generamos PDF sin él.
        dividendos = dividendos or None
        opciones = opciones or None

    pdf_path = work_dir / f"informe_fiscal_{ejercicio}.pdf"
    try:
        pdf_gen.generate_fiscal_pdf(
            results, ejercicio=ejercicio, output_path=str(pdf_path),
            dividendos=dividendos, opciones=opciones,
            # Opcionales que aún no alimentamos desde Cima (Roadmap 1.9 extras):
            # compensacion (pérdidas 4Y), futuros (IBKR), fx_pl (IBKR forex),
            # complejos_investigaciones. El template las omite cuando son None.
            compensacion=None, futuros=None, fx_pl=None,
            complejos_investigaciones=None,
        )
    except Exception:
        return None
    return pdf_path if pdf_path.exists() else None


def _empaquetar_zip(work_dir: Path, ejercicio: int) -> tuple[Path, list[str]]:
    """Empaqueta los outputs del ejercicio en un ZIP dentro de `work_dir`.
    Devuelve (zip_path, nombres_incluidos)."""
    zip_path = work_dir / f"cartera_irpf_{ejercicio}.zip"
    incluidos: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(work_dir.iterdir()):
            if not f.is_file():
                continue
            if not _es_output_cuadrate(f.name, ejercicio):
                continue
            z.write(f, arcname=f.name)
            incluidos.append(f.name)
    return zip_path, incluidos


def generar_irpf_zip(
    db: Session, cartera_id: str, ejercicio: int,
) -> IRPFResultado:
    """Materializa los CSVs guardados, invoca `generar_irpf.py` y empaqueta
    los outputs en un ZIP. El llamador limpia `work_dir` cuando termina.

    Raises:
        SinExtractosError: no hay CSVs guardados para ese ejercicio.
        GenerarIRPFError:  el subprocess falló o no produjo el XLSX.
    """
    # 1) Materializar CSVs al tempdir con los nombres EXACTOS de Cuádrate.
    work_dir = Path(tempfile.mkdtemp(prefix=f"cima_irpf_{ejercicio}_"))
    materializados = storage_extractos.materializar_para_ejercicio(
        db, cartera_id, ejercicio, work_dir,
    )
    if not materializados:
        # No dejamos el tempdir colgando si no hay nada que hacer.
        _rm_tree_silent(work_dir)
        raise SinExtractosError(
            f"No hay extractos guardados para el ejercicio {ejercicio}. "
            f"Sube primero el CSV del broker en Importar extracto indicando "
            f"el ejercicio."
        )

    # 2) Invocar `generar_irpf.py` como subprocess (aislamiento total: no
    # contamina sys.argv ni los globals del proceso de Cima).
    script = _ruta_generar_irpf()
    if not script.is_file():
        _rm_tree_silent(work_dir)
        raise GenerarIRPFError(
            f"generar_irpf.py no encontrado en {script}. Ejecuta "
            f"`python scripts/sync_cuadrate.py` para refrescar el vendor."
        )

    cmd = [
        sys.executable, str(script),
        "--base-path", str(work_dir),
        "--ejercicio", str(ejercicio),
        "--no-interactive",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _rm_tree_silent(work_dir)
        raise GenerarIRPFError(
            f"generar_irpf.py excedió el timeout de {_TIMEOUT_S}s. "
            f"Si tu histórico es muy grande, abre un issue."
        )

    # `generar_irpf.py` puede exit 0 incluso si no hay XLSX (cuando los CSVs
    # están pero no contienen operaciones del ejercicio). Verificamos por la
    # presencia del fichero, no por el exit code.
    xlsx = work_dir / f"cartera_valores_irpf_{ejercicio}.xlsx"
    if proc.returncode != 0 and not xlsx.exists():
        tail_err = (proc.stderr or "").strip()[-1500:]
        tail_out = (proc.stdout or "").strip()[-1500:]
        _rm_tree_silent(work_dir)
        raise GenerarIRPFError(
            f"generar_irpf.py salió con código {proc.returncode}.\n"
            f"--- stderr ---\n{tail_err}\n--- stdout ---\n{tail_out}"
        )
    if not xlsx.exists():
        # Exit 0 pero sin XLSX → no había operaciones reconciliables.
        tail = (proc.stdout or "").strip()[-1500:]
        _rm_tree_silent(work_dir)
        raise GenerarIRPFError(
            f"generar_irpf.py terminó sin generar XLSX. Revisa que los CSVs "
            f"subidos contengan operaciones del ejercicio {ejercicio}.\n"
            f"--- últimas líneas del log ---\n{tail}"
        )

    # 3) Generar el PDF fiscal (best-effort — si falla, seguimos sin él).
    #    Reusa el XLSX recién generado como input al motor para reconstruir
    #    fifo_results y los .txt de informe para dividendos/opciones.
    _generar_pdf_fiscal(work_dir, ejercicio)

    # 4) Empaquetar XLSX + informes + sidecars + PDF en un ZIP.
    zip_path, incluidos = _empaquetar_zip(work_dir, ejercicio)
    return IRPFResultado(
        zip_path=zip_path,
        work_dir=work_dir,
        ejercicio=ejercicio,
        kinds_usados=sorted(materializados.keys()),
        ficheros_generados=incluidos,
        stdout_tail=(proc.stdout or "").strip()[-2000:],
    )


def _rm_tree_silent(path: Path) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def limpiar(resultado: IRPFResultado) -> None:
    """Limpieza diferida: borra el tempdir tras enviar el ZIP."""
    _rm_tree_silent(resultado.work_dir)
