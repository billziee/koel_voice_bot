import React, { useEffect, useRef, useState } from 'react';
import { CheckCircle2, Activity, Lock, XCircle } from 'lucide-react';

import { login, KoelApiError } from '../lib/koel';
import type { Theme } from '../lib/theme';

interface LoginViewProps {
  onLogin: (opts: { live: boolean; token: string | null }) => void;
  theme: Theme;
}

// Real Cloudflare Turnstile is active only when a site key is configured at build
// time (6.7). Without it, the login form falls back to the local "I'm human"
// simulation so the offline demo/CI path (and its e2e) is unchanged.
const TURNSTILE_SITE_KEY: string | undefined =
  (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_TURNSTILE_SITE_KEY;

const TURNSTILE_SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

interface TurnstileApi {
  render: (
    el: HTMLElement,
    opts: {
      sitekey: string;
      callback: (token: string) => void;
      'expired-callback'?: () => void;
      'error-callback'?: () => void;
      theme?: 'light' | 'dark' | 'auto';
    },
  ) => string;
  reset: (widgetId?: string) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

function loadTurnstileScript(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (window.turnstile) {
      resolve();
      return;
    }
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${TURNSTILE_SCRIPT}"]`);
    if (existing) {
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error('turnstile script failed')));
      return;
    }
    const script = document.createElement('script');
    script.src = TURNSTILE_SCRIPT;
    script.async = true;
    script.defer = true;
    script.addEventListener('load', () => resolve());
    script.addEventListener('error', () => reject(new Error('turnstile script failed')));
    document.head.appendChild(script);
  });
}

export function LoginView({ onLogin, theme }: LoginViewProps) {
  const [email, setEmail] = useState('demo');
  const [password, setPassword] = useState('koel-demo');
  const [authing, setAuthing] = useState(false);
  const [error, setError] = useState<string | null>(null); // Item 60
  // Simulation path (no site key): a local checkbox stands in for the challenge.
  const [humanVerified, setHumanVerified] = useState(false); // Item 21
  const [verifying, setVerifying] = useState(false);
  // Real path (site key set): the token Cloudflare hands back on a solved challenge.
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [widgetError, setWidgetError] = useState(false);
  const widgetRef = useRef<HTMLDivElement | null>(null);

  const usingRealTurnstile = Boolean(TURNSTILE_SITE_KEY);

  useEffect(() => {
    if (!usingRealTurnstile || !widgetRef.current) return;
    let cancelled = false;
    loadTurnstileScript()
      .then(() => {
        if (cancelled || !widgetRef.current || !window.turnstile) return;
        window.turnstile.render(widgetRef.current, {
          sitekey: TURNSTILE_SITE_KEY as string,
          theme: 'light',
          callback: (token: string) => setTurnstileToken(token),
          'expired-callback': () => setTurnstileToken(null),
          'error-callback': () => setTurnstileToken(null),
        });
      })
      .catch(() => {
        // Script blocked/offline: surface it rather than silently allowing login.
        if (!cancelled) setWidgetError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [usingRealTurnstile]);

  const handleVerify = () => {
    setVerifying(true);
    window.setTimeout(() => { setHumanVerified(true); setVerifying(false); }, 800);
  };

  const verified = usingRealTurnstile ? Boolean(turnstileToken) : humanVerified;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!verified) {
      setError(
        usingRealTurnstile
          ? 'Please complete the Cloudflare verification.'
          : 'Please complete the human verification checkbox.',
      );
      return;
    }
    setAuthing(true);
    // Try the real VB_Svc /v1/auth/login. On 401 → bad creds; on 403 → the
    // Turnstile check was rejected by the backend. On a network error (backend
    // down) → fall back to the offline demo simulation so the showcase still runs.
    try {
      const tok = await login(email, password, usingRealTurnstile ? turnstileToken : undefined);
      onLogin({ live: true, token: tok });
    } catch (err) {
      if (err instanceof KoelApiError && err.status === 401) {
        setError('401 — Invalid credentials. Use the seeded demo credentials below.');
        setAuthing(false);
        return;
      }
      if (err instanceof KoelApiError && err.status === 403) {
        setError('403 — Human verification failed. Please retry the challenge.');
        setTurnstileToken(null);
        if (usingRealTurnstile && window.turnstile) window.turnstile.reset();
        setAuthing(false);
        return;
      }
      // Backend unreachable — proceed in demo mode so the showcase still runs.
      onLogin({ live: false, token: null });
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6" style={{ backgroundColor: theme.primary, fontFamily: '"DM Sans", sans-serif' }}>
      <div className="absolute top-[-60px] right-[-80px] w-[480px] h-[480px] rounded-full border border-white/10 pointer-events-none"></div>
      <div className="absolute bottom-[-100px] left-[80px] w-[320px] h-[320px] rounded-full border border-white/10 pointer-events-none"></div>

      <div className="w-full max-w-md animate-in fade-in slide-in-from-bottom-2">
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-full bg-white mx-auto mb-4 flex items-center justify-center text-2xl font-semibold" style={{ color: theme.primary, fontFamily: '"Lora", serif' }}>K</div>
          <h1 className="text-4xl font-semibold text-white mb-2" style={{ fontFamily: '"Lora", serif' }}>KOEL</h1>
          <p className="text-sm text-white/70">Voice Bot Platform for Financial Services</p>
        </div>

        <form onSubmit={submit} className="bg-white rounded-lg shadow-2xl p-8">
          <h2 className="font-semibold text-lg mb-1" style={{ color: '#000A1B' }}>Sign in to the dashboard</h2>
          <p className="text-xs mb-6" style={{ color: '#67707F' }}>All endpoints are JWT-protected — even in the MVP demo. The WebSocket handshake presents this token before upgrading.</p>

          <label htmlFor="login-email" className="block text-[12.5px] font-medium mb-1.5" style={{ color: '#3A4150' }}>Username</label>
          <input id="login-email" type="text" value={email} onChange={e => setEmail(e.target.value)} className="w-full border rounded-md p-2.5 mb-4 outline-none focus:border-[#053BEE] focus:ring-2 focus:ring-[#E8EDFF] transition-all text-[13.5px]" style={{ borderColor: '#E2E6ED', color: '#000A1B' }} />
          <label htmlFor="login-password" className="block text-[12.5px] font-medium mb-1.5" style={{ color: '#3A4150' }}>Password</label>
          <input id="login-password" type="password" value={password} onChange={e => setPassword(e.target.value)} className="w-full border rounded-md p-2.5 mb-4 outline-none focus:border-[#053BEE] focus:ring-2 focus:ring-[#E8EDFF] transition-all text-[13.5px]" style={{ borderColor: '#E2E6ED', color: '#000A1B' }} />

          {/* Cloudflare Turnstile (6.7): real widget when a site key is configured,
              otherwise a local simulation for the offline demo/CI path. */}
          {usingRealTurnstile ? (
            <div className="mb-4">
              <div ref={widgetRef} aria-label="Cloudflare Turnstile challenge" />
              {widgetError && (
                <p className="mt-2 text-xs font-medium" style={{ color: '#D93520' }}>
                  Verification widget failed to load. Check your connection and refresh.
                </p>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-3 mb-4 p-3 border rounded-md" style={{ borderColor: '#E2E6ED', backgroundColor: '#F8FAFC' }}>
              {humanVerified ? (
                <CheckCircle2 size={22} className="text-green-600 shrink-0" />
              ) : verifying ? (
                <Activity size={22} className="text-blue-500 animate-pulse shrink-0" />
              ) : (
                <input type="checkbox" onChange={handleVerify} className="w-5 h-5 accent-[#053BEE] cursor-pointer shrink-0" aria-label="Human verification checkbox" />
              )}
              <span className="text-sm font-medium" style={{ color: '#3A4150' }}>{humanVerified ? 'Verified' : verifying ? 'Verifying…' : "I'm human"}</span>
              <span className="ml-auto text-[10px] font-medium" style={{ color: '#98A0AD' }}>Cloudflare Turnstile (dev)</span>
            </div>
          )}

          {error && (
            <div className="mb-4 p-3 rounded-md text-sm font-medium flex items-center gap-2" style={{ backgroundColor: '#FEF2F0', border: '1px solid #FECDC8', color: '#D93520' }}>
              <XCircle size={16} /> {error}
            </div>
          )}

          <button type="submit" disabled={authing} className="w-full py-3 rounded-md text-white font-medium flex items-center justify-center gap-2 shadow-md transition-all hover:opacity-90 disabled:opacity-60" style={{ backgroundColor: '#053BEE' }}>
            {authing ? <><Activity size={16} className="animate-pulse" /> Issuing JWT…</> : <><Lock size={16} /> Sign In</>}
          </button>

          <div className="mt-5 p-3 rounded-md text-xs" style={{ backgroundColor: '#F2F5FF', border: '1px solid #E8EDFF', color: '#032CB8' }}>
            <span className="font-semibold">Seeded demo credentials:</span> demo / koel-demo
          </div>
        </form>
      </div>
    </div>
  );
}
