import React, { useEffect, useState, useCallback } from 'react';
import {
  GitBranch,
  RefreshCw,
  ExternalLink,
  Key,
  Trash2,
  Plus,
  Play,
} from 'lucide-react';
import { api } from './api.js';
import {
  Signal,
  AgentTrack,
  RuleLedger,
  DiffView,
  StatRow,
  VerdictDot,
  relativeTime,
  shortSha,
} from './components.jsx';

// =========================================================================
// Dashboard: recent runs across every registered repository
// =========================================================================

export function Dashboard({ navigate }) {
  const [runs, setRuns] = useState(null);
  const [stats, setStats] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [runsResult, statsResult] = await Promise.all([
        api.listRuns({ limit: 30 }),
        api.stats(),
      ]);
      setRuns(runsResult.body);
      setStats(statsResult.body);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 15000);
    return () => clearInterval(interval);
  }, [load]);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Recent gate runs</h1>
          <div className="page-subtitle">Every commit AutoHeal Gate has evaluated, newest first.</div>
        </div>
        <div className="toolbar">
          <button className="btn" onClick={load}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </div>

      <StatRow stats={stats} />

      {error ? <div className="panel panel-pad">{error}</div> : null}

      <div className="panel">
        {runs === null ? (
          <div className="empty">Loading…</div>
        ) : runs.length === 0 ? (
          <div className="empty">
            <div className="empty-title">No runs yet</div>
            Send a commit through <code>POST /api/gate/check</code>, or trigger the bundled demo
            workflow, to see it here.
          </div>
        ) : (
          runs.map((run) => (
            <a
              className="run-row"
              key={run.run_id}
              href={`#/runs/${run.run_id}`}
              onClick={(e) => {
                e.preventDefault();
                navigate(`/runs/${run.run_id}`);
              }}
            >
              <VerdictDot verdict={run.verdict} />
              <div>
                <div className="run-repo">{run.repository}</div>
                <div className="run-meta">
                  {shortSha(run.commit_sha)} · {run.branch || 'no branch'}
                </div>
              </div>
              <div className="run-reason">{run.reason}</div>
              <div className="run-risk">risk {run.risk_score}</div>
              <div className="run-time">{relativeTime(run.created_at)}</div>
            </a>
          ))
        )}
      </div>
    </div>
  );
}

// =========================================================================
// Run detail
// =========================================================================

export function RunDetail({ runId, navigate }) {
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [comment, setComment] = useState('');
  const [openPr, setOpenPr] = useState(false);
  const [busy, setBusy] = useState(false);
  const [prResult, setPrResult] = useState(null);

  const load = useCallback(async () => {
    try {
      const result = await api.getRun(runId);
      setRun(result.body);
    } catch (err) {
      setError(err.message);
    }
  }, [runId]);

  useEffect(() => {
    load();
  }, [load]);

  async function decide(decision) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.decide(runId, {
        decision,
        comment: comment || null,
        open_pull_request: decision === 'approve' && openPr,
      });
      if (result.body.pull_request) setPrResult(result.body.pull_request);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="panel panel-pad">{error}</div>;
  if (!run) return <div className="empty">Loading…</div>;

  const artifacts = run.artifacts || {};
  const canDecide = run.hitl_required && run.verdict === 'HOLD';

  return (
    <div>
      <div className="page-header">
        <div>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/')}>
            ← All runs
          </button>
          <h1 className="page-title" style={{ marginTop: 8 }}>
            {run.repository}
          </h1>
          <div className="page-subtitle mono">
            {shortSha(run.commit_sha)} · {run.branch || 'no branch'} · run {run.run_id}
          </div>
        </div>
      </div>

      <Signal verdict={run.verdict} reason={run.reason} />

      <div className="panel panel-pad" style={{ marginTop: 16 }}>
        <div className="section-label">Agent pipeline ({run.engine || 'deterministic'})</div>
        <AgentTrack timeline={artifacts.timeline} />
      </div>

      {run.root_cause ? (
        <div className="panel panel-pad" style={{ marginTop: 16 }}>
          <div className="section-label">Root cause</div>
          <div>{run.root_cause}</div>
        </div>
      ) : null}

      <div className="panel panel-pad" style={{ marginTop: 16 }}>
        <div className="section-label">
          Candidate fix{' '}
          {run.tests_passed
            ? artifacts.validation?.source === 'github-snapshot'
              ? '(validated against a downloaded GitHub snapshot)'
              : '(validated)'
            : run.patch_applied
            ? '(applied, not proven)'
            : ''}
        </div>
        <DiffView patch={run.patch} notes={run.fix_notes} />
      </div>

      <div className="panel panel-pad" style={{ marginTop: 16 }}>
        <div className="section-label">Policy rules</div>
        <RuleLedger rules={(run.policy_evaluation || {}).rules} />
      </div>

      {canDecide ? (
        <div className="panel panel-pad" style={{ marginTop: 16 }}>
          <div className="section-label">Reviewer decision</div>
          <div className="field">
            <label htmlFor="comment">Comment (optional)</label>
            <textarea
              id="comment"
              rows={2}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Why are you approving or rejecting this?"
            />
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13, marginBottom: 14 }}>
            <input type="checkbox" checked={openPr} onChange={(e) => setOpenPr(e.target.checked)} />
            Open a pull request with the validated fix on approval
          </label>
          <div className="toolbar">
            <button className="btn btn-primary" disabled={busy} onClick={() => decide('approve')}>
              Approve
            </button>
            <button className="btn btn-danger" disabled={busy} onClick={() => decide('reject')}>
              Reject
            </button>
          </div>
          {prResult ? (
            <div style={{ marginTop: 12, fontSize: 13 }}>
              {prResult.created ? (
                <a href={prResult.pull_request_url} target="_blank" rel="noreferrer">
                  Pull request opened <ExternalLink size={12} style={{ verticalAlign: 'middle' }} />
                </a>
              ) : (
                <span className="page-subtitle">{prResult.reason}</span>
              )}
            </div>
          ) : null}
        </div>
      ) : run.approvals && run.approvals.length ? (
        <div className="panel panel-pad" style={{ marginTop: 16 }}>
          <div className="section-label">Reviewer decision</div>
          {run.approvals.map((a, i) => (
            <div key={i} style={{ fontSize: 13 }}>
              <strong>@{a.actor}</strong> {a.decision === 'approve' ? 'approved' : 'rejected'} this run
              {a.comment ? ` — "${a.comment}"` : ''}.
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// =========================================================================
// Repositories
// =========================================================================

export function Repositories() {
  const [repos, setRepos] = useState(null);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({
    full_name: '',
    local_path: '',
    test_command: '',
    setup_command: '',
  });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.listRepositories();
      setRepos(result.body.repositories);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function register(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.registerRepository({
        full_name: form.full_name,
        local_path: form.local_path || null,
        test_command: form.test_command || null,
        setup_command: form.setup_command || null,
      });
      setForm({ full_name: '', local_path: '', test_command: '', setup_command: '' });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleApproval(repo) {
    await api.updateRepository(repo.id, {
      policy: { ...repo.policy, require_human_approval: !repo.policy.require_human_approval },
    });
    await load();
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Repositories</h1>
          <div className="page-subtitle">Register a repository so the gate knows its checkout and policy.</div>
        </div>
      </div>

      <div className="panel panel-pad" style={{ marginBottom: 20 }}>
        <div className="section-label">Register a repository</div>
        <form onSubmit={register}>
          <div className="field">
            <label htmlFor="full_name">Repository (owner/name)</label>
            <input
              id="full_name"
              type="text"
              required
              value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })}
              placeholder="acme/payments-api"
            />
          </div>
          <div className="field">
            <label htmlFor="local_path">Local checkout path (optional)</label>
            <input
              id="local_path"
              type="text"
              value={form.local_path}
              onChange={(e) => setForm({ ...form, local_path: e.target.value })}
              placeholder="/srv/checkouts/payments-api"
            />
            <div className="field-hint">
              Used to validate a candidate fix. Without one, AutoHeal downloads the exact commit from
              GitHub instead, so this is optional -- but a local checkout is faster and doesn't need a
              setup command.
            </div>
          </div>
          <div className="field">
            <label htmlFor="test_command">Test command (optional)</label>
            <input
              id="test_command"
              type="text"
              value={form.test_command}
              onChange={(e) => setForm({ ...form, test_command: e.target.value })}
              placeholder="python -m pytest -q"
            />
          </div>
          <div className="field">
            <label htmlFor="setup_command">Setup command (optional)</label>
            <input
              id="setup_command"
              type="text"
              value={form.setup_command}
              onChange={(e) => setForm({ ...form, setup_command: e.target.value })}
              placeholder="pip install -r requirements.txt"
            />
            <div className="field-hint">
              Runs once in the patched workspace before the test command. Matters most without a local
              checkout: a snapshot downloaded from GitHub starts with nothing installed.
            </div>
          </div>
          <button className="btn btn-primary" type="submit" disabled={busy}>
            <Plus size={14} /> Register
          </button>
        </form>
      </div>

      {error ? <div className="panel panel-pad">{error}</div> : null}

      <div className="panel">
        {repos === null ? (
          <div className="empty">Loading…</div>
        ) : repos.length === 0 ? (
          <div className="empty">
            <GitBranch size={22} />
            <div className="empty-title">No repositories registered</div>
            Register one above, or let it register itself the first time a webhook arrives.
          </div>
        ) : (
          repos.map((repo) => (
            <div className="run-row" key={repo.id} style={{ gridTemplateColumns: '1fr auto auto' }}>
              <div>
                <div className="run-repo">{repo.full_name}</div>
                <div className="run-meta">
                  {repo.run_count} run(s) · default branch {repo.default_branch}
                </div>
              </div>
              <div className="run-reason">{repo.local_path || 'no local checkout'}</div>
              <button className="btn btn-sm" onClick={() => toggleApproval(repo)}>
                {repo.policy.require_human_approval ? 'Requires approval' : 'Auto-approves'}
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// =========================================================================
// Settings — API keys for CI runners
// =========================================================================

export function Settings() {
  const [keys, setKeys] = useState(null);
  const [name, setName] = useState('');
  const [revealed, setRevealed] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const result = await api.listApiKeys();
      setKeys(result.body.api_keys);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create(e) {
    e.preventDefault();
    setError(null);
    try {
      const result = await api.createApiKey(name || 'CI runner');
      setRevealed(result.body);
      setName('');
      await load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function revoke(id) {
    await api.revokeApiKey(id);
    await load();
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">CI API keys</h1>
          <div className="page-subtitle">
            A pipeline can't complete a browser sign-in, so it authenticates with a key instead. A key
            inherits your GitHub token at the moment it is issued.
          </div>
        </div>
      </div>

      <div className="panel panel-pad" style={{ marginBottom: 20 }}>
        <div className="section-label">Issue a new key</div>
        <form onSubmit={create} style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label htmlFor="key-name">Name</label>
            <input
              id="key-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="github-actions-prod"
            />
          </div>
          <button className="btn btn-primary" type="submit">
            <Key size={14} /> Issue key
          </button>
        </form>

        {revealed ? (
          <div style={{ marginTop: 14 }}>
            <div className="field-hint" style={{ marginBottom: 6 }}>
              {revealed.warning} Put it in your CI secret store now.
            </div>
            <div className="key-reveal">{revealed.api_key}</div>
          </div>
        ) : null}
      </div>

      {error ? <div className="panel panel-pad">{error}</div> : null}

      <div className="panel">
        {keys === null ? (
          <div className="empty">Loading…</div>
        ) : keys.length === 0 ? (
          <div className="empty">No API keys yet.</div>
        ) : (
          keys.map((key) => (
            <div className="run-row" key={key.id} style={{ gridTemplateColumns: '1fr auto auto' }}>
              <div>
                <div className="run-repo mono">{key.prefix}…</div>
                <div className="run-meta">{key.name}</div>
              </div>
              <div className="run-time">
                {key.revoked
                  ? 'revoked'
                  : key.last_used_at
                  ? `used ${relativeTime(key.last_used_at)}`
                  : 'never used'}
              </div>
              {!key.revoked ? (
                <button className="btn btn-sm btn-danger" onClick={() => revoke(key.id)}>
                  <Trash2 size={13} /> Revoke
                </button>
              ) : (
                <span />
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// =========================================================================
// Demo — run the bundled sample-repo through the gate from the browser
// =========================================================================

export function Demo() {
  const [scenario, setScenario] = useState('test_failure');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function run() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await api.runDemoCheck({
        repository: 'autoheal/sample-repo',
        commit_sha: `demo${Date.now().toString(16)}`.padEnd(40, '0'),
        branch: 'demo',
        ci_passed: scenario === 'success',
        failure_logs:
          scenario === 'success'
            ? ''
            : 'COMMAND: pytest -q\nEXIT CODE: 1\n\nFAILED tests/test_calculator.py::test_add - assert -1 == 5\nE   assert -1 == 5\n',
      });
      setResult(response.body);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Try the gate</h1>
          <div className="page-subtitle">
            Runs the bundled sample-repo scenario straight from the browser, no CI required.
          </div>
        </div>
      </div>

      <div className="panel panel-pad">
        <div className="field">
          <label htmlFor="scenario">Scenario</label>
          <select id="scenario" value={scenario} onChange={(e) => setScenario(e.target.value)}>
            <option value="test_failure">Failing test (arithmetic bug)</option>
            <option value="success">Passing build</option>
          </select>
        </div>
        <button className="btn btn-primary" onClick={run} disabled={busy}>
          <Play size={14} /> {busy ? 'Running…' : 'Run through the gate'}
        </button>
      </div>

      {error ? (
        <div className="panel panel-pad" style={{ marginTop: 16 }}>
          {error}
        </div>
      ) : null}

      {result ? (
        <div style={{ marginTop: 16 }}>
          <Signal verdict={result.verdict} reason={result.reason} />
          <div className="panel panel-pad" style={{ marginTop: 16 }}>
            <div className="section-label">Agent pipeline ({result.engine})</div>
            <AgentTrack timeline={result.timeline} />
          </div>
          <div className="panel panel-pad" style={{ marginTop: 16 }}>
            <div className="section-label">Candidate fix</div>
            <DiffView patch={result.patch} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

// =========================================================================
// System — live backend health, durable jobs and provider capabilities
// =========================================================================

export function SystemStatus() {
  const [health, setHealth] = useState(null);
  const [jobs, setJobs] = useState(null);
  const [providers, setProviders] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [healthResult, jobsResult, providersResult] = await Promise.all([
        api.health(),
        api.listJobs(40),
        api.providers(),
      ]);
      setHealth(healthResult.body);
      setJobs(jobsResult.body.jobs || []);
      setProviders(providersResult.body);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [load]);

  const statusClass = health?.status === 'ok' ? 'pass' : 'hold';
  const counts = (jobs || []).reduce((acc, job) => {
    acc[job.status] = (acc[job.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">System status</h1>
          <div className="page-subtitle">Live runtime health, durable jobs and provider capabilities.</div>
        </div>
        <button className="btn" onClick={load}><RefreshCw size={14} /> Refresh</button>
      </div>

      {error ? <div className="panel panel-pad" style={{ marginBottom: 16 }}>{error}</div> : null}

      <div className="system-grid">
        <div className="panel panel-pad">
          <div className="section-label">Runtime</div>
          <div className="system-status-line">
            <span className={`dot ${statusClass}`} />
            <strong>{health?.status || 'loading'}</strong>
          </div>
          <div className="system-list">
            <div><span>Version</span><span className="mono">{health?.version || '—'}</span></div>
            <div><span>Database</span><span>{health?.database?.ok ? 'healthy' : 'degraded'}</span></div>
            <div><span>Queue</span><span>{health?.queue?.started ? `${health.queue.workers} workers` : 'stopped'}</span></div>
            <div><span>Pending</span><span>{health?.queue?.pending ?? '—'}</span></div>
            <div><span>Tracing</span><span>{health?.tracing?.enabled ? 'enabled' : 'disabled'}</span></div>
          </div>
        </div>

        <div className="panel panel-pad">
          <div className="section-label">Durable jobs</div>
          <div className="job-counts">
            <span><strong>{counts.QUEUED || 0}</strong> queued</span>
            <span><strong>{counts.RUNNING || 0}</strong> running</span>
            <span><strong>{counts.RETRYING || 0}</strong> retrying</span>
            <span><strong>{counts.DEAD_LETTER || 0}</strong> dead-letter</span>
          </div>
          <div className="field-hint">Jobs survive worker restarts and are claimed atomically by workers.</div>
        </div>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-pad" style={{ paddingBottom: 10 }}>
          <div className="section-label">Recent durable jobs</div>
        </div>
        {jobs === null ? <div className="empty">Loading…</div> : jobs.length === 0 ? <div className="empty">No durable jobs yet.</div> : jobs.map((job) => (
          <div className="run-row system-job-row" key={job.id}>
            <div>
              <div className="run-repo mono">{job.id}</div>
              <div className="run-meta">{job.provider} · attempt {job.attempts}/{job.max_attempts}</div>
            </div>
            <span className="badge pending">{job.status}</span>
            <div className="run-time">{job.event_key || 'no event key'}</div>
          </div>
        ))}
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-pad" style={{ paddingBottom: 10 }}>
          <div className="section-label">Provider capabilities</div>
          <div className="field-hint">Capabilities describe implemented operations only. Credentials are never returned.</div>
        </div>
        {providers ? (
          <div className="provider-table-wrap">
            <table className="provider-table">
              <thead><tr><th>Provider</th><th>Role</th><th>Implemented capabilities</th></tr></thead>
              <tbody>
                {['source', 'cicd'].flatMap((role) => Object.entries(providers[role] || {}).map(([name, data]) => (
                  <tr key={`${role}-${name}`}>
                    <td className="mono">{name}</td>
                    <td>{role.toUpperCase()}</td>
                    <td>{Object.entries(data.capabilities || {}).filter(([, enabled]) => enabled).map(([cap]) => cap).join(' · ') || 'none'}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty">Loading…</div>}
      </div>
    </div>
  );
}
