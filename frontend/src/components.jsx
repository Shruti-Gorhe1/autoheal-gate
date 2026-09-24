import React from 'react';
import { CheckCircle2, PauseCircle, XCircle, HelpCircle } from 'lucide-react';

const VERDICT_META = {
  PASS: { className: 'pass', label: 'PASS', icon: CheckCircle2 },
  HOLD: { className: 'hold', label: 'HOLD', icon: PauseCircle },
  BLOCK: { className: 'block', label: 'BLOCK', icon: XCircle },
  PENDING: { className: 'pending', label: 'PENDING', icon: HelpCircle },
};

function verdictMeta(verdict) {
  return VERDICT_META[verdict] || VERDICT_META.PENDING;
}

export function VerdictDot({ verdict }) {
  return <span className={`dot ${verdictMeta(verdict).className}`} />;
}

export function VerdictBadge({ verdict }) {
  const meta = verdictMeta(verdict);
  return <span className={`badge ${meta.className}`}>{meta.label}</span>;
}

/** The hero of a run page: one signal light, plain language underneath. */
export function Signal({ verdict, reason }) {
  const meta = verdictMeta(verdict);
  const Icon = meta.icon;
  return (
    <div className="panel signal">
      <div className={`signal-lamp ${meta.className}`}>
        <Icon size={28} />
      </div>
      <div className="signal-body">
        <div className="signal-verdict">{meta.label}</div>
        {reason ? <div className="signal-reason">{reason}</div> : null}
      </div>
    </div>
  );
}

const STAGE_LABELS = {
  triage: 'Triage',
  retrieval: 'Knowledge',
  root_cause: 'Root cause',
  fix: 'Fix',
  validation: 'Validation',
};

/** The agent pipeline as block sections along a line — a real sequence. */
export function AgentTrack({ timeline = [] }) {
  const stages = Object.keys(STAGE_LABELS).map((key) => {
    const entry = timeline.find((t) => t.agent === key);
    return { key, entry };
  });

  return (
    <div className="track">
      {stages.map((stage, index) => {
        const status = stage.entry ? stage.entry.status : 'skip';
        const markerClass = status === 'ok' ? 'ok' : status === 'error' ? 'error' : 'skip';
        return (
          <div className="track-stage" key={stage.key}>
            <div className="track-node" title={stage.entry ? stage.entry.summary : 'Skipped'}>
              <div className={`track-marker ${markerClass}`}>
                {markerClass === 'ok' ? '✓' : markerClass === 'error' ? '!' : index + 1}
              </div>
              <div className="track-label">{STAGE_LABELS[stage.key]}</div>
            </div>
            {index < stages.length - 1 ? (
              <div className={`track-rail ${markerClass === 'ok' ? 'ok' : ''}`} />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** The rules that produced the verdict — a checked ledger, not a card grid. */
export function RuleLedger({ rules = [] }) {
  if (!rules.length) {
    return <div className="empty">No rules were evaluated for this run.</div>;
  }
  return (
    <div className="ledger">
      {rules.map((rule) => {
        const mark = rule.passed ? '✓' : rule.verdict === 'HOLD' ? '‖' : '✕';
        const cls = rule.passed ? 'pass' : rule.verdict === 'HOLD' ? 'hold' : 'block';
        return (
          <div className="ledger-row" key={rule.rule}>
            <div className={`ledger-mark ${cls}`}>{mark}</div>
            <div className="ledger-rule">{rule.rule}</div>
            <div className="ledger-message">{rule.message}</div>
            <div className="ledger-weight">{rule.weight ? `+${rule.weight}` : ''}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Minimal unified-diff renderer. No syntax highlighting library needed. */
export function DiffView({ patch, notes }) {
  if (!patch || !patch.trim()) {
    return (
      <div className="empty">
        <div className="empty-title">No candidate fix was produced</div>
        {notes || 'The evidence did not reduce to a safe, specific change.'}
      </div>
    );
  }
  const lines = patch.split('\n');
  return (
    <div className="diff">
      {lines.map((line, i) => {
        let cls = '';
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'remove';
        else if (line.startsWith('@@')) cls = 'hunk';
        else if (line.startsWith('diff --git') || line.startsWith('index ')) cls = 'meta';
        return (
          <div className={`diff-line ${cls}`} key={i}>
            {line || ' '}
          </div>
        );
      })}
    </div>
  );
}

export function StatRow({ stats }) {
  if (!stats) return null;
  const cells = [
    { label: 'Total runs', value: stats.total ?? 0 },
    { label: 'Auto-healed', value: stats.auto_healed ?? 0 },
    { label: 'Awaiting review', value: stats.awaiting_review ?? 0 },
    { label: 'Avg. risk score', value: stats.average_risk_score ?? 0 },
    {
      label: 'Avg. duration',
      value: stats.average_duration_ms ? `${Math.round(stats.average_duration_ms / 1000)}s` : '—',
    },
  ];
  return (
    <div className="stat-row">
      {cells.map((cell) => (
        <div className="stat-cell" key={cell.label}>
          <div className="stat-value">{cell.value}</div>
          <div className="stat-label">{cell.label}</div>
        </div>
      ))}
    </div>
  );
}

export function relativeTime(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  const diff = Math.max(0, Date.now() - then);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return 'just now';
  if (diff < hour) return `${Math.floor(diff / minute)}m ago`;
  if (diff < day) return `${Math.floor(diff / hour)}h ago`;
  return `${Math.floor(diff / day)}d ago`;
}

export function shortSha(sha) {
  return sha ? sha.slice(0, 8) : '';
}
