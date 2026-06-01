import Plotly from 'plotly.js-dist-min';
import createPlotlyComponent from 'react-plotly.js/factory';

// react-plotly.js' default import bundles the FULL plotly.js build (~3MB
// gzipped). Use the factory with the dist-min build instead to cut bundle
// size roughly in half. dist-min still includes scattermap so all four
// plots in this app work.
const Plot = createPlotlyComponent(Plotly);
export default Plot;
