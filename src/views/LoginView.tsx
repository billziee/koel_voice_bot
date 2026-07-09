import React, { useState } from 'react';
import { CheckCircle2, Activity, Lock, XCircle } from 'lucide-react';

import { login, KoelApiError } from '../lib/koel';
import type { Theme } from '../lib/theme';

interface LoginViewProps {
  onLogin: (opts: { live: boolean; token: string | null }) => void;
  theme: Theme;
}

export function LoginView({ onLogin, theme }: LoginViewProps) {
  const [email, setEmail] = useState('demo');
  const [password, setPassword] = useState('koel-demo');
  const [authing, setAuthing] = useState(false);
  const [error, setError] = useState<string | null>(null); // Item 60
  const [humanVerified, setHumanVerified] = useState(false); // Item 21
  const [verifying, setVerifying] = useState(false);

  const handleVerify = () => {
    setVerifying(true);
    window.setTimeout(() => { setHumanVerified(true); setVerifying(false); }, 800);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!humanVerified) {
      setError('Please complete the human verification checkbox.');
      return;
    }
    setAuthing(true);
    // Try the real VB_Svc /v1/auth/login. On 401 → bad creds. On a network
    // error (backend down) → fall back to the offline demo simulation.
    try {
      const tok = await login(email, password);
      onLogin({ live: true, token: tok });
    } catch (err) {
      if (err instanceof KoelApiError && err.status === 401) {
        setError('401 — Invalid credentials. Use the seeded demo credentials below.');
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

          {/* Item 21: Cloudflare Turnstile simulation */}
          <div className="flex items-center gap-3 mb-4 p-3 border rounded-md" style={{ borderColor: '#E2E6ED', backgroundColor: '#F8FAFC' }}>
            {humanVerified ? (
              <CheckCircle2 size={22} className="text-green-600 shrink-0" />
            ) : verifying ? (
              <Activity size={22} className="text-blue-500 animate-pulse shrink-0" />
            ) : (
              <input type="checkbox" onChange={handleVerify} className="w-5 h-5 accent-[#053BEE] cursor-pointer shrink-0" aria-label="Human verification checkbox" />
            )}
            <span className="text-sm font-medium" style={{ color: '#3A4150' }}>{humanVerified ? 'Verified' : verifying ? 'Verifying…' : "I'm human"}</span>
            <span className="ml-auto text-[10px] font-medium" style={{ color: '#98A0AD' }}>Cloudflare Turnstile</span>
          </div>

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
