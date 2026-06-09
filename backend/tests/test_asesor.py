"""Tests del asesor IA (contexto, responder con IA fake, persistencia, modo)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.ia.asesor import system_asesor
from app.db import models
from app.services import asesor as svc


class _FakeIA:
    def __init__(self, reply="Vas por buen camino: 56% del objetivo."):
        self.reply = reply
        self.recibido = None

    def completar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        self.recibido = (system, user)
        return self.reply


def _seed(db, cartera, monkeypatch) -> None:
    b = models.Bloque(cartera_id=cartera.id, nombre="Compounders", categoria_base="growth",
                      orden=0, es_base=True)
    db.add(b); db.flush()
    p = models.Posicion(cartera_id=cartera.id, isin="US_MSFT", nombre="Microsoft",
                        divisa_local="EUR", bloque_id=b.id)
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_MSFT", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("30"),
                            metrica_base_4y=Decimal("12")))
    db.commit()
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"US_MSFT": {"sector": "Technology"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_MSFT": (Decimal("100"), "EUR")})
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({"US_MSFT": Decimal("100")}, []))


def test_system_asesor_por_modo() -> None:
    assert "PRESCRIPTIVO" in system_asesor("owner")
    s = system_asesor("saas").lower()
    assert "no des recomendaciones" in s or "no es asesoramiento" in s


def test_system_asesor_incluye_reglas_integridad_analitica() -> None:
    """Guardrails contra los fallos garrafales detectados en la conversación
    LVMH→Hermès (2026-06-09): TTM vs Forward, divisas, contraste de fuentes,
    veredicto antes de datos, capitulación tras corrección, rotación vs DCA,
    preferencia por herramientas internas. Si se elimina alguna, los tests
    avisan — son guardrails críticos, no negociables."""
    s = system_asesor("owner")
    # 1. Múltiplos: TTM vs Forward
    assert "TTM" in s and "Forward" in s
    assert "rotación" in s.lower() and "4 años" in s
    # 2. Divisas: aclarar EUR/USD
    assert "DIVISA" in s or "divisa" in s
    # 3. Contraste de fuentes con discrepancia
    assert "10%" in s and "discrepancia" in s.lower()
    # 4. Cálculos trazables (coherencia interna)
    assert "coherencia" in s.lower() or "cuadra" in s.lower()
    # 5. Veredicto después de datos, no antes
    assert "VEREDICTO" in s or "veredicto" in s
    assert "CAGR4+Div" in s
    # 6. No capitular ante presión social del usuario
    assert "capitular" in s.lower() or "complacencia" in s.lower()
    # 7. Rotación táctica ≠ DCA
    assert "Rotación" in s or "rotación" in s.lower()
    assert "DCA" in s
    # 8. Preferencia por herramientas internas de Cima
    assert "Valoración asistida" in s
    assert "one-pager" in s or "Análisis" in s


def test_system_asesor_incluye_reglas_2a_conversacion() -> None:
    """Guardrails añadidos tras 2ª conversación LVMH→Hermès (2026-06-09):
    capitulación atenuada al dato del usuario contra fuentes propias,
    invención de cifras auxiliares, aritmética interna rota, emisión de
    tarjeta tras decir 'no emito', mezcla doctrinal."""
    s = system_asesor("owner")
    # 9. Datos del usuario que contradicen tus fuentes — no capitular sin verificar
    assert "ORIGEN EXACTO" in s
    assert "METODOLOGÍA" in s or "metodología" in s.lower()
    assert "GAAP" in s         # ejemplo concreto de metodología que importa
    # 10. Cifras auxiliares sin inventar
    assert "anclada" in s.lower() or "anclar" in s.lower() or "fuente" in s.lower()
    assert "SUPUESTO" in s or "supuesto" in s.lower()
    # 11. Auto-verificación aritmética
    assert "REHAZ" in s or "rehaz" in s.lower() or "verifica" in s.lower()
    assert "aritmética" in s.lower() or "cálculo" in s.lower()
    # 12. No emitir tarjeta sobre incertidumbres
    assert "tarjeta" in s.lower()
    assert "presión social" in s.lower() or "complacencia" in s.lower()
    # 13. Doctrina por bloque correcta (15/15 SOLO Bloque A)
    assert "15/15" in s
    assert "Bloque A" in s or "Estable" in s


def test_contexto_incluye_cartera_y_regimen(db: Session, cartera, monkeypatch) -> None:
    _seed(db, cartera, monkeypatch)
    ctx = svc._contexto(db, cartera.id)
    assert "Capital invertido" in ctx
    assert "Microsoft" in ctx                 # fila de la posición
    assert "Régimen macro" in ctx


def test_responder_persiste_y_usa_contexto(db: Session, cartera, monkeypatch) -> None:
    _seed(db, cartera, monkeypatch)
    fake = _FakeIA()
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: fake)

    m, acciones = svc.responder(db, cartera.id, "¿Cómo voy?")
    assert m.rol == "assistant" and "buen camino" in m.contenido
    assert acciones == []                          # la respuesta no traía acciones
    assert "Microsoft" in fake.recibido[0]         # el system llevaba el estado de la cartera
    assert [x.rol for x in svc.historial(db, cartera.id)] == ["user", "assistant"]

    svc.limpiar(db, cartera.id)
    assert svc.historial(db, cartera.id) == []


def test_parse_acciones_valida_y_limpia() -> None:
    txt = ('Bajaría el PER de Microsoft.\n\n```json\n{"acciones":['
           '{"tipo":"ajustar_estimacion","isin":"US_MSFT","multiplo_objetivo":25,"metrica_base_4y":14,"razon":"r"},'
           '{"tipo":"crear_paso","isin":"US_X","decision":"VENDER","prioridad":"ALTA","razon":"r"},'
           '{"tipo":"hackear","isin":"X"}]}\n```')
    limpio, acc = svc.parse_acciones(txt)
    assert "```" not in limpio and limpio.startswith("Bajaría")
    assert [a.tipo for a in acc] == ["ajustar_estimacion", "crear_paso"]   # 'hackear' descartado
    assert acc[1].params["decision"] == "VENDER"


def test_ajustar_estimacion_acepta_tipo_val_y_dividendo() -> None:
    # cambiar el método de valoración (PER → P_FRE, p.ej. BAM) + múltiplo/métrica coherentes
    _, acc = svc.parse_acciones(
        '```json\n{"acciones":[{"tipo":"ajustar_estimacion","isin":"CA_BAM","tipo_val":"P_FRE",'
        '"multiplo_objetivo":22,"metrica_base_4y":5,"razon":"se valora por FRE"}]}\n```')
    assert len(acc) == 1
    p = acc[0].params
    assert p["tipo_val"] == "P_FRE" and p["multiplo_objetivo"] == 22.0 and p["metrica_base_4y"] == 5.0
    assert "P/FRE" in acc[0].descripcion

    # solo dividendo, sin tocar múltiplo/métrica
    _, acc2 = svc.parse_acciones(
        '```json\n{"acciones":[{"tipo":"ajustar_estimacion","isin":"US_KO","dividendo_share":1.94}]}\n```')
    assert len(acc2) == 1 and acc2[0].params["dividendo_share"] == 1.94
    assert "multiplo_objetivo" not in acc2[0].params and "dividendo" in acc2[0].descripcion

    # múltiplo sin métrica (incoherente) → descartada
    _, acc3 = svc.parse_acciones(
        '```json\n{"acciones":[{"tipo":"ajustar_estimacion","isin":"US_X","multiplo_objetivo":20}]}\n```')
    assert acc3 == []

    # tipo_val inválido → descartada
    _, acc4 = svc.parse_acciones(
        '```json\n{"acciones":[{"tipo":"ajustar_estimacion","isin":"US_X","tipo_val":"P/EBITDA"}]}\n```')
    assert acc4 == []


def test_responder_owner_extrae_acciones_y_saas_no(db: Session, cartera, monkeypatch) -> None:
    _seed(db, cartera, monkeypatch)
    reply = ('Ajusta Microsoft.\n```json\n{"acciones":[{"tipo":"ajustar_estimacion",'
             '"isin":"US_MSFT","multiplo_objetivo":25,"metrica_base_4y":14,"razon":"r"}]}\n```')
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA(reply))
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "mode", "owner")
    _, acc = svc.responder(db, cartera.id, "ajusta lo que veas mal")
    assert len(acc) == 1 and acc[0].tipo == "ajustar_estimacion"

    monkeypatch.setattr(cfg.settings, "mode", "saas")
    m, acc = svc.responder(db, cartera.id, "ajusta lo que veas mal")
    assert acc == []                               # en SaaS no se emiten acciones
    assert "```" in m.contenido or "acciones" in m.contenido  # el texto queda tal cual (sin parsear)


def test_requiere_web_detecta_palabras_clave() -> None:
    from app.services.asesor import _requiere_web
    assert _requiere_web("por qué L'Oréal sube hoy un 5%")
    assert _requiere_web("¿qué noticias hay de Microsoft?")
    assert _requiere_web("cuál es el precio actual de Apple")
    assert _requiere_web("qué pasa con Diageo")
    assert _requiere_web("Novo ha publicado resultados?")
    assert not _requiere_web("¿cómo voy hacia la IF?")
    assert not _requiere_web("revisa mis estimaciones de Microsoft")
    assert not _requiere_web("¿están bien las estimaciones?")


class _FakeIADual:
    def __init__(self) -> None:
        self.completar_n = 0
        self.investigar_n = 0
    def completar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        self.completar_n += 1
        return "ok"
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        self.investigar_n += 1
        return "ok con web"


def test_responder_enruta_a_investigar_si_la_pregunta_pide_actualidad(
    db: Session, cartera, monkeypatch,
) -> None:
    _seed(db, cartera, monkeypatch)
    ia = _FakeIADual()
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: ia)

    svc.responder(db, cartera.id, "¿cómo voy hacia la IF?")            # sin web
    svc.responder(db, cartera.id, "por qué L'Oréal sube hoy un 5%")     # con web
    assert ia.completar_n == 1
    assert ia.investigar_n == 1


def test_requiere_web_ampliada_cubre_busqueda_e_investiga() -> None:
    from app.services.asesor import _requiere_web
    # Peticiones explícitas que la heurística vieja no cazaba (caso real Angel).
    assert _requiere_web("investiga Microsoft a fondo")
    assert _requiere_web("búscame las últimas noticias de OWL")
    assert _requiere_web("¿puedes buscar información sobre BAM?")
    assert _requiere_web("dame datos en tiempo real de la cotización")


def test_forzar_web_salta_la_heuristica(db: Session, cartera, monkeypatch) -> None:
    """El toggle 🌐 del chat debe enrutar a `investigar` aunque la pregunta no
    contenga ningún keyword (caso 'qué herramientas tienes')."""
    _seed(db, cartera, monkeypatch)
    ia = _FakeIADual()
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: ia)

    # Pregunta neutra que NO dispara la heurística:
    svc.responder(db, cartera.id, "qué herramientas tienes a tu disposición")
    assert ia.investigar_n == 0 and ia.completar_n == 1
    # La misma pregunta con forzar_web=True debe ir a investigar.
    svc.responder(db, cartera.id, "qué herramientas tienes a tu disposición",
                  forzar_web=True)
    assert ia.investigar_n == 1


def test_system_prompt_sin_web_redirige_al_toggle() -> None:
    """Cuando NO hay web, el system prompt instruye a redirigir al toggle 🌐,
    no a decir 'no tengo acceso' (el bug original que vio Angel)."""
    from app.adapters.ia.asesor import system_asesor
    sin_web = system_asesor("owner", con_web=False)
    assert "🌐" in sin_web and "vuelve a preguntármelo" in sin_web
    # Cita explícita de la frase prohibida (en 2ª persona, dirigida al modelo):
    assert "no tienes acceso a" in sin_web
