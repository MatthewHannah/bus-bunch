import { NavLink, Outlet } from 'react-router-dom';

export default function Layout() {
  return (
    <>
      <header className="app-header">
        <h1>bus-bunch · viz</h1>
        <nav>
          <NavLink to="/marey">Marey</NavLink>
          <NavLink to="/vehicle-track">Vehicle track</NavLink>
          <NavLink to="/prediction-error">Prediction error</NavLink>
          <NavLink to="/prediction-evolution">Prediction evolution</NavLink>
        </nav>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </>
  );
}
