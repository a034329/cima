import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: 'class',
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // Marca Cima: oro/bronce (familia Cuádrate, cuyo oro es #E6B763 = brand-300).
        // brand-600 (#B8860B) funciona como fondo de botón con texto claro.
        brand: {
          50: '#FBF6EC',
          100: '#F6EAD0',
          200: '#ECD5A3',
          300: '#E6B763',  // oro Cuádrate (acento/badges)
          400: '#D4AF37',
          500: '#C2962A',
          600: '#B8860B',  // CTA / fondo botón
          700: '#9A6F09',
          800: '#7C5A0A',
          900: '#5E440B',
        },
        // Azul Cuádrate, para enlaces cruzados de suite.
        cuadrate: '#0B2B8F',
      },
      fontFamily: {
        sans: ['var(--font-inter)', 'Inter', 'system-ui', 'sans-serif'],
        serif: ['var(--font-fraunces)', 'Fraunces', 'Georgia', 'serif'],
        mono: ['var(--font-mono)', 'IBM Plex Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};

export default config;
