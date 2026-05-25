import { fetchCartera } from '@/lib/api';
import { CarteraNav } from '@/components/CarteraNav';
import { Dashboard } from '@/components/Dashboard';
import { AccionesCartera } from '@/components/AccionesCartera';

const MOCK_CARTERA_ID = '00000000-0000-0000-0000-000000000001';

export default async function HomePage() {
  let cartera;
  try {
    cartera = await fetchCartera();
  } catch {
    cartera = null;
  }
  const esMock = !cartera || cartera.cartera_id === MOCK_CARTERA_ID;

  return (
    <div>
      <CarteraNav />
      <div className="mb-6 flex items-center justify-between gap-4 flex-wrap">
        <div className="text-xs text-[rgb(var(--muted))]">
          {esMock ? (
            <span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
              Modo demo · BD vacía. Importa un extracto para empezar.
            </span>
          ) : (
            <span>A fecha de hoy</span>
          )}
        </div>
        <AccionesCartera carteraVacia={esMock} />
      </div>
      <Dashboard />
    </div>
  );
}
