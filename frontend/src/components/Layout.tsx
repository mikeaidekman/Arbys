import { NavLink, Outlet } from "react-router-dom";

const linkCls = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded text-sm font-medium ${
    isActive
      ? "bg-purple-600 text-white"
      : "text-slate-300 hover:bg-slate-800 hover:text-white"
  }`;

export function Layout() {
  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-slate-800 bg-slate-950">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-6">
          <h1 className="text-xl font-semibold text-white tracking-tight">
            🥩 Arbys
          </h1>
          <nav className="flex gap-1">
            <NavLink to="/" end className={linkCls}>
              Opportunities
            </NavLink>
            <NavLink to="/monitored" className={linkCls}>
              Monitored
            </NavLink>
            <NavLink to="/portfolio" className={linkCls}>
              Portfolio
            </NavLink>
            <NavLink to="/admin" className={linkCls}>
              Admin
            </NavLink>
          </nav>
          <div className="ml-auto text-xs text-slate-500">
            Prediction market arbitrage — paper trading
          </div>
        </div>
      </header>
      <main className="flex-1 max-w-7xl w-full mx-auto px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
