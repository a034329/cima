/**
 * Parseo de números escritos por el usuario en formato es-ES o anglosajón.
 *
 * Auditoría Cima 2026-06-11 (A10/F2): `parseFloat(s.replace(',', '.'))`
 * rompía con el separador de miles — "300.000" (objetivo IF) se guardaba
 * como 300 €, "18.000" (colchón) como 18 € — y `parseFloat` de texto no
 * numérico devolvía NaN, que JSON serializa como null y el backend
 * interpreta como "borrar el valor guardado".
 *
 * Reglas:
 *  - admite "1.234,56", "1,234.56", "1234,56", "1234.56", "300.000", "1 000"
 *  - con ambos separadores, el decimal es el ÚLTIMO que aparece
 *  - solo puntos: grupos de 3 tras el primero ⇒ separador de miles
 *    ("300.000" → 300000); si no, decimal ("300.5", "0.123")
 *  - devuelve null para entrada vacía o no numérica (NUNCA NaN): el caller
 *    decide si ignora el cambio o muestra aviso — no borra datos sin querer.
 */
export function parseNumEs(s: string | number | null | undefined): number | null {
  if (typeof s === 'number') return Number.isFinite(s) ? s : null;
  if (typeof s !== 'string') return null;
  let t = s.trim().replace(/[\s€%]/g, '');
  if (!t) return null;
  const neg = t.startsWith('-');
  if (neg || t.startsWith('+')) t = t.slice(1);

  const coma = t.lastIndexOf(',');
  const punto = t.lastIndexOf('.');
  if (coma >= 0 && punto >= 0) {
    const dec = coma > punto ? ',' : '.';
    const miles = dec === ',' ? '.' : ',';
    t = t.split(miles).join('').replace(dec, '.');
  } else if (coma >= 0) {
    const partes = t.split(',');
    // una sola coma ⇒ decimal es-ES; varias ⇒ separador de miles anglosajón
    t = partes.length === 2 ? partes.join('.') : partes.join('');
  } else if (punto >= 0) {
    const partes = t.split('.');
    const esMiles =
      partes.length > 1 &&
      partes[0].length >= 1 &&
      partes[0].length <= 3 &&
      parseInt(partes[0], 10) > 0 &&
      partes.slice(1).every((p) => p.length === 3);
    if (esMiles) t = partes.join('');
  }
  const v = Number(t);
  if (!Number.isFinite(v)) return null;
  return neg ? -v : v;
}
