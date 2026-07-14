import { useEffect, useState } from 'react';

import { listConnections } from '../lib/koel';
import type { ModelConnection } from '../types';

// Loads the org's model connections once on mount (and whenever the token
// changes). Read-only: the per-bot selectors only need the list to populate
// their dropdowns. On any failure — or when running without a live token — it
// degrades to an empty list so the caller still offers the "Default (built-in)"
// option. The full CRUD surface (create/edit/test/deactivate) lives in
// ConnectionsView, which owns its own fetch + refresh instead of this hook.
export function useConnections(token: string | null): ModelConnection[] {
  const [connections, setConnections] = useState<ModelConnection[]>([]);

  useEffect(() => {
    if (!token) {
      setConnections([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const rows = await listConnections(token);
        if (!cancelled) setConnections(rows);
      } catch {
        if (!cancelled) setConnections([]); // degrade to Default-only
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  return connections;
}
