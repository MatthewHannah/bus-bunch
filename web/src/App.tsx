import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import HomePage from './pages/HomePage';
import MareyPage from './pages/MareyPage';
import VehicleTrackPage from './pages/VehicleTrackPage';
import PredictionErrorPage from './pages/PredictionErrorPage';
import PredictionEvolutionPage from './pages/PredictionEvolutionPage';

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/marey" element={<MareyPage />} />
        <Route path="/vehicle-track" element={<VehicleTrackPage />} />
        <Route path="/prediction-error" element={<PredictionErrorPage />} />
        <Route path="/prediction-evolution" element={<PredictionEvolutionPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
