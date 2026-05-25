import { FiscalNav } from '@/components/FiscalNav';

/**
 * Hub de Fiscalidad. El ejercicio es propiedad de esta sección (no global):
 * el selector de año + las sub-pestañas viven aquí y las páginas hijas
 * heredan el `[ano]` de la ruta. Estructura: /fiscal/<ano>/<seccion>.
 */
export default function FiscalLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <FiscalNav />
      {children}
    </div>
  );
}
