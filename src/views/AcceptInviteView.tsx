import React, { useState } from 'react';
import { CheckCircle2, Lock } from 'lucide-react';

import { acceptInvite, KoelApiError } from '../lib/koel';
import type { Theme } from '../lib/theme';

interface Props {
  token: string; // the invite/reset token from the `?invite=` query param
  theme: Theme;
  onAccepted: (accessToken: string) => void;
  onCancel: () => void;
}

// Standalone page reached via a shared invite/reset link (no client-side
// router in this SPA — App.tsx renders this in place of the normal
// login/dashboard flow when it detects `?invite=` in the URL). Redeems the
// token via the PUBLIC accept-invite route and sets a password; on success
// the backend hands back a working access token so the user lands logged in
// without a second login step.
export function AcceptInviteView({ token, theme, onAccepted, onCancel }: Props) {
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    setSubmitting(true);
    try {
      const accessToken = await acceptInvite(token, password);
      onAccepted(accessToken);
    } catch (err) {
      setError(
        err instanceof KoelApiError
          ? err.message
          : 'Could not reach the backend. Check your connection and try again.',
      );
      setSubmitting(false);
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
          <p className="text-sm text-white/70">Set up your account</p>
        </div>

        <form onSubmit={submit} className="bg-white rounded-lg shadow-2xl p-8">
          <h2 className="font-semibold text-lg mb-1 flex items-center gap-2" style={{ color: '#000A1B' }}>
            <Lock size={18} style={{ color: '#053BEE' }} /> Choose a password
          </h2>
          <p className="text-xs mb-6" style={{ color: '#67707F' }}>
            This link was sent by your organization's admin. It works once and expires after a few days.
          </p>

          <label htmlFor="accept-password" className="block text-[12.5px] font-medium mb-1.5" style={{ color: '#3A4150' }}>New password</label>
          <input
            id="accept-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full border rounded-md p-2.5 mb-4 outline-none focus:border-[#053BEE] focus:ring-2 focus:ring-[#E8EDFF] transition-all text-[13.5px]"
            style={{ borderColor: '#E2E6ED', color: '#000A1B' }}
          />
          <label htmlFor="accept-confirm" className="block text-[12.5px] font-medium mb-1.5" style={{ color: '#3A4150' }}>Confirm password</label>
          <input
            id="accept-confirm"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            className="w-full border rounded-md p-2.5 mb-4 outline-none focus:border-[#053BEE] focus:ring-2 focus:ring-[#E8EDFF] transition-all text-[13.5px]"
            style={{ borderColor: '#E2E6ED', color: '#000A1B' }}
          />

          {error && (
            <div className="mb-4 text-[12.5px] font-medium px-3 py-2 rounded-md" style={{ backgroundColor: '#FEF2F0', color: '#D93520' }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full flex items-center justify-center gap-2 py-2.5 rounded-md text-white font-semibold text-sm disabled:opacity-50"
            style={{ backgroundColor: '#053BEE' }}
          >
            <CheckCircle2 size={16} /> {submitting ? 'Setting up…' : 'Set password and sign in'}
          </button>

          <button
            type="button"
            onClick={onCancel}
            className="w-full mt-3 text-[12.5px] font-medium"
            style={{ color: '#67707F' }}
          >
            Cancel and go to sign in
          </button>
        </form>
      </div>
    </div>
  );
}
