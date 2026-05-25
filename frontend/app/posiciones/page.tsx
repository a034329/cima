import { CarteraNav } from '@/components/CarteraNav';
import { PosicionesVista } from '@/components/PosicionesVista';
import { AccionesCartera } from '@/components/AccionesCartera';

export default function PosicionesPage() {
  return (
    <div>
      <CarteraNav />
      <div className="mb-6 flex justify-end">
        <AccionesCartera carteraVacia={false} />
      </div>
      <PosicionesVista />
    </div>
  );
}
