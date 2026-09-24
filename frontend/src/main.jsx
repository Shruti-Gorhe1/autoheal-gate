import React, { useEffect, useState, useCallback } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ShieldCheck,
  LayoutList,
  GitBranch,
  Key,
  FlaskConical,
  Activity,
  LogOut,
} from 'lucide-react';
import './styles.css';
import { api } from './api.js';
import { Dashboard, RunDetail, Repositories, Settings, Demo, SystemStatus } from './pages.jsx';

// A tiny hash router. The dashboard has five screens; a routing library
// would be pure overhead here.
function useRoute() {
  const [route, setRoute] = useState(window.location.hash.slice(1) || '/');

  useEffect(() => {
    const onChange = () => setRoute(window.location.hash.slice(1) || '/');
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const navigate = useCallback((path) => {
    window.location.hash = path;
  }, []);

  return [route, navigate];
}

function LoginScreen() {
  return (
    <div className="login-screen">
      <div className="panel login-card">
        <div className="login-signal" />
        <h1 className="login-title">AutoHeal Gate</h1>
        <div className="login-copy">
          Sign in with GitHub to diagnose failing pipelines, review proposed fixes, and govern what
          gets released.
        </div>
        <a
          className="btn btn-primary"
          href={api.loginUrl(window.location.origin + window.location.pathname)}
        >
          Continue with GitHub
        </a>
      </div>
    </div>
  );
}

const NAV_ITEMS = [
  { path: '/', label: 'Runs', icon: LayoutList },
  { path: '/repositories', label: 'Repositories', icon: GitBranch },
  { path: '/demo', label: 'Try the gate', icon: FlaskConical },
  { path: '/settings', label: 'API keys', icon: Key },
  { path: '/system', label: 'System', icon: Activity },
];

function Sidebar({ route, navigate, user, onLogout }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <ShieldCheck size={17} />
        </div>
        <div>
          <div className="brand-name">AutoHeal Gate</div>
          <div className="brand-sub">Release governance</div>
        </div>
      </div>

      <nav className="nav">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = route === item.path || (item.path !== '/' && route.startsWith(item.path));
          return (
            <button
              key={item.path}
              className={`nav-item ${active ? 'active' : ''}`}
              onClick={() => navigate(item.path)}
            >
              <Icon size={16} />
              {item.label}
            </button>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        {user ? (
          <>
            <div className="user-chip">
              {user.avatar_url ? <img src={user.avatar_url} alt="" /> : null}
              <span className="login">@{user.login}</span>
            </div>
            <button className="nav-item" onClick={onLogout}>
              <LogOut size={16} /> Sign out
            </button>
          </>
        ) : null}
      </div>
    </aside>
  );
}

function App() {
  const [route, navigate] = useRoute();
  const [session, setSession] = useState(null); // null = loading

  useEffect(() => {
    api
      .session()
      .then((result) => setSession(result.body))
      .catch(() => setSession({ authenticated: false }));
  }, []);

  async function logout() {
    await api.logout();
    setSession({ authenticated: false });
  }

  if (session === null) {
    return <div className="empty">Loading…</div>;
  }

  if (!session.authenticated) {
    return <LoginScreen />;
  }

  let page;
  if (route === '/' || route === '') {
    page = <Dashboard navigate={navigate} />;
  } else if (route.startsWith('/runs/')) {
    page = <RunDetail runId={route.slice('/runs/'.length)} navigate={navigate} />;
  } else if (route === '/repositories') {
    page = <Repositories />;
  } else if (route === '/settings') {
    page = <Settings />;
  } else if (route === '/demo') {
    page = <Demo />;
  } else if (route === '/system') {
    page = <SystemStatus />;
  } else {
    page = <Dashboard navigate={navigate} />;
  }

  return (
    <div className="app-shell">
      <Sidebar route={route} navigate={navigate} user={session.user} onLogout={logout} />
      <main className="content">{page}</main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
