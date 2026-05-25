import { CarteraNav } from '@/components/CarteraNav';
import { DividendosChart } from '@/components/DividendosChart';
import { DiversificacionDividendos } from '@/components/DiversificacionDividendos';

export default function DividendosPage() {
  return (
    <div>
      <CarteraNav />
      <div className="space-y-6">
        <DividendosChart />
        <DiversificacionDividendos />
      </div>
    </div>
  );
}
