import type { Metadata } from 'next';
import Link from 'next/link';
import { Fraunces, Inter, IBM_Plex_Mono } from 'next/font/google';
import { ThemeToggle } from '@/components/ThemeToggle';
import { Wordmark } from '@/components/Wordmark';
import './globals.css';

const fraunces = Fraunces({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  style: ['normal', 'italic'],
  variable: '--font-fraunces',
  display: 'swap',
});
const inter = Inter({ subsets: ['latin'], variable: '--font-inter', display: 'swap' });
const mono = IBM_Plex_Mono({
  subsets: ['latin'], weight: ['400', '500'], variable: '--font-mono', display: 'swap',
});

export const metadata: Metadata = {
  title: 'Cima — patrimonio con estrategia',
  description:
    'Tracker con estrategia desde el primer día y motor fiscal español completo para inversores con cartera compleja.',
};

// Tema oscuro por defecto; respeta la preferencia guardada antes de pintar.
const noFlash = `try{if(localStorage.getItem('cima-theme')==='light'){document.documentElement.classList.remove('dark');document.documentElement.style.colorScheme='light';}}catch(e){}`;

const NAV = [
  { href: '/', label: 'Cartera' },
  { href: '/estrategia', label: 'Estrategia' },
  { href: '/fiscal/2026', label: 'Fiscalidad' },
  { href: '/transacciones', label: 'Movimientos' },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="es"
      className={`dark ${fraunces.variable} ${inter.variable} ${mono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: noFlash }} />
      </head>
      <body className="min-h-screen font-sans antialiased">
        <header className="border-b border-[rgb(var(--border))] bg-[rgb(var(--card))]/80 backdrop-blur sticky top-0 z-30">
          <div className="mx-auto max-w-6xl px-6 py-3 flex items-center justify-between gap-4">
            <Link href="/" className="flex items-baseline gap-2.5 group">
              <Wordmark className="text-xl" />
              <span className="hidden sm:inline text-xs text-[rgb(var(--muted))] tracking-wide">
                patrimonio con estrategia
              </span>
            </Link>
            <div className="flex items-center gap-6">
              <nav className="flex gap-5 text-sm">
                {NAV.map((n) => (
                  <Link
                    key={n.href}
                    href={n.href}
                    className="text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] transition-colors"
                  >
                    {n.label}
                  </Link>
                ))}
              </nav>
              <Link
                href="/config"
                aria-label="Configuración"
                title="Configuración"
                className="text-[rgb(var(--muted))] hover:text-[rgb(var(--fg))] transition-colors text-base leading-none"
              >
                ⚙
              </Link>
              <ThemeToggle />
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
        <footer className="border-t border-[rgb(var(--border))] mt-16">
          <div className="mx-auto max-w-6xl px-6 py-5 flex items-center justify-between flex-wrap gap-2 text-xs text-[rgb(var(--muted))]">
            <span>
              <Wordmark className="text-sm" /> · patrimonio con estrategia
            </span>
            <a
              href="https://cuadrate.es"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 hover:text-[rgb(var(--fg))] transition-colors"
            >
              Parte de la suite
              <span className="font-serif">
                <span className="text-brand-300 font-extrabold">[</span>
                <span className="font-semibold text-[rgb(var(--fg))]">Cuádrat</span>
                <span className="text-brand-300 font-semibold italic">e</span>
                <span className="text-brand-300 font-extrabold">]</span>
              </span>
            </a>
          </div>
        </footer>
      </body>
    </html>
  );
}
