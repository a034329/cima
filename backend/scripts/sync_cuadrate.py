#!/usr/bin/env python3
"""Sincroniza el motor fiscal de Cuádrate vendorizado dentro de Cima.

Cima NO depende del repo de Cuádrate en runtime: lleva una COPIA del motor en
`backend/vendor/cuadrate/` (commiteada → disponible en el contenedor de
producción). El motor lo desarrolla el agente de Cuádrate; el agente de Cima
ejecuta este script para traer la última versión.

Se respeta la estructura de carpetas que el motor ASUME (BASE_DIR/siblings):
  vendor/cuadrate/                 (= equivalente a /app/720)
    casillas_irpf.json             (generar_irpf lo busca en el padre del motor)
    irpf/                          (= /app/720/irpf — va en sys.path)
      motor_fiscal.py  generar_irpf.py  compensacion_perdidas.py
      instrument_classifier.py
      derechos_clasificados.json  etf_isin_list.json
      stock_blacklist.json  socimi_es_isin_list.json

Uso:
  python scripts/sync_cuadrate.py            # copia desde el origen + manifiesto
  python scripts/sync_cuadrate.py --check    # ¿está el vendor desactualizado? (exit 1 si sí)
  python scripts/sync_cuadrate.py --source /app/720
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ficheros del motor que Cima importa + sus datos estáticos. NO se vendoriza
# perdidas_pendientes.json (dato de usuario; Cima gestiona pérdidas aparte).
IRPF_PY = [
    "motor_fiscal.py", "generar_irpf.py",
    "compensacion_perdidas.py", "instrument_classifier.py",
]
IRPF_DATA = [
    "derechos_clasificados.json", "etf_isin_list.json",
    "stock_blacklist.json", "socimi_es_isin_list.json",
    "ecb_fx_cache.json",   # tipos BCE históricos (necesarios para conversión FX)
]
PARENT_DATA = ["casillas_irpf.json"]   # generar_irpf lo busca un nivel arriba

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "cuadrate"
MANIFEST = VENDOR / "MANIFEST.json"
ORIGEN_DEFECTO = Path("/app/720")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(source: Path) -> list[tuple[Path, Path, str]]:
    """(origen, destino, clave_manifiesto) para cada fichero a vendorizar."""
    items: list[tuple[Path, Path, str]] = []
    for f in IRPF_PY + IRPF_DATA:
        items.append((source / "irpf" / f, VENDOR / "irpf" / f, f"irpf/{f}"))
    for f in PARENT_DATA:
        items.append((source / f, VENDOR / f, f))
    return items


def comprobar(source: Path) -> int:
    if not source.is_dir():
        print(f"[sync_cuadrate] Origen no encontrado: {source} — no se comprueba.")
        return 0
    man = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"files": {}}
    stale = [k for src, _dst, k in _plan(source)
             if src.exists() and man["files"].get(k) != _sha(src)]
    if stale:
        print(f"[sync_cuadrate] DESACTUALIZADO vs {source}: {', '.join(stale)}")
        print("  → ejecuta `python scripts/sync_cuadrate.py` para actualizar.")
        return 1
    print("[sync_cuadrate] Vendor al día.")
    return 0


def sincronizar(source: Path) -> int:
    if not source.is_dir():
        print(f"[sync_cuadrate] ERROR: origen no encontrado: {source}")
        return 2
    if VENDOR.exists():
        shutil.rmtree(VENDOR)              # regenerar limpio
    (VENDOR / "irpf").mkdir(parents=True)
    shas: dict[str, str] = {}
    for src, dst, key in _plan(source):
        if not src.exists():
            print(f"[sync_cuadrate] AVISO: falta en origen, se omite: {key}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        shas[key] = _sha(dst)
        print(f"[sync_cuadrate] copiado {key}")
    MANIFEST.write_text(json.dumps({
        "source": str(source),
        "copied_at": datetime.now(timezone.utc).isoformat(),
        "files": shas,
    }, indent=2, ensure_ascii=False) + "\n")
    print(f"[sync_cuadrate] manifiesto → {MANIFEST}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=ORIGEN_DEFECTO)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    return comprobar(args.source) if args.check else sincronizar(args.source)


if __name__ == "__main__":
    sys.exit(main())
