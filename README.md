# Cima

> *El tracker con estrategia desde el primer día y con el motor fiscal más completo del mercado español para inversores con cartera compleja.*

**Dominio**: `cima.app` (pendiente de registro).
**Estado**: fase 0 — diseño y scaffolding.
**Mercado primario**: España. Inversor particular medio-alto con cartera compleja y objetivo de Independencia Financiera (IF).

---

## Producto en una frase

Cima es el compañero digital del inversor de perfil medio-alto que opera con cartera compleja en España y quiere llegar a la independencia financiera con método. Consolida operativa, aplica fiscalidad española completa (incluidas opciones, corporate actions, T-Bills, derechos, forex y crypto), estructura la cartera según una estrategia firmada por el propio usuario, y la vigila contra los sesgos psicológicos.

## Posicionamiento

> *No estás aprendiendo a invertir. Quieres consolidar lo que ya haces.*

## Diferenciadores reales (basados en el motor de Cuádrate ya existente)

- Opciones financieras con jurisprudencia DGT V2172-21 aplicada (5 casos fiscales).
- Corporate actions: splits, NAME_CHANGE, ISIN_CHANGE con migración FIFO, scrip dividend puro y mixto, scrip multi-paso IBKR.
- T-Bills, bonos, forex multidivisa, crypto IBKR/TR.
- Tasas externas cruzadas al coste: Tobin, Stamp Duty, SEC fee, FINRA TAF, FTT francés.
- Deducción CDI casilla 0588 con límites por país.
- Bloques de estrategia con plan personal firmado.
- Coaching IA puntual contra sesgos psicológicos (onboarding co-construido).

## Tiers comerciales

| Tier | Disponible en | Pricing anual |
|---|---|---|
| Free trial | Fase 1+ | 0 € (1 mes) |
| Base | Fase 1 | 99 € |
| Base + Estimaciones | Fase 2 | 149 € |
| Base + Plan IA | Fase 3 | 249 € |
| Full / Pro | Fase 3 | 399 € |
| Add-on Recuperación CDI | Fase 4 | Variable |

## Documentos clave

- [Documento de diseño Word](/app/WealthGuardian_producto_y_mercado.docx) — producto, mercado, marco regulatorio.
- [ROADMAP.md](./ROADMAP.md) — fases, hitos, criterios de paso.
- [docs/decisions/](./docs/decisions/) — Architecture Decision Records.
- [Motor fiscal Cuádrate](/app/720/) — base técnica reutilizable.
- [WG personal](/app/) — productividad personal, doctrina del agente y origen.

## Modos de operación previstos

Cima nace con dos modos de ejecución desde el día 1:

- **Modo SaaS** (clientes): IA capada, disclaimers MiFID II, sin recomendaciones concretas, decisión humana explícita.
- **Modo Owner** (instancia personal del fundador): IA sin restricciones, recomendaciones directas, agente externo (Claude Code o voz) actuando sobre la API. *Defensible legalmente porque el usuario es a la vez prestador y cliente.*

## Relación con productos hermanos

- **Cuádrate** sigue como producto independiente (declaración IRPF, 19,90 €/decl.). Cima ofrece descuento 50% a sus usuarios. No se canibalizan.
- **WG personal** (Excel + scripts + agente actual) sigue funcionando intacto. Cima se construye en paralelo. La transición a Cima como motor principal es la Fase 3-4 del roadmap personal del fundador.
