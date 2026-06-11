"""Classifier de instrumentos: distingue ACCIONES de ETFs/IIC, derivados,
cripto, bonos, SOCIMI españolas y ETCs físicos.

Implicación fiscal:
  - Acciones (STOCK): casillas 0326-0340 (apartado F2 RentaWEB).
  - ETFs / IIC: 2224-2236 desde Renta 2025 (RD 249/2023 + Art. 75.3.j RIRPF);
    junto a acciones en 0326-0340 hasta Renta 2024.
  - Derivados estructurados (turbos, factor certificates): 1624-1654 clave 4.
  - Criptomonedas: 1800-1814.
  - Bonos individuales: 0030-0033 (RCM, Art. 25.2 LIRPF).
  - SOCIMI españolas (Ley 11/2009): 0324/0325 (apartado F2, subapartado
    específico de IIC/SOCIMI). Solo aplica a SOCIMI españolas; los REITs/SIIC
    extranjeros (Klepierre, Realty Income, Simon, OBDC) van como acciones
    cotizadas normales (no hay doctrina DGT que los reclasifique).
  - ETCs físicos (Exchange Traded Commodities colateralizados): casilla 0031
    como RCM por cesión a terceros (Art. 25.2 LIRPF) según DGT V0267-25.
    No son IIC sino notas de deuda respaldadas por commodity físico.
    Distinción crítica con ETFs de mineras: 'iShares Physical Gold' es ETC
    y va a 0031; 'iShares Gold Producers' / RING es ETF UCITS sobre mineras
    y va a 2224-2236.

Prioridad de fuentes (de mayor a menor fiabilidad):
  1. IBKR FII Type — si el broker es IBKR y el statement trae el campo Type
     del bloque "Financial Instrument Information" → 100% fiable.
  2. SOCIMI españolas whitelist — ISIN español en `socimi_es_isin_list.json` → SOCIMI.
  3. ETF whitelist — ISIN está en `etf_isin_list.json` → ETF.
  4. Stock blacklist — ISIN está en `stock_blacklist.json` (acciones con
     nombre engañoso tipo MSCI Inc., S&P Global Inc., Aperam SA…) → STOCK.
  5. Heurística estricta por nombre — ETF probable solo si combina patrón de
     gestora conocida (iShares, Vanguard, Amundi…) + ISIN europeo (IE/LU/DE/FR)
     + término de fondo/índice (UCITS, MSCI, FTSE…). Sin el término, la marca
     sola puede ser la ACCIÓN de la propia gestora (Amundi SA) → STOCK UNKNOWN.
  6. Default → STOCK (con flag UNKNOWN si la heurística era marginal, para
     que el usuario pueda re-marcar como ETF en el Excel).
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

InstrumentType = Literal['STOCK', 'ETF', 'DERIVATIVE', 'CRYPTO', 'BOND', 'SOCIMI', 'ETC', 'FUTURE']

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ETF_LIST_PATH = os.path.join(_BASE_DIR, 'etf_isin_list.json')
_BLACKLIST_PATH = os.path.join(_BASE_DIR, 'stock_blacklist.json')
_SOCIMI_LIST_PATH = os.path.join(_BASE_DIR, 'socimi_es_isin_list.json')
_ETC_LIST_PATH = os.path.join(_BASE_DIR, 'etc_isin_list.json')


# ── Heurística de nombre ─────────────────────────────────────────────────────

# Marcas de gestoras de ETFs UCITS más comunes. Si el nombre del activo
# empieza o contiene una de estas marcas → señal fuerte de ETF.
_ETF_GESTORA_MARCAS = (
    'iShares', 'ISHARES',
    'Vanguard', 'VANGUARD',
    'Amundi', 'AMUNDI',
    'Lyxor', 'LYXOR',
    'Xtrackers', 'XTRACKERS', 'Xtrackrs', 'X-trackers',
    'SPDR ', 'SPDR.',  # con espacio para evitar matchear acciones que empiezan SPDR-otra-cosa (raro pero por defensividad)
    'Invesco', 'INVESCO',
    'WisdomTree', 'WISDOMTREE',
    'VanEck', 'VANECK', 'Van Eck',
    'HSBC',  # HSBC tiene ETFs UCITS — pero también es banco; cuidado, exigir otras señales
    'BNP Paribas Easy', 'BNPP Easy',
    'UBS ETF', 'UBS (Lux) Fund',
    'Deka ', 'DEKA ',  # Deka MSCI (alemán)
    'L&G', 'LGIM',
    'Fidelity Index', 'Fidelity ETF',
    'Franklin', 'FRANKLIN',  # exigir otras señales (también gestora activa)
    'JPMorgan ETF', 'JP Morgan ETF',
    'Global X', 'GlobalX',
    'KraneShares', 'KRANESHARES',
    'iShares Core', 'iShares MSCI', 'iShares S&P', 'iShares Edge',
)

# Términos que aparecen en nombres de ETFs/IIC pero raramente en acciones.
# Solo considerar como señal si combinan con otra (gestora o ISIN europeo).
_ETF_TERMINOS_NEUTROS = (
    ' UCITS', ' UCIT', ' OEIC',
    'SICAV',
    ' INDEX FUND', ' INDEX ETF',
    ' MSCI ', ' FTSE ', ' STOXX ', ' EURO STOXX ',
    ' S&P 500 ', ' S&P500 ',  # con espacios para evitar S&P Global (la empresa)
    ' Nasdaq-100 ', ' NASDAQ-100 ', ' NASDAQ 100 ',  # con guion/espacios
    ' Russell ',
    ' Bloomberg Barclays', ' Bloomberg ',
    ' Solactive',
    ' DAX UCITS',
    ' Nikkei ',
    'CAC 40 UCITS', 'IBEX 35 UCITS',
)

# Códigos de país de domicilio fiscal típicos de ETFs UCITS europeos.
# (2 primeros caracteres del ISIN — country code ISO 3166-1 alpha-2)
_ETF_FRIENDLY_COUNTRIES = {'IE', 'LU', 'DE', 'FR', 'NL', 'JE', 'GG'}

# Compat: algunos checks usan prefijos de 4 chars (legacy).
_ETF_FRIENDLY_ISIN_PREFIXES = ('IE00', 'LU00', 'LU01', 'LU02', 'DE00', 'FR00', 'NL00', 'JE00', 'GG00')

# Señal adicional de fondo/índice exigida junto a la marca de gestora.
# Varias gestoras son a la vez empresas cotizadas (Amundi SA en Euronext
# París, HSBC Holdings, Franklin Resources…): "AMUNDI" + ISIN FR encajaría
# en la heurística marca+ISIN-europeo y la ACCIÓN de la gestora acabaría
# clasificada como ETF con confianza. Sin esta señal → STOCK + UNKNOWN.
_RE_ETF_SENAL_FONDO = re.compile(
    r'\b(UCITS|OEIC|SICAV|ETFS?|ETP|INDEX|MSCI|FTSE|STOXX|RUSSELL|SOLACTIVE'
    r'|NIKKEI|NASDAQ|TOPIX|DAX|TRACKER)\b'
    r'|S&P\s?-?500|IBEX\s?35|CAC\s?40',
    re.IGNORECASE,
)


# ── Heurística de derivados estructurados (DeGiro sin Asset Category) ────────

# Tokens que indican derivado apalancado / certificado estructurado.
# Se buscan como SUBSTRING en el nombre normalizado a mayúsculas.
_DERIVATIVE_NAME_TOKENS = (
    'FACTOR',          # SG ADOBE FACTOR (MULTI) LONG LEV 4
    'TURBO',           # Turbo Long / Turbo Short
    'MINI L',          # Mini Future Long
    'MINI S',          # Mini Future Short
    'LEVERAGE',
    'LONG LEV',        # SG ... LONG LEV 4
    'SHORT LEV',       # SG ... SHORT LEV 3
    'KO CERT',         # Knock-Out Certificate
    'KNOCK-OUT',
    'KNOCK OUT',
    'BONUS CERTIF',    # Bonus Certificate
    'DISCOUNT CERTIF', # Discount Certificate
    'EXPRESS CERTIF',  # Express Certificate
    'REVERSE CONV',    # Reverse Convertible
    'OPEN END',        # Open End Certificate / Tracker
    'WARRANT',         # warrant cotizado
    'CALL WARRANT',
    'PUT WARRANT',
    'CONST LEV',       # Constant Leverage
    'X-MARKETS',       # DB X-Markets (Deutsche Bank certificados)
)

# Prefijos de emisor de derivados estructurados — bancos que emiten estos
# productos a través de sus mesas de derivados, NO la acción de la empresa.
# Se comprueba como prefijo del nombre (con espacio o punto al final).
_DERIVATIVE_EMISOR_PREFIXES = (
    'SG ',          # Société Générale Issuance — derivados (no la acción SOC GEN)
    'BNP ',         # BNP Paribas Issuance (no la acción BNP PARIBAS SA)
    'BNPP ',
    'VONTOBEL ',
    'CITI ',
    'GS ',
    'MS ',          # Morgan Stanley B.V. (no la acción MORGAN STANLEY)
    'UBS ',         # UBS AG (no la acción UBS GROUP AG, que va sin "AG" en DeGiro)
    'COMMERZBANK ',
    'DB X-MARKETS',  # Deutsche Bank
    'ING ',
)

# Prefijos ISIN típicos de productos estructurados (alemanes principalmente).
# DE000 + emisor banco + nombre con tokens derivados → derivado estructurado.
_DERIVATIVE_ISIN_PREFIXES = ('DE000',)


# ── Heurística de ETCs físicos (DGT V0267-25 → casilla 0031) ────────────────
#
# ETC = Exchange Traded Commodity. Es una nota de deuda colateralizada
# físicamente por commodity (oro/plata/platino/paladio/cobre/petróleo).
# Doctrina: RCM por cesión a terceros (Art. 25.2.b LIRPF + DGT V0267-25).
#
# Distinción crítica con:
#   - ETF UCITS de mineras (RING, iShares Gold Producers): SÍ son IIC,
#     no tienen 'PHYSICAL' en el nombre — quedan fuera de la heurística.
#   - ETF UCITS de bonos vinculados a commodity: misma lógica, no encajan.
#
# Heurística conservadora: si no estamos seguros, dejar que caiga al
# clasificador ETF/STOCK normal. Un falso negativo (ETC clasificado como
# ETF) es menos grave que un falso positivo (ETF de mineras enviado a
# casilla 0031 incorrecta).

# Commodities físicas que se colateralizan en ETCs retail europeos.
_ETC_COMMODITIES = (
    'GOLD', 'SILVER', 'PLATINUM', 'PALLADIUM',
    'COPPER',
    # Bonus: variantes nombre. WisdomTree/iShares usan estos términos.
)

# Tokens que marcan ETC físico con alta probabilidad.
#   - 'PHYSICAL' + commodity → ETC (iShares/Invesco/WisdomTree pattern)
#   - sufijo ' ETC' en el nombre → explícito
_ETC_NAME_REQUIRES_PHYSICAL = True   # requiere "PHYSICAL" + commodity en el nombre


# ── Singletons cargados en lazy-load ─────────────────────────────────────────

_etf_whitelist: dict | None = None
_stock_blacklist: dict | None = None
_socimi_whitelist: dict | None = None
_etc_whitelist: dict | None = None


def _load_etf_whitelist() -> dict:
    global _etf_whitelist
    if _etf_whitelist is None:
        if os.path.exists(_ETF_LIST_PATH):
            with open(_ETF_LIST_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _etf_whitelist = data.get('etfs', {})
        else:
            _etf_whitelist = {}
    return _etf_whitelist


def _load_stock_blacklist() -> dict:
    global _stock_blacklist
    if _stock_blacklist is None:
        if os.path.exists(_BLACKLIST_PATH):
            with open(_BLACKLIST_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _stock_blacklist = data.get('stocks_engañosos', {})
        else:
            _stock_blacklist = {}
    return _stock_blacklist


def _load_socimi_whitelist() -> dict:
    global _socimi_whitelist
    if _socimi_whitelist is None:
        if os.path.exists(_SOCIMI_LIST_PATH):
            with open(_SOCIMI_LIST_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _socimi_whitelist = data.get('socimis', {})
        else:
            _socimi_whitelist = {}
    return _socimi_whitelist


def _load_etc_whitelist() -> dict:
    global _etc_whitelist
    if _etc_whitelist is None:
        if os.path.exists(_ETC_LIST_PATH):
            with open(_ETC_LIST_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _etc_whitelist = data.get('etcs', {})
        else:
            _etc_whitelist = {}
    return _etc_whitelist


def _is_etc_by_name_heuristic(name_norm: str) -> tuple[bool, str]:
    """Heurística por nombre: detecta ETC físico cuando combina "PHYSICAL" +
    nombre de commodity, o cuando termina explícitamente en " ETC".

    Conservadora: si no hay señal clara, devuelve (False, ''). El caller
    cae al classifier ETF/STOCK normal. Esto evita falsos positivos como
    iShares Gold Producers (ETF UCITS de mineras de oro, NO ETC).

    Returns:
        (is_etc, motivo_breve)
    """
    if not name_norm:
        return (False, '')
    name_up = name_norm.upper()

    # Sufijo explícito "ETC" ANCLADO al final del nombre (con espacio antes).
    # Patrón típico: "iShares Physical Gold ETC", "Invesco Physical Gold ETC".
    # NO usar '\bETC\b' en cualquier posición: empresas/productos con "ETC"
    # como palabra intermedia (p.ej. emisores tipo "ETC Group ...") acabarían
    # en la casilla 0031 incorrecta. Los ETC con sufijo de clase tras "ETC"
    # ("... ETC USD Acc") se cubren por la señal PHYSICAL+commodity de abajo.
    if re.search(r'\sETC$', name_up):
        return (True, 'sufijo "ETC" al final del nombre')

    # PHYSICAL + commodity colateralizable.
    if 'PHYSICAL' in name_up:
        for commodity in _ETC_COMMODITIES:
            if commodity in name_up:
                return (True, f'"PHYSICAL" + "{commodity}" en nombre')

    return (False, '')


# ── API pública ──────────────────────────────────────────────────────────────

def classify_isin(
    isin: str,
    name: str = '',
    broker: str = '',
    ibkr_type: str | None = None,
) -> tuple[InstrumentType, str, bool]:
    """Clasifica un instrumento como STOCK o ETF.

    Args:
        isin: ISIN del instrumento (12 caracteres alfanuméricos).
        name: Nombre del activo (denominación canónica). Opcional.
        broker: 'IBKR', 'DEGIRO' u otro. Opcional.
        ibkr_type: si el broker es IBKR y el FII trae el campo Type
            ('COMMON', 'ETF', 'PREFERRED', 'RIGHT'). Opcional.

    Returns:
        (instrument_type, reason, is_unknown)
        - instrument_type: 'STOCK' o 'ETF'.
        - reason: explicación breve de la decisión (para logs / banners).
        - is_unknown: True si la decisión es marginal (caso por defecto STOCK
          cuando la heurística no ha podido clasificar con seguridad). Cuando
          es True, el output debe ofrecer al usuario re-marcar como ETF.
    """
    isin = (isin or '').strip().upper()
    name_norm = (name or '').strip()
    broker_norm = (broker or '').strip().upper()

    # 1. IBKR FII Type — fuente más fiable. Si IBKR clasifica como COMMON
    # pero el ISIN está en la lista de SOCIMI españolas, prevalece SOCIMI
    # (porque IBKR no distingue SOCIMI de acción común — la AEAT sí).
    # Análogamente, los ETCs son un caso ciego para IBKR: el Activity
    # Statement clasifica los Exchange Traded Commodities como 'ETF' (a
    # veces 'COMMON' según mercado). Aplicamos primer filtrado IBKR y
    # solo después chequeamos whitelist/heurística ETC con la pista del
    # FII Type para evitar falsos positivos.
    if broker_norm == 'IBKR' and ibkr_type:
        t = ibkr_type.strip().upper()
        if t == 'ETF':
            # IBKR dice ETF → puede ser ETF UCITS o ETC (IBKR no diferencia).
            # Primero whitelist ETC; si no está, heurística PHYSICAL+commodity.
            etc_list = _load_etc_whitelist()
            if isin in etc_list:
                entry = etc_list[isin]
                return ('ETC',
                        f"IBKR Type='ETF' + ISIN en whitelist ETC "
                        f"({entry.get('ticker', '?')})",
                        False)
            is_etc_h, motivo_h = _is_etc_by_name_heuristic(name_norm)
            if is_etc_h:
                return ('ETC',
                        f"IBKR Type='ETF' + heurística ETC ({motivo_h})",
                        True)  # marginal: usuario puede recalificar
            return ('ETF', f"IBKR FII Type='{t}' (autoritativo)", False)
        if t in ('CRYPTOCURRENCY', 'CRYPTO'):
            return ('CRYPTO', f"IBKR FII Type='{t}' (autoritativo)", False)
        if t in ('COMMON', 'PREFERRED', 'COMMON STOCK', 'PREFERRED STOCK'):
            socimi_list = _load_socimi_whitelist()
            if isin in socimi_list:
                entry = socimi_list[isin]
                return ('SOCIMI',
                        f"ISIN en whitelist SOCIMI ES ({entry.get('nombre', isin)} — {entry.get('mercado', '?')})",
                        False)
            # Defensivo: incluso si IBKR dice COMMON, si el ISIN está en
            # whitelist ETC explícita confiamos en la whitelist (DGT V0267-25
            # vincula a la AEAT por encima de la clasificación del broker).
            # NO aplicamos heurística por nombre aquí — IBKR diciendo COMMON
            # es señal fuerte de que NO es ETC físico.
            etc_list = _load_etc_whitelist()
            if isin in etc_list:
                entry = etc_list[isin]
                return ('ETC',
                        f"IBKR Type='{t}' pero ISIN en whitelist ETC "
                        f"({entry.get('ticker', '?')}) — prevalece V0267-25",
                        False)
            return ('STOCK', f"IBKR FII Type='{t}' (autoritativo)", False)
        # 'RIGHT', 'WARRANT', etc. — caen al fallback siguiente.

    # 2. SOCIMI españolas — whitelist por ISIN. Aplica antes que ETF whitelist
    # (las SOCIMI no son IIC sino sociedades) y antes que stock blacklist
    # (porque la SOCIMI tiene casillas distintas a las acciones cotizadas).
    socimi_list = _load_socimi_whitelist()
    if isin in socimi_list:
        entry = socimi_list[isin]
        return ('SOCIMI',
                f"ISIN en whitelist SOCIMI ES ({entry.get('nombre', isin)} — {entry.get('mercado', '?')})",
                False)

    # 2.5. ETC físico — whitelist por ISIN (anclaje primario). Va ANTES de
    # ETF whitelist porque algunos ETCs tienen ISIN europeo (IE/JE/DE) que
    # también encajaría en heurística ETF — la doctrina V0267-25 los califica
    # como RCM 0031, no IIC 2224. La heurística ETC por nombre (fallback) va
    # después de las listas curadas, en el paso 3.5.
    etc_list = _load_etc_whitelist()
    if isin in etc_list:
        entry = etc_list[isin]
        return ('ETC',
                f"ISIN en whitelist ETC ({entry.get('ticker', '?')} - {entry.get('nombre', isin)})",
                False)
    # 3. ETF whitelist por ISIN — fuente curada.
    etf_list = _load_etf_whitelist()
    if isin in etf_list:
        entry = etf_list[isin]
        return ('ETF', f"ISIN en whitelist ETF ({entry.get('gestora', '?')} - {entry.get('indice', '?')})", False)

    # 3. Stock blacklist por ISIN — defensivo.
    blacklist = _load_stock_blacklist()
    if isin in blacklist:
        return ('STOCK', f"ISIN en blacklist (empresa con nombre engañoso): {blacklist[isin].get('motivo', '')}", False)

    # 3.5. Heurística ETC por nombre — conservadora, solo dispara si hay señal
    # clara ("PHYSICAL"+commodity o sufijo " ETC" al final). Va DESPUÉS de las
    # whitelists ETF y blacklist STOCK (prioridad documentada: fuente curada >
    # heurística) — un ISIN curado como ETF/STOCK nunca debe acabar en 0031
    # por su nombre. Si no hay señal, cae al classifier ETF/STOCK normal —
    # preferimos falso negativo (ETC tratado como ETF) que falso positivo
    # (ETF de mineras enviado a 0031 incorrecta).
    is_etc_h, motivo_h = _is_etc_by_name_heuristic(name_norm)
    if is_etc_h:
        # Marcamos UNKNOWN=True para que el usuario pueda re-clasificar
        # manualmente desde el Excel si la heurística se equivocó.
        return ('ETC', f'Heurística ETC: {motivo_h}', True)

    # 4. Heurística DERIVATIVE (DeGiro no expone Asset Category).
    # Se aplica ANTES de la heurística ETF para que un nombre tipo
    # "SG MSCI WORLD FACTOR LONG LEV 5" se clasifique como DERIVATIVE
    # (porque tiene FACTOR + LONG LEV) y no como ETF (porque tiene MSCI).
    if name_norm:
        name_up = name_norm.upper()

        # 4a. Prefijo de emisor de derivados (SG, BNP, VONTOBEL...) +
        # cualquier señal adicional (token derivado o ISIN DE000).
        for emisor in _DERIVATIVE_EMISOR_PREFIXES:
            if name_up.startswith(emisor):
                # Confirmar con token o ISIN alemán de estructurado.
                tiene_token = any(t in name_up for t in _DERIVATIVE_NAME_TOKENS)
                isin_aleman = isin.startswith('DE000') if isin else False
                if tiene_token or isin_aleman:
                    return ('DERIVATIVE',
                            f"Emisor estructurado '{emisor.strip()}' + " +
                            ("token derivado" if tiene_token else f"ISIN {isin[:5]}"),
                            False)

        # 4b. Token derivado claro (FACTOR, TURBO, KO, etc.) sin necesitar
        # prefijo de emisor — cubre productos donde DeGiro pone el nombre
        # sin marca delante.
        for token in _DERIVATIVE_NAME_TOKENS:
            if token in name_up:
                # Para 'FACTOR' / 'WARRANT' / etc. exigimos confirmación con
                # ISIN alemán para evitar falsos positivos (ej. "AMUNDI MSCI
                # WORLD FACTOR INVESTING UCITS ETF" tiene FACTOR pero es ETF).
                if 'UCITS' in name_up or 'OEIC' in name_up or 'SICAV' in name_up:
                    # Si dice UCITS/SICAV es IIC, no derivado. Saltar a heurística ETF.
                    break
                isin_aleman = isin.startswith('DE000') if isin else False
                if isin_aleman:
                    return ('DERIVATIVE',
                            f"Token '{token}' + ISIN alemán {isin[:5]}",
                            False)
                # Sin ISIN alemán: marginal, pero suficiente para flag UNKNOWN.
                return ('DERIVATIVE',
                        f"Token derivado '{token}' detectado (revisar)",
                        True)

    # 5. Heurística ETF — marca de gestora + ISIN europeo.
    if name_norm:
        # Marca de gestora reconocida → señal fuerte, pero NO suficiente:
        # la propia gestora puede ser una acción cotizada (Amundi SA cotiza
        # en Euronext París con ISIN FR). Exigimos además una señal de fondo
        # (UCITS/ETF/índice) en el nombre.
        for marca in _ETF_GESTORA_MARCAS:
            if marca.lower() in name_norm.lower():
                # Reforzar con ISIN europeo para confirmar.
                if isin[:2] in _ETF_FRIENDLY_COUNTRIES:
                    if _RE_ETF_SENAL_FONDO.search(name_norm):
                        return ('ETF', f"Marca gestora '{marca}' + ISIN europeo {isin[:2]} + término de fondo/índice", False)
                    # Marca + ISIN europeo pero sin UCITS/índice en el nombre:
                    # probable acción de la propia gestora → STOCK revisable.
                    return ('STOCK', f"Marca gestora '{marca}' + ISIN europeo {isin[:2]} pero sin término de fondo/índice (UCITS, MSCI…) — posible acción de la gestora, tratado como acción (UNKNOWN, revisable)", True)
                else:
                    # Marca pero ISIN no europeo — improbable que sea ETF UCITS, dudoso.
                    return ('STOCK', f"Marca gestora '{marca}' detectada pero ISIN no europeo ({isin[:2]}) — tratado como acción por defensividad", True)

        # Términos neutros (UCITS, MSCI, S&P 500…) — solo si combinan con ISIN europeo.
        if isin[:2] in _ETF_FRIENDLY_COUNTRIES:
            for termino in _ETF_TERMINOS_NEUTROS:
                if termino.lower() in name_norm.lower():
                    # Doble confirmación: contiene "UCITS" además de algún término.
                    if 'UCITS' in name_norm.upper() or 'OEIC' in name_norm.upper() or 'SICAV' in name_norm.upper():
                        return ('ETF', f"Término ETF '{termino.strip()}' + 'UCITS/OEIC/SICAV' en nombre + ISIN europeo", False)
                    # Solo un término sin UCITS/SICAV — marginal.
                    return ('STOCK', f"Término ETF '{termino.strip()}' detectado pero sin 'UCITS/SICAV' en nombre — tratado como acción (UNKNOWN, revisable)", True)

    # 6. Default → STOCK. No hay UNKNOWN si no hay señales (caso normal).
    return ('STOCK', 'Sin señales de ETF detectadas (default acción)', False)


def is_etf(isin: str, name: str = '', broker: str = '', ibkr_type: str | None = None) -> bool:
    """Helper: True si el instrumento es ETF."""
    t, _, _ = classify_isin(isin, name, broker, ibkr_type)
    return t == 'ETF'


def is_etc(isin: str, name: str = '', broker: str = '', ibkr_type: str | None = None) -> bool:
    """Helper: True si el instrumento es ETC físico (RCM 0031, V0267-25)."""
    t, _, _ = classify_isin(isin, name, broker, ibkr_type)
    return t == 'ETC'


def get_socimi_market(isin: str) -> str | None:
    """Devuelve el mercado de cotización de una SOCIMI española ('Continuo' o
    'Growth') o None si el ISIN no está en la whitelist.

    Crítico para la regla anti-aplicación: SOCIMI en Mercado Continuo (BME)
    están en mercado regulado MiFID II → regla 2 meses (Art. 33.5.f LIRPF).
    SOCIMI en BME Growth son SMN, no mercado regulado → regla 1 año
    (Art. 33.5.g LIRPF). El motor fiscal usa este helper para decidir la
    ventana aplicable a cada match.
    """
    isin = (isin or '').strip().upper()
    return _load_socimi_whitelist().get(isin, {}).get('mercado')


def reload_caches() -> None:
    """Fuerza recarga de las listas JSON desde disco. Útil en tests."""
    global _etf_whitelist, _stock_blacklist, _socimi_whitelist, _etc_whitelist
    _etf_whitelist = None
    _stock_blacklist = None
    _socimi_whitelist = None
    _etc_whitelist = None
