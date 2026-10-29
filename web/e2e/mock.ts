export const mockSettings = {
  revision: 1,
  items: [],
  audit: {
    logical_bytes: 1024,
    db_bytes: 32768,
    wal_bytes: 4096,
    shm_bytes: 32768,
    degraded: false,
  },
  session: { degraded: false, last_error: null, path: "/tmp/admin-sessions.json" },
};

export const mockDashboard = {
  summary: { requests: 0, success_rate: 1, total_tokens: 0, credit: 0 },
  series: [],
  models: [],
  profiles: [],
  health: { credentials: [] },
  storage: {
    logical_bytes: 1024,
    max_bytes: 268435456,
    db_bytes: 32768,
    wal_bytes: 4096,
    shm_bytes: 32768,
    degraded: false,
    dropped_records: 0,
  },
  official_credits: {},
  generated_at: 1789200000,
  range: { days: 7 },
};
