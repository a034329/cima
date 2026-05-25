"""Adapters — capa de traducción entre sistemas externos y el dominio de Cima.

Hoy: motor fiscal de Cuádrate en `/app/720/irpf/generar_irpf.py`. Un único
módulo (`cuadrate.py`) encapsula el conocimiento del path y de los nombres
de las funciones. Cuando extraigamos `wg-core/`, sólo este módulo cambia.
"""
