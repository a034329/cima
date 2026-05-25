import { EstrategiaNav } from '@/components/EstrategiaNav';

export default function EstrategiaLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <EstrategiaNav />
      {children}
    </div>
  );
}
