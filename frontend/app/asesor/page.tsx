import { ChatAsesor } from '@/components/ChatAsesor';

// El asesor vive como widget flotante (esquina inferior derecha) en toda la app.
// Esta vista a pantalla completa sigue disponible por URL para una sesión larga.
export default function AsesorPage() {
  return (
    <div className="flex flex-col h-[calc(100vh-12rem)] max-w-3xl mx-auto">
      <div className="mb-3">
        <h2 className="text-2xl font-semibold tracking-tight">Asesor</h2>
        <p className="text-sm text-[rgb(var(--muted))]">
          Tu Analista Financiero Senior, con tu cartera, estrategia y plan en contexto. También lo
          tienes como burbuja de chat en cualquier pantalla. Para análisis profundo con noticias, usa
          la pestaña <strong>Análisis</strong>.
        </p>
      </div>
      <ChatAsesor />
    </div>
  );
}
