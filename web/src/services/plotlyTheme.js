// Light/dark (Apple-style) theme support for Plotly outputs. Pure module (no imports).
//
// Kernel figures are baked with the plotly_dark template, so their axis/tick/legend text
// is near-white on an opaque near-black paper. In light and dark, this script runs inside
// the sandboxed iframe and re-themes a figure ONLY when its resolved font color is light
// (i.e. it was made for a dark page): transparent paper/plot, theme text/grid colors.
// Trace colors are never touched. Any failure leaves the chart exactly as rendered.
// Ember keeps today's output (no script).
const retheme = (p) =>
  '\n<script>(function(){' +
  'function lum(c){try{c=String(c||"").trim();var r,g,b,m;' +
  'if(/^#[0-9a-f]{3}$/i.test(c)){r=parseInt(c[1]+c[1],16);g=parseInt(c[2]+c[2],16);b=parseInt(c[3]+c[3],16);}' +
  'else if(/^#[0-9a-f]{6}$/i.test(c)){r=parseInt(c.slice(1,3),16);g=parseInt(c.slice(3,5),16);b=parseInt(c.slice(5,7),16);}' +
  'else if((m=c.match(/^rgba?\\(\\s*([\\d.]+)\\s*,\\s*([\\d.]+)\\s*,\\s*([\\d.]+)/i))){r=+m[1];g=+m[2];b=+m[3];}' +
  'else{return -1;}' +
  'function ch(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);}' +
  'return 0.2126*ch(r)+0.7152*ch(g)+0.0722*ch(b);}catch(e){return -1;}}' +
  'function patch(gd){try{' +
  'if(gd.__ltThemed||typeof Plotly==="undefined")return;var fl=gd._fullLayout;if(!fl)return;' +
  'if(!(lum(fl.font&&fl.font.color)>0.5))return;gd.__ltThemed=true;' +
  'var u={"paper_bgcolor":"rgba(0,0,0,0)","plot_bgcolor":"rgba(0,0,0,0)",' +
  `"font.color":"${p.text}","title.font.color":"${p.text}","legend.font.color":"${p.secondary}",` +
  `"hoverlabel.bgcolor":"${p.hoverBg}","hoverlabel.font.color":"${p.text}","hoverlabel.bordercolor":"${p.hoverBorder}",` +
  `"modebar.bgcolor":"${p.modebarBg}","modebar.color":"${p.modebar}","modebar.activecolor":"${p.accent}"};` +
  'Object.keys(fl).forEach(function(k){if(/^[xy]axis\\d*$/.test(k)){' +
  `u[k+".gridcolor"]="${p.grid}";u[k+".linecolor"]="${p.line}";` +
  `u[k+".zerolinecolor"]="${p.zero}";u[k+".tickfont.color"]="${p.secondary}";` +
  `u[k+".title.font.color"]="${p.secondary}";}});` +
  `var p=Plotly.relayout(gd,u);if(p&&p.catch)p.catch(function(e){console.debug("${p.label}",e);});` +
  `}catch(e){console.debug("${p.label}",e);}}` +
  'function watch(gd){var tries=0;' +
  'try{if(gd.on)gd.on("plotly_afterplot",function(){patch(gd);});}catch(e){}' +
  '(function poll(){if(gd.__ltThemed)return;if(gd._fullLayout){patch(gd);return;}' +
  'if(++tries<20)setTimeout(poll,100);})();}' +
  'function start(){try{var gs=document.querySelectorAll(".plotly-graph-div");' +
  `for(var i=0;i<gs.length;i++)watch(gs[i]);}catch(e){console.debug("${p.label}",e);}}` +
  'if(document.readyState==="complete")start();else window.addEventListener("load",start);' +
  '})();</script>'
// Values mirror the theme tokens (theme.css): text / secondary text / elevated surface /
// hairlines / text-safe accent.
export const PLOTLY_LIGHT_RETHEME = retheme({
  label: 'plotly light retheme', text: '#1d1d1f', secondary: '#424245',
  hoverBg: '#ffffff', hoverBorder: 'rgba(0,0,0,0.12)',
  modebarBg: 'rgba(255,255,255,0.7)', modebar: '#6e6e73', accent: '#0066cc',
  grid: 'rgba(0,0,0,0.08)', line: 'rgba(0,0,0,0.25)', zero: 'rgba(0,0,0,0.18)',
})
export const PLOTLY_DARK_RETHEME = retheme({
  label: 'plotly dark retheme', text: '#f5f5f7', secondary: '#d1d1d6',
  hoverBg: '#2c2c2e', hoverBorder: 'rgba(255,255,255,0.12)',
  modebarBg: 'rgba(28,28,30,0.7)', modebar: '#aeaeb2', accent: '#64aeff',
  grid: 'rgba(255,255,255,0.08)', line: 'rgba(255,255,255,0.25)', zero: 'rgba(255,255,255,0.18)',
})
const RETHEME = { light: PLOTLY_LIGHT_RETHEME, dark: PLOTLY_DARK_RETHEME }
// Returns the iframe srcDoc: `html` plus the height reporter (and, in light/dark, the
// re-theme script), inserted before </body> or appended. For ember (and any other theme)
// the output is byte-identical to the previous inline logic in CellOutput.jsx.
export function buildPlotlySrcDoc(html, theme, heightReporter) {
  const inject = Object.hasOwn(RETHEME, theme) ? heightReporter + RETHEME[theme] : heightReporter
  return html.includes('</body>')
    ? html.replace('</body>', inject + '</body>')
    : html + inject
}
