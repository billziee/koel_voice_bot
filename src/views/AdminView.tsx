import { useCallback, useEffect, useState } from 'react';
import { Building2, Copy, Plus, RotateCcw, ShieldCheck, UserPlus } from 'lucide-react';

import {
  KoelApiError,
  createOrg,
  deactivateOrg,
  deactivateUser,
  inviteUser,
  listOrgs,
  listUsers,
  reactivateUser,
  resendInvite,
  resetUserPassword,
  tokenOrgId,
  updateUser,
} from '../lib/koel';
import type { Org, PlatformRole, PlatformUser } from '../types';
import type { Theme } from '../lib/theme';

type Props = { theme: Theme; token: string | null; onToast: (m: string) => void };
type Tab = 'orgs' | 'users';

// Org + user admin (backend koel/voicebot/api/orgs.py + users.py). A platform
// admin (org_id=None) manages every tenant; a tenant-scoped admin only ever
// sees/acts on their own org — enforced server-side, mirrored here by simply
// hiding controls that would always 403 (the org picker, other tenants' rows).
export function AdminView({ theme, token, onToast }: Props) {
  const myOrgId = token ? tokenOrgId(token) : null;
  const isPlatformAdmin = myOrgId === null;

  const [tab, setTab] = useState<Tab>('orgs');
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [orgFilter, setOrgFilter] = useState(''); // platform-admin-only users filter
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const [orgRows, userRows] = await Promise.all([
        isPlatformAdmin ? listOrgs(token) : Promise.resolve([]),
        listUsers(token, isPlatformAdmin && orgFilter ? orgFilter : undefined),
      ]);
      setOrgs(orgRows);
      setUsers(userRows);
      setError(null);
    } catch (e) {
      setError(e instanceof KoelApiError ? e.message : 'Could not load admin data.');
    } finally {
      setLoading(false);
    }
  }, [token, isPlatformAdmin, orgFilter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const copyLink = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      onToast('Link copied — share it with the invitee.');
    } catch {
      onToast(`Link: ${url}`);
    }
  };

  if (!token) {
    return (
      <div className="rounded-lg border p-8 text-center text-[13px]" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.faint }}>
        <ShieldCheck size={28} className="mx-auto mb-3 opacity-40" style={{ color: theme.primary }} />
        Connect to a live VB_Svc backend to manage organizations and accounts.
      </div>
    );
  }

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="mb-6">
        <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>Admin</h1>
        <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
          {isPlatformAdmin ? 'Organizations and accounts, across every tenant.' : 'Accounts within your organization.'}
        </p>
      </div>

      {isPlatformAdmin && (
        <div className="flex gap-1 mb-5 border-b" style={{ borderColor: theme.rule }}>
          {(['orgs', 'users'] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className="px-4 py-2 text-[13px] font-medium -mb-px border-b-2"
              style={{
                borderColor: tab === t ? theme.primary : 'transparent',
                color: tab === t ? theme.primary : theme.inkMuted,
              }}
            >
              {t === 'orgs' ? 'Organizations' : 'Users'}
            </button>
          ))}
        </div>
      )}

      {loading && <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>Loading…</div>}
      {!loading && error && <div className="p-8 text-center text-[13px]" style={{ color: theme.warning }}>{error}</div>}

      {!loading && !error && (!isPlatformAdmin || tab === 'orgs') && isPlatformAdmin && (
        <OrgsSection theme={theme} token={token} orgs={orgs} onToast={onToast} onChanged={refresh} />
      )}

      {!loading && !error && (tab === 'users' || !isPlatformAdmin) && (
        <UsersSection
          theme={theme}
          token={token}
          users={users}
          orgs={orgs}
          isPlatformAdmin={isPlatformAdmin}
          orgFilter={orgFilter}
          onOrgFilterChange={setOrgFilter}
          onToast={onToast}
          onCopyLink={copyLink}
          onChanged={refresh}
        />
      )}
    </div>
  );
}

// --- Organizations -----------------------------------------------------------

function OrgsSection({
  theme, token, orgs, onToast, onChanged,
}: {
  theme: Theme; token: string; orgs: Org[]; onToast: (m: string) => void; onChanged: () => Promise<void>;
}) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    setSaving(true);
    try {
      await createOrg(token, name, slug);
      onToast(`Organization “${name}” created.`);
      setName(''); setSlug(''); setShowForm(false);
      await onChanged();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to create organization.');
    } finally {
      setSaving(false);
    }
  };

  const deactivate = async (org: Org) => {
    try {
      await deactivateOrg(token, org.id);
      onToast(`Organization “${org.name}” deactivated.`);
      await onChanged();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to deactivate organization.');
    }
  };

  return (
    <section className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: theme.ruleSubtle }}>
        <div className="flex items-center gap-2">
          <Building2 size={16} style={{ color: theme.primary }} />
          <span className="text-[14px] font-semibold" style={{ color: theme.ink }}>Organizations ({orgs.length})</span>
        </div>
        <button onClick={() => setShowForm((s) => !s)} className="flex items-center gap-1.5 text-[12.5px] font-bold px-3 py-1.5 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
          <Plus size={13} /> {showForm ? 'Close' : 'New org'}
        </button>
      </div>

      {showForm && (
        <div className="p-4 border-b flex flex-wrap items-end gap-3" style={{ borderColor: theme.ruleSubtle }}>
          <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Bank" className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink }} />
          </label>
          <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
            Slug
            <input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} placeholder="acme" className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none font-mono" style={{ borderColor: theme.rule, color: theme.ink }} />
          </label>
          <button
            disabled={saving || !name.trim() || !slug.trim()}
            onClick={() => void submit()}
            className="text-[12.5px] font-bold px-4 py-1.5 rounded-md text-white disabled:opacity-50"
            style={{ backgroundColor: theme.primary }}
          >
            {saving ? 'Creating…' : 'Create'}
          </button>
        </div>
      )}

      <div className="divide-y" style={{ borderColor: theme.ruleSubtle }}>
        {orgs.length === 0 && <p className="text-[12.5px] p-4" style={{ color: theme.faint }}>No organizations yet.</p>}
        {orgs.map((org) => (
          <div key={org.id} className="p-4 flex items-center justify-between">
            <div>
              <div className="text-[13.5px] font-medium" style={{ color: theme.ink }}>{org.name}</div>
              <div className="text-[12px] font-mono" style={{ color: theme.faint }}>{org.slug}</div>
            </div>
            <div className="flex items-center gap-3">
              <span
                className="text-[11.5px] font-medium px-2 py-0.5 rounded"
                style={{ backgroundColor: org.active ? theme.greenLight : theme.destructiveLight, color: org.active ? theme.green : theme.destructive }}
              >
                {org.active ? 'Active' : 'Inactive'}
              </span>
              {org.active && (
                <button onClick={() => void deactivate(org)} className="text-[12px] font-medium" style={{ color: theme.destructive }}>
                  Deactivate
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// --- Users --------------------------------------------------------------------

function UsersSection({
  theme, token, users, orgs, isPlatformAdmin, orgFilter, onOrgFilterChange, onToast, onCopyLink, onChanged,
}: {
  theme: Theme; token: string; users: PlatformUser[]; orgs: Org[]; isPlatformAdmin: boolean;
  orgFilter: string; onOrgFilterChange: (v: string) => void; onToast: (m: string) => void;
  onCopyLink: (url: string) => Promise<void>; onChanged: () => Promise<void>;
}) {
  const [showForm, setShowForm] = useState(false);
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<PlatformRole>('operator');
  const [inviteOrgId, setInviteOrgId] = useState('');
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    setSaving(true);
    try {
      const result = await inviteUser(token, {
        username, email, role, orgId: isPlatformAdmin ? (inviteOrgId || null) : undefined,
      });
      onToast(`Invited “${username}”.`);
      await onCopyLink(result.inviteUrl);
      setUsername(''); setEmail(''); setRole('operator'); setInviteOrgId(''); setShowForm(false);
      await onChanged();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to invite user.');
    } finally {
      setSaving(false);
    }
  };

  const toggleRole = async (user: PlatformUser) => {
    try {
      const next: PlatformRole = user.role === 'admin' ? 'operator' : 'admin';
      await updateUser(token, user.id, { role: next });
      onToast(`${user.username ?? user.email} is now ${next}.`);
      await onChanged();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to update role.');
    }
  };

  const toggleActive = async (user: PlatformUser) => {
    try {
      if (user.active) {
        await deactivateUser(token, user.id);
      } else {
        await reactivateUser(token, user.id);
      }
      onToast(`${user.username ?? user.email} ${user.active ? 'deactivated' : 'reactivated'}.`);
      await onChanged();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to update account status.');
    }
  };

  const handleResetPassword = async (user: PlatformUser) => {
    try {
      const result = await resetUserPassword(token, user.id);
      await onCopyLink(result.inviteUrl);
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to issue a reset link.');
    }
  };

  const handleResendInvite = async (user: PlatformUser) => {
    try {
      const result = await resendInvite(token, user.id);
      await onCopyLink(result.inviteUrl);
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to resend the invite.');
    }
  };

  return (
    <section className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="p-4 border-b flex flex-wrap items-center justify-between gap-3" style={{ borderColor: theme.ruleSubtle }}>
        <div className="flex items-center gap-3">
          <span className="text-[14px] font-semibold" style={{ color: theme.ink }}>Users ({users.length})</span>
          {isPlatformAdmin && orgs.length > 0 && (
            <select
              value={orgFilter}
              onChange={(e) => onOrgFilterChange(e.target.value)}
              className="border rounded-md px-2 py-1 text-[12.5px] outline-none"
              style={{ borderColor: theme.rule, color: theme.ink }}
            >
              <option value="">All organizations</option>
              {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          )}
        </div>
        <button onClick={() => setShowForm((s) => !s)} className="flex items-center gap-1.5 text-[12.5px] font-bold px-3 py-1.5 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
          <UserPlus size={13} /> {showForm ? 'Close' : 'Invite user'}
        </button>
      </div>

      {showForm && (
        <div className="p-4 border-b flex flex-wrap items-end gap-3" style={{ borderColor: theme.ruleSubtle }}>
          <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
            Username
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="jsmith" className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink }} />
          </label>
          <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
            Email
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="j@acme.com" className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink }} />
          </label>
          <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as PlatformRole)} className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink }}>
              <option value="operator">Operator</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          {isPlatformAdmin && (
            <label className="flex flex-col gap-1 text-[12px]" style={{ color: theme.ink2 }}>
              Organization
              <select value={inviteOrgId} onChange={(e) => setInviteOrgId(e.target.value)} className="border rounded-md px-2.5 py-1.5 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink }}>
                <option value="">Platform admin (no org)</option>
                {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
              </select>
            </label>
          )}
          <button
            disabled={saving || !username.trim() || !email.trim()}
            onClick={() => void submit()}
            className="text-[12.5px] font-bold px-4 py-1.5 rounded-md text-white disabled:opacity-50"
            style={{ backgroundColor: theme.primary }}
          >
            {saving ? 'Inviting…' : 'Send invite'}
          </button>
        </div>
      )}

      <div className="divide-y" style={{ borderColor: theme.ruleSubtle }}>
        {users.length === 0 && <p className="text-[12.5px] p-4" style={{ color: theme.faint }}>No users yet.</p>}
        {users.map((user) => (
          <div key={user.id} className="p-4 flex items-center justify-between gap-3 flex-wrap">
            <div className="min-w-0">
              <div className="text-[13.5px] font-medium" style={{ color: theme.ink }}>{user.username ?? user.email}</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>
                {user.email}{user.orgId ? ` · ${orgs.find((o) => o.id === user.orgId)?.name ?? user.orgId}` : ' · Platform admin'}
              </div>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => void toggleRole(user)}
                className="text-[11.5px] font-medium px-2 py-0.5 rounded"
                style={{ backgroundColor: user.role === 'admin' ? theme.primaryLight : theme.ruleSubtle, color: user.role === 'admin' ? theme.primary : theme.inkMuted }}
                title="Toggle role"
              >
                {user.role}
              </button>
              {user.invitePending && (
                <span className="text-[11.5px] font-medium px-2 py-0.5 rounded" style={{ backgroundColor: theme.warningLight, color: theme.warning }}>
                  Invite pending
                </span>
              )}
              <span
                className="text-[11.5px] font-medium px-2 py-0.5 rounded"
                style={{ backgroundColor: user.active ? theme.greenLight : theme.destructiveLight, color: user.active ? theme.green : theme.destructive }}
              >
                {user.active ? 'Active' : 'Inactive'}
              </span>
              {user.invitePending ? (
                <button onClick={() => void handleResendInvite(user)} className="flex items-center gap-1 text-[12px] font-medium" style={{ color: theme.primary }}>
                  <Copy size={12} /> Resend link
                </button>
              ) : (
                <button onClick={() => void handleResetPassword(user)} className="flex items-center gap-1 text-[12px] font-medium" style={{ color: theme.primary }}>
                  <RotateCcw size={12} /> Reset password
                </button>
              )}
              <button onClick={() => void toggleActive(user)} className="text-[12px] font-medium" style={{ color: theme.destructive }}>
                {user.active ? 'Deactivate' : 'Reactivate'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
