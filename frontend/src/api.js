// Thin fetch wrapper. Cookies carry the session, so every call uses
// credentials: 'include'. A 401 anywhere means "not signed in", which the
// caller can treat uniformly.

export const API_BASE =
  (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL) ||
  'http://localhost:8000';

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });

  const contentType = response.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await response.json() : await response.text();

  if (!response.ok && response.status !== 409) {
    const message = (body && body.detail) || `Request to ${path} failed (${response.status}).`;
    throw new ApiError(message, response.status, body);
  }

  return { status: response.status, body };
}

export const api = {
  session: () => request('/api/auth/session'),
  me: () => request('/api/auth/me'),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  loginUrl: (redirectTo) =>
    `${API_BASE}/api/auth/github/login?redirect_to=${encodeURIComponent(redirectTo)}`,

  listApiKeys: () => request('/api/auth/api-keys'),
  createApiKey: (name) =>
    request('/api/auth/api-keys', { method: 'POST', body: JSON.stringify({ name }) }),
  revokeApiKey: (id) => request(`/api/auth/api-keys/${id}`, { method: 'DELETE' }),

  health: () => request('/api/health'),
  listJobs: (limit = 50) => request(`/api/system/jobs?limit=${limit}`),
  getJob: (id) => request(`/api/system/jobs/${id}`),
  providers: () => request('/api/system/providers'),

  listRepositories: () => request('/api/gate/repositories'),
  registerRepository: (payload) =>
    request('/api/gate/repositories', { method: 'POST', body: JSON.stringify(payload) }),
  updateRepository: (id, payload) =>
    request(`/api/gate/repositories/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),

  listRuns: (params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request(`/api/gate/runs${query ? `?${query}` : ''}`);
  },
  getRun: (id) => request(`/api/gate/runs/${id}`),
  decide: (id, payload) =>
    request(`/api/gate/runs/${id}/decision`, { method: 'POST', body: JSON.stringify(payload) }),

  stats: (repository) =>
    request(`/api/gate/stats${repository ? `?repository=${encodeURIComponent(repository)}` : ''}`),

  defaultPolicy: () => request('/api/gate/policy/default'),
  simulatePolicy: (evidence, overrides) =>
    request('/api/gate/policy/simulate', {
      method: 'POST',
      body: JSON.stringify({ evidence, overrides }),
    }),

  runDemoCheck: (payload) =>
    request('/api/gate/check', { method: 'POST', body: JSON.stringify(payload) }),
};

export { ApiError };
