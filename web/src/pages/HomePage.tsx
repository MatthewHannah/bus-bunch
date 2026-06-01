import { Link } from 'react-router-dom';

export default function HomePage() {
  return (
    <>
      <h2 className="page-title">bus-bunch visualizations</h2>
      <p className="page-blurb">
        On-demand plots over the MARTA realtime data captured by the{' '}
        <code>BusBunch.Functions</code> Function App. Each page below mirrors
        the corresponding Python script under <code>scripts/viz/</code>.
      </p>

      <ul>
        <li>
          <Link to="/marey">Marey (string-line) diagram</Link> — every bus run
          on a route + direction over the last N hours.
        </li>
        <li>
          <Link to="/vehicle-track">Vehicle track</Link> — map trail for a
          single bus (by <code>vehicle_id</code>) with a snapshot scrubber.
        </li>
        <li>
          <Link to="/prediction-error">Prediction error</Link> — how MARTA's
          ETAs for each trip diverged from the eventual arrival, at a
          chosen stop and ET window. Requires the route to be in{' '}
          <code>dbo.tracked_route</code>.
        </li>
        <li>
          <Link to="/prediction-evolution">Prediction evolution</Link> — same
          stop / window, but anchored on the ETA itself with reference
          lines for the observed and scheduled arrivals.
        </li>
      </ul>
    </>
  );
}
