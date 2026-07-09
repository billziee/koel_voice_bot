// Theme tokens — CSS variables for light/dark (Item 61).
//
// Extracted from App.tsx (roadmap Q-1 decomposition) so view modules can import
// both the palette factory and its `Theme` type instead of reaching into the
// monolith. `Theme` is what every view/component annotates its `theme` prop with.

export const getTheme = (dark: boolean) => ({
  primary: dark ? '#7DA2FF' : '#053BEE',
  primaryDark: dark ? '#A3BFFF' : '#032CB8',
  primaryLight: dark ? '#1E2D5E' : '#E8EDFF',
  primarySubtle: dark ? '#0F1A3A' : '#F2F5FF',
  surface: dark ? '#0B1120' : '#F5F7FA',
  surfaceCard: dark ? '#111827' : '#FFFFFF',
  ink: dark ? '#E5E7EB' : '#000A1B',
  ink2: dark ? '#D1D5DB' : '#3A4150',
  inkMuted: dark ? '#9CA3AF' : '#67707F',
  faint: dark ? '#6B7280' : '#98A0AD',
  rule: dark ? '#374151' : '#E2E6ED',
  ruleSubtle: dark ? '#1F2937' : '#ECEFF4',
  accent: dark ? '#F97316' : '#EB5324',
  accentLight: dark ? '#431407' : '#FFF0EB',
  green: dark ? '#34D399' : '#05604F',
  greenLight: dark ? '#064E3B' : '#EDFAF4',
  greenMid: dark ? '#10B981' : '#2DBD74',
  destructive: dark ? '#EF4444' : '#D93520',
  destructiveLight: dark ? '#450A0A' : '#FEF2F0',
  warning: dark ? '#F59E0B' : '#8A5800',
  warningLight: dark ? '#451A03' : '#FFF7E6',
  sidebarBg: dark ? '#0F172A' : '#FFFFFF',
  inset: dark ? '#0F172A' : '#F8FAFC',
  otelBg: dark ? '#020617' : '#000A1B',
  transcriptBg: dark ? '#0F172A' : '#F5F7FA',
});

export type Theme = ReturnType<typeof getTheme>;
