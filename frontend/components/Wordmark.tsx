/**
 * Wordmark de Cima: [Cim·a] — espejo del [Cuádrat·e] de Cuádrate (familia/suite).
 * Corchetes y la "a" final en oro cursivo; resto en color de texto.
 */
export function Wordmark({ className = '' }: { className?: string }) {
  return (
    <span className={`font-serif select-none ${className}`}>
      <span className="text-brand-300 font-extrabold">[</span>
      <span className="font-semibold">Cim</span>
      <span className="text-brand-300 font-semibold italic">a</span>
      <span className="text-brand-300 font-extrabold">]</span>
    </span>
  );
}
