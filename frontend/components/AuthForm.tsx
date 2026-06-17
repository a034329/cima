'use client';

import { useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { login, signup, fetchMe } from '@/lib/api';
import { setToken } from '@/lib/auth';
import { Wordmark } from '@/components/Wordmark';

export function AuthForm({ modo }: { modo: 'login' | 'signup' }) {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const esSignup = modo === 'signup';

  async function enviar(e: React.FormEvent) {
    e.preventDefault();
    setCargando(true);
    setError(null);
    try {
      const r = esSignup
        ? await signup(email.trim().toLowerCase(), password)
        : await login(email.trim().toLowerCase(), password);
      setToken(r.access_token);
      // Verifica que el token vale antes de navegar (y caza desajustes de modo).
      await fetchMe();
      const destino = params.get('next') || '/';
      router.replace(destino);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCargando(false);
    }
  }

  return (
    <div className="min-h-[80vh] flex items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="text-center space-y-1">
          <Wordmark className="text-3xl" />
          <p className="text-sm text-[rgb(var(--muted))]">
            {esSignup ? 'Crea tu cuenta' : 'Entra en tu cartera'}
          </p>
        </div>

        <form onSubmit={enviar} className="space-y-4 rounded-lg border border-[rgb(var(--border))] bg-[rgb(var(--card))] p-6">
          <label className="block space-y-1">
            <span className="text-xs font-medium text-[rgb(var(--muted))]">Email</span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-2.5 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-[rgb(var(--muted))]">Contraseña</span>
            <input
              type="password"
              autoComplete={esSignup ? 'new-password' : 'current-password'}
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-2.5 py-1.5 text-sm rounded border border-[rgb(var(--border))] bg-[rgb(var(--bg))]"
            />
            {esSignup && (
              <span className="text-[11px] text-[rgb(var(--muted))]">Mínimo 8 caracteres.</span>
            )}
          </label>

          {error && <p className="text-sm text-rose-600">{error}</p>}

          <button
            type="submit"
            disabled={cargando}
            className="w-full px-3 py-1.5 text-sm font-medium rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
          >
            {cargando ? 'Un momento…' : esSignup ? 'Crear cuenta' : 'Entrar'}
          </button>
        </form>

        <p className="text-center text-sm text-[rgb(var(--muted))]">
          {esSignup ? (
            <>¿Ya tienes cuenta?{' '}
              <Link href="/login" className="text-brand-600 hover:underline">Entra</Link>
            </>
          ) : (
            <>¿Aún no tienes cuenta?{' '}
              <Link href="/signup" className="text-brand-600 hover:underline">Regístrate</Link>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
