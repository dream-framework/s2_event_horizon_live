const DATA_URL = 'data/news_s2.json';
const REFRESH_MS = 5 * 60 * 1000;

const state = {
  data: null,
  selectedTopic: null,
  view: 'retention',
  sort: 'articles',
  signal: localStorage.getItem('dream-news-signal') || 'corrected',
  theme: localStorage.getItem('dream-news-theme') || 'dark',
  chart: null,
  lastDataStamp: null
};

const els = {
  statusDot: document.getElementById('status-dot'),
  lastUpdated: document.getElementById('last-updated'),
  statTopics: document.getElementById('stat-topics'),
  statArticles: document.getElementById('stat-articles'),
  statSources: document.getElementById('stat-sources'),
  statErrors: document.getElementById('stat-errors'),
  topicSelect: document.getElementById('topic-select'),
  viewSelect: document.getElementById('view-select'),
  signalSelect: document.getElementById('signal-select'),
  sortSelect: document.getElementById('sort-select'),
  themeBtn: document.getElementById('theme-btn'),
  refreshBtn: document.getElementById('refresh-btn'),
  chartTitle: document.getElementById('chart-title'),
  phaseBadge: document.getElementById('phase-badge'),
  chart: document.getElementById('chart'),
  chartNote: document.getElementById('chart-note'),
  chartLegend: document.getElementById('chart-legend'),
  metricsStrip: document.getElementById('metrics-strip'),
  topicTable: document.querySelector('#topic-table tbody'),
  topicBoard: document.getElementById('topic-board'),
  storyList: document.getElementById('story-list'),
  storyCount: document.getElementById('story-count'),
  storyTemplate: document.getElementById('story-template'),
  sourceList: document.getElementById('source-list'),
  sourceCount: document.getElementById('source-count')
};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartColors() {
  return {
    text: cssVar('--text') || '#edf4ff',
    muted: cssVar('--muted') || '#9aa8c3',
    line: cssVar('--line') || 'rgba(255,255,255,.12)',
    accent: cssVar('--accent') || '#7dd3fc',
    accent2: cssVar('--accent-2') || '#a78bfa',
    warn: cssVar('--warn') || '#fde68a',
    bad: cssVar('--bad') || '#fda4af',
    good: cssVar('--good') || '#86efac'
  };
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  els.themeBtn.textContent = `Theme: ${state.theme === 'dark' ? 'Dark' : 'Light'}`;
  localStorage.setItem('dream-news-theme', state.theme);
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function fmtHours(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  if (value >= 48) return `${fmtNumber(value / 24, 1)}d`;
  return `${fmtNumber(value, 1)}h`;
}

function fmtDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function ageLabel(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (Number.isNaN(diff)) return '-';
  const mins = Math.max(0, Math.round(diff / 60000));
  if (mins < 90) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Math.round(Number(value) * 100)}%`;
}

function retention(x, tau, beta) {
  if (!tau || !beta) return null;
  return Math.exp(-Math.pow(Math.max(0, x) / tau, beta));
}

function halfLife(tau, beta) {
  if (!tau || !beta) return null;
  return tau * Math.pow(Math.log(2), 1 / beta);
}

function clampSignal(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return null;
  return Math.max(0, Math.min(1.5, Number(v)));
}

function observedValue(point, mode = state.signal) {
  if (!point) return null;
  const preferred = mode === 'raw' ? point.observed_raw : point.observed_corrected;
  const fallback = mode === 'raw' ? point.observed : (point.observed_corrected ?? point.observed);
  return clampSignal(preferred ?? fallback);
}

function fitValue(point, topic) {
  const tau = topic?.fit?.tau_hours;
  const beta = topic?.fit?.beta;
  return retention(point?.x_hours || 0, tau, beta) ?? clampSignal(point?.fit) ?? 0;
}

function topicDust(topic) {
  const values = topic?.series || [];
  const late = values.slice(Math.floor(values.length / 2)).filter(d => observedValue(d) !== null);
  if (!late.length) return topic?.residual_dust;
  const rms = Math.sqrt(late.reduce((s, d) => s + Math.pow(observedValue(d) - fitValue(d, topic), 2), 0) / late.length);
  return rms;
}

function topicNewest(topic) {
  const articles = state.data?.articles || [];
  const matches = articles.filter(a => a.topic === topic.key);
  if (!matches.length) return null;
  return matches.map(a => new Date(a.published_at).getTime()).filter(Number.isFinite).sort((a,b) => b - a)[0];
}

function articleXHours(article, topic) {
  if (!article?.published_at || topic?.peak_bin_index == null || !state.data?.generated_at) return null;

  const generated = new Date(state.data.generated_at).getTime();
  const published = new Date(article.published_at).getTime();
  if (!Number.isFinite(generated) || !Number.isFinite(published)) return null;

  const windowHours = state.data.window_hours || 168;
  const binHours = state.data.bin_hours || 3;
  const start = generated - windowHours * 3600 * 1000;
  const idx = Math.floor((published - start) / (binHours * 3600 * 1000));

  if (!Number.isFinite(idx)) return null;
  return Math.max(0, (idx - topic.peak_bin_index) * binHours);
}

function nearestSeriesPoint(topic, xHours) {
  const series = topic?.series || [];
  if (!series.length || xHours == null) return null;
  return series.reduce((best, point) => {
    if (!best) return point;
    return Math.abs(Number(point.x_hours) - xHours) < Math.abs(Number(best.x_hours) - xHours)
      ? point
      : best;
  }, null);
}

function storyStickiness(article, topic) {
  const fit = topic?.fit || {};
  const formal = fit.fit_status === 'formal';

  if (!formal) {
    return {
      score: 0,
      residual: null,
      expected: null,
      xHours: articleXHours(article, topic),
      postLambda: false,
      role: 'latest activity'
    };
  }

  const xHours = articleXHours(article, topic);
  const point = nearestSeriesPoint(topic, xHours);
  const expected = point ? fitValue(point, topic) : null;
  const observed = point ? observedValue(point) : null;
  const residual = observed == null || expected == null ? 0 : Math.max(0, observed - expected);
  const tau = Number(fit.tau_hours) || 0;
  const postLambda = tau > 0 && xHours != null && xHours >= tau;

  const ageWeight = tau > 0 && xHours != null
    ? 0.45 + 0.55 * Math.min(1, xHours / tau)
    : 0.45;

  const postBonus = postLambda && residual > 0 ? 0.18 : 0;
  const score = Math.round(100 * Math.min(1, residual * 2.2 * ageWeight + postBonus));

  let role = 'decays with baseline';
  if (score > 0 && postLambda) role = 'post-λq survivor';
  else if (score > 0) role = 'positive S2 residual';

  return {
    score,
    residual,
    expected,
    xHours,
    postLambda,
    role
  };
}

function topicStickiness(topic) {
  const articles = state.data?.articles || [];
  const scores = articles
    .filter(article => article.topic === topic.key)
    .map(article => storyStickiness(article, topic).score)
    .sort((a, b) => b - a);

  if (!scores.length) return 0;
  const top = scores.slice(0, 5);
  return top.reduce((sum, value) => sum + value, 0) / top.length;
}

function getTopicsSorted() {
  const topics = [...(state.data?.topics || [])];
  topics.sort((a, b) => {
    if (state.sort === 'stickiness') return topicStickiness(b) - topicStickiness(a);
    if (state.sort === 'lambda') return (b.fit?.tau_hours || 0) - (a.fit?.tau_hours || 0);
    if (state.sort === 'dust') return (topicDust(b) || 0) - (topicDust(a) || 0);
    if (state.sort === 'fit') return (b.fit?.log_r2 || -10) - (a.fit?.log_r2 || -10);
    if (state.sort === 'recent') return (topicNewest(b) || 0) - (topicNewest(a) || 0);
    return (b.article_count || 0) - (a.article_count || 0);
  });
  return topics;
}

function selectedTopic() {
  const topics = getTopicsSorted();
  if (!topics.length) return null;
  return topics.find(t => t.key === state.selectedTopic) || topics[0];
}

async function loadData({ quiet = false } = {}) {
  try {
    if (!quiet) els.lastUpdated.textContent = 'Loading live JSON...';
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const nextData = await response.json();
    const nextStamp = nextData.generated_at || nextData.updated_at || JSON.stringify(nextData.summary || {});
    if (quiet && nextStamp === state.lastDataStamp) return;
    state.lastDataStamp = nextStamp;
    state.data = nextData;
    const topics = state.data.topics || [];
    if (!state.selectedTopic || !topics.some(t => t.key === state.selectedTopic)) {
      state.selectedTopic = topics[0]?.key || null;
    }
    els.statusDot.className = 'status-dot ok';
    render();
  } catch (err) {
    els.statusDot.className = 'status-dot err';
    els.lastUpdated.textContent = `Data load failed: ${err.message}`;
  }
}

function render() {
  if (!state.data) return;
  const errors = state.data.summary?.fetch_errors || [];
  els.lastUpdated.textContent = `updated ${fmtDate(state.data.generated_at)}`;
  els.statTopics.textContent = (state.data.topics || []).length;
  els.statArticles.textContent = state.data.summary?.article_count ?? '-';
  els.statSources.textContent = state.data.summary?.source_count ?? '-';
  els.statErrors.textContent = errors.length;
  renderTopicOptions();
  renderSelected();
  renderTopicBoard();
  renderTopicTable();
  renderStories();
  renderSources();
}

function renderTopicOptions() {
  const topics = getTopicsSorted();
  els.topicSelect.innerHTML = '';
  topics.forEach(topic => {
    const opt = document.createElement('option');
    opt.value = topic.key;
    opt.textContent = `${topic.label} (${topic.article_count})`;
    els.topicSelect.appendChild(opt);
  });
  els.topicSelect.value = state.selectedTopic || topics[0]?.key || '';
  els.viewSelect.value = state.view;
  els.signalSelect.value = state.signal;
  els.sortSelect.value = state.sort;
}

function renderSelected() {
  const topic = selectedTopic();
  if (!topic) return;
  state.selectedTopic = topic.key;
  const fit = topic.fit || {};
  const bias = topic.circadian_bias;
  els.phaseBadge.textContent = topic.phase || '-';
  els.metricsStrip.innerHTML = [
    ['lambda_q / tau', fmtHours(fit.tau_hours), 'coherence cliff'],
    ['D_eff / beta', fmtNumber(fit.beta, 2), (fit.beta || 0) > 1 ? 'cliff-like' : 'long tail'],
    ['Half-life', fmtHours(halfLife(fit.tau_hours, fit.beta)), 'R(lambda)=0.5'],
    ['log-R2', fmtNumber(fit.log_r2, 3), 'fit quality'],
    ['Delta AIC', fmtNumber(fit.delta_aic_vs_exp, 2), 'vs D=1 exp'],
    ['Sleep bias', bias === undefined ? '-' : pct(bias), state.signal === 'corrected' ? 'removed pre-fit' : 'raw mode']
  ].map(([k, v, s]) => `<div class="metric"><span>${k}</span><strong>${v}</strong><small>${s}</small></div>`).join('');

  const suffix = state.signal === 'corrected' ? 'circadian-corrected' : 'raw feed';
  if (state.view === 'residuals') {
    els.chartTitle.textContent = `${topic.label}: residual dust (${suffix})`;
  } else if (state.view === 'comparison') {
    els.chartTitle.textContent = `${topic.label}: S2 vs exponential (${suffix})`;
  } else {
    els.chartTitle.textContent = `${topic.label}: S2 retention (${suffix})`;
  }
  drawInteractiveChart(topic);
}

function initChart() {
  if (!window.echarts) {
    els.chart.innerHTML = '<div class="empty-state">Interactive chart library did not load. Check network access to the ECharts CDN.</div>';
    return null;
  }
  if (!state.chart) {
    state.chart = window.echarts.init(els.chart, null, { renderer: 'canvas' });
  }
  return state.chart;
}

function makeLineSeries(name, data, color, opts = {}) {
  return {
    name,
    type: 'line',
    data,
    showSymbol: opts.showSymbol ?? true,
    symbolSize: opts.symbolSize ?? 6,
    smooth: opts.smooth ?? true,
    connectNulls: false,
    lineStyle: { width: opts.width || 3, type: opts.dash ? 'dashed' : 'solid', color, opacity: opts.opacity ?? 1 },
    itemStyle: { color, opacity: opts.opacity ?? 1 },
    emphasis: { disabled: true },
    blur: { disabled: true },
    z: opts.z || 3
  };
}

function drawInteractiveChart(topic) {
  const chart = initChart();
  if (!chart) return;
  const series = topic.series || [];
  if (!series.length) {
    chart.clear();
    els.chart.innerHTML = '<div class="empty-state">No retention series yet. Run the update workflow to fetch live RSS feeds.</div>';
    els.chartLegend.innerHTML = '';
    els.chartNote.textContent = 'Waiting for live feed history.';
    return;
  }
  const c = chartColors();
  const tau = topic.fit?.tau_hours;
  const beta = topic.fit?.beta;
  const xs = series.map(d => Number(d.x_hours) || 0);
  const maxX = Math.max(1, ...xs);
  const primaryName = state.signal === 'corrected' ? 'observed corrected' : 'observed raw';
  const altName = state.signal === 'corrected' ? 'raw feed' : 'circadian corrected';
  const observed = series.map(d => [Number(d.x_hours) || 0, observedValue(d)]);
  const alternate = series.map(d => [Number(d.x_hours) || 0, observedValue(d, state.signal === 'corrected' ? 'raw' : 'corrected')]);
  const fit = series.map(d => [Number(d.x_hours) || 0, fitValue(d, topic)]);
  const exp = series.map(d => [Number(d.x_hours) || 0, tau ? Math.exp(-Math.max(0, Number(d.x_hours) || 0) / tau) : null]);
  const residuals = series.map(d => [Number(d.x_hours) || 0, observedValue(d) === null ? null : observedValue(d) - fitValue(d, topic)]);
  const factors = series.map(d => [Number(d.x_hours) || 0, d.circadian_factor ?? null]);

  const chartSeries = [];
  let yMin = 0;
  let yMax = 1.1;
  let yName = 'retention';
  if (state.view === 'residuals') {
    yName = 'residual';
    const maxAbs = Math.max(0.1, ...residuals.map(d => d[1]).filter(v => v !== null).map(v => Math.abs(v))) * 1.2;
    yMin = -maxAbs;
    yMax = maxAbs;
    chartSeries.push({
      name: 'residual dust',
      type: 'bar',
      data: residuals,
      barWidth: 12,
      itemStyle: { color: params => (params.value[1] || 0) >= 0 ? c.bad : c.good, borderRadius: [4, 4, 0, 0] },
      emphasis: { disabled: true },
      blur: { disabled: true },
      z: 4
    });
    chartSeries.push(makeLineSeries('zero', [[0,0],[maxX,0]], c.line, { showSymbol: false, smooth: false, width: 1, dash: true, opacity: 0.85, z: 1 }));
  } else {
    const all = [...observed, ...fit].map(d => d[1]).filter(v => v !== null);
    yMax = Math.max(1.05, ...all) * 1.05;
    chartSeries.push(makeLineSeries('S2 fit', fit, c.accent, { showSymbol: false, width: 4, z: 4 }));
    chartSeries.push(makeLineSeries(primaryName, observed, c.accent2, { width: 2.5, dash: true, symbolSize: 7, z: 5 }));
    chartSeries.push(makeLineSeries(altName, alternate, c.muted, { width: 1.5, dash: true, symbolSize: 4, opacity: 0.5, z: 2 }));
    if (state.view === 'comparison') {
      chartSeries.push(makeLineSeries('D=1 exponential', exp, c.warn, { showSymbol: false, width: 2.4, dash: true, z: 3 }));
    }
  }

  if (state.signal === 'corrected' && state.view !== 'residuals') {
    chartSeries.push({
      name: 'activity factor',
      type: 'line',
      yAxisIndex: 1,
      data: factors,
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 1.2, color: c.good, opacity: 0.35, type: 'dotted' },
      itemStyle: { color: c.good, opacity: 0.35 },
      tooltip: { valueFormatter: v => v == null ? '-' : fmtNumber(v, 2) },
      z: 1
    });
  }

  const markLineData = tau ? [{ xAxis: Math.min(maxX, tau), label: { formatter: `lambda_q ${fmtHours(tau)}` } }] : [];
  if (markLineData.length && chartSeries[0]) {
    chartSeries[0].markLine = {
      symbol: ['none', 'none'],
      label: { color: c.warn, fontWeight: 800, formatter: `lambda_q ${fmtHours(tau)}` },
      lineStyle: { color: c.warn, width: 2, type: 'dashed' },
      data: markLineData
    };
  }

  const option = {
    backgroundColor: 'transparent',
    color: [c.accent, c.accent2, c.warn, c.good, c.bad],
    animationDuration: 450,
    grid: {
      left: 48,
      right: state.signal === 'corrected' && state.view !== 'residuals' ? 52 : 24,
      top: 20,
      bottom: 70,
      containLabel: true
    },
    tooltip: {
      trigger: 'axis',
      confine: false,
      appendToBody: true,
      enterable: false,
      transitionDuration: 0.05,
      axisPointer: { type: 'line', label: { color: c.text, backgroundColor: 'rgba(60,70,95,.92)' } },
      extraCssText: 'max-width:270px;white-space:normal;pointer-events:none;box-shadow:0 10px 30px rgba(0,0,0,.18);',
      backgroundColor: state.theme === 'dark' ? 'rgba(9,14,30,.96)' : 'rgba(255,255,255,.98)',
      borderColor: c.line,
      textStyle: { color: c.text },
      formatter(params) {
        const x = Array.isArray(params) ? params[0]?.axisValue : null;
        const rows = [`<b>${topic.label}</b>`, `<span>${fmtNumber(x, 1)}h since peak</span>`];
        (params || []).forEach(p => {
          const val = Array.isArray(p.value) ? p.value[1] : p.value;
          if (val === null || val === undefined || Number.isNaN(Number(val))) return;
          rows.push(`${p.marker}${p.seriesName}: <b>${fmtNumber(val, 3)}</b>`);
        });
        rows.push(`<span>circadian bias: ${topic.circadian_bias == null ? '-' : pct(topic.circadian_bias)}</span>`);
        return rows.join('<br/>');
      }
    },
    legend: { show: false },
    toolbox: {
      show: true,
      right: 6,
      top: 0,
      iconStyle: { borderColor: c.muted },
      emphasis: { iconStyle: { borderColor: c.accent } },
      feature: { dataZoom: { yAxisIndex: 'none' }, restore: {}, saveAsImage: { backgroundColor: state.theme === 'dark' ? '#0a0f1e' : '#eef3fb' } }
    },
    dataZoom: [
      { type: 'inside', throttle: 40, xAxisIndex: 0 },
      {
        type: 'slider',
        xAxisIndex: 0,
        height: 12,
        bottom: 8,
        borderColor: c.line,
        fillerColor: state.theme === 'dark' ? 'rgba(125,211,252,.16)' : 'rgba(3,105,161,.16)',
        handleStyle: { color: c.accent },
        textStyle: { color: c.muted, fontSize: 10 }
      }
      ],
    xAxis: {
      type: 'value',
      min: 0,
      max: maxX,
      axisLabel: {
        color: c.muted,
        margin: 12,
        formatter: v => `${fmtNumber(v, 0)}h`
      },
      axisLine: { lineStyle: { color: c.line } },
      splitLine: { lineStyle: { color: c.line, type: 'dashed', opacity: 0.65 } }
    },
    yAxis: [
      { type: 'value', name: yName, min: yMin, max: yMax, nameTextStyle: { color: c.muted }, axisLabel: { color: c.muted }, axisLine: { lineStyle: { color: c.line } }, splitLine: { lineStyle: { color: c.line, type: 'dashed', opacity: 0.65 } } },
      { type: 'value', name: 'activity', min: 0, max: 1.15, show: state.signal === 'corrected' && state.view !== 'residuals', nameTextStyle: { color: c.muted }, axisLabel: { color: c.muted }, axisLine: { lineStyle: { color: c.line } }, splitLine: { show: false } }
    ],
    series: chartSeries
  };
  chart.setOption(option, true);

  const status = topic.fit?.fit_status === 'provisional' ? 'Provisional publish-time guide: formal S2 metrics unlock after enough post-peak published-article bins accumulate. ' : '';
  const norm = state.signal === 'corrected' ? 'Circadian-corrected signal divides raw publish-time counts by expected publishing/activity availability before S2 fitting.' : 'Raw mode shows the uncorrected RSS publish-time attention stream; overnight gaps may mimic decay.';
  els.chartNote.textContent = `${status}${norm} tau=${fmtHours(tau)}, beta=${fmtNumber(beta, 2)}, half-life=${fmtHours(halfLife(tau, beta))}.`;
  els.chartLegend.innerHTML = chartSeries.filter(s => s.name !== 'zero').map(s => `<span><i style="background:${s.lineStyle?.color || c.bad}"></i>${s.name}</span>`).join('');
}

function tailReadiness(topic) {
  const fit = topic.fit || {};
  const binHours = state.data?.bin_hours || 3;

  // Formal S2 threshold used by the backend:
  // at least 4 nonzero post-peak bins and at least 2 tail articles.
  const minBins = 4;
  const minArticles = 2;
  const minSpanHours = (minBins - 1) * binHours;

  const series = topic.series || [];
  const observedTail = series
    .filter(d => Number(d.x_hours) >= 0)
    .filter(d => observedValue(d) !== null);

  const nonzeroBins = observedTail
    .filter(d => Number(observedValue(d)) > 0.001)
    .length;

  const tailSpanHours = observedTail.length
    ? Math.max(...observedTail.map(d => Number(d.x_hours) || 0))
    : 0;

  const articleCount = topic.article_count || 0;

  const binScore = Math.min(1, nonzeroBins / minBins);
  const articleScore = Math.min(1, articleCount / minArticles);
  const spanScore = Math.min(1, tailSpanHours / minSpanHours);

  // Use the strictest requirement as readiness.
  const score = Math.min(binScore, articleScore, spanScore);
  const percent = Math.max(0, Math.min(100, Math.round(score * 100)));

  const needBins = Math.max(0, minBins - nonzeroBins);
  const needArticles = Math.max(0, minArticles - articleCount);
  const needHours = Math.max(0, Math.ceil(minSpanHours - tailSpanHours));

  const needs = [];
  if (needBins > 0) needs.push(`+${needBins} bins`);
  if (needArticles > 0) needs.push(`+${needArticles} articles`);
  if (needHours > 0) needs.push(`+${needHours}h span`);

  return {
    percent,
    nonzeroBins,
    tailSpanHours,
    needText: needs.length ? `needs ${needs.join(' / ')}` : 'ready for formal fit',
    label: fit.fit_status === 'formal' ? 'formal S2' : `${percent}% tail ready`
  };
}

function renderTopicBoard() {
  const topics = getTopicsSorted();

  if (!topics.length) {
    els.topicBoard.innerHTML = '<div class="empty-state">No topic cycles yet.</div>';
    return;
  }

  els.topicBoard.innerHTML = topics.map(topic => {
    const fit = topic.fit || {};
    const isFormal = fit.fit_status === 'formal';
    const readiness = tailReadiness(topic);

    const realSeries = (topic.series || []).filter(d => observedValue(d) !== null);
    const retained = realSeries.length
      ? observedValue(realSeries[realSeries.length - 1])
      : (topic.series?.length ? fitValue(topic.series[topic.series.length - 1], topic) : 0);

    const newest = topicNewest(topic);

    const badge = isFormal
      ? pct(retained)
      : `${readiness.percent}%`;

    const width = isFormal
      ? Math.min(100, Math.round((retained || 0) * 100))
      : readiness.percent;

    const statusText = isFormal
      ? topic.phase
      : `${readiness.label} · ${readiness.needText}`;

    const barTitle = isFormal
      ? `Retained signal: ${badge}`
      : `Tail readiness: ${readiness.percent}%`;

    return `<article class="topic-card ${topic.key === state.selectedTopic ? 'active' : ''}" data-topic="${topic.key}">
      <div class="topic-top">
        <span class="topic-name">${topic.label}</span>
        <span class="topic-badge">${badge}</span>
      </div>

      <p class="topic-meta">
        <span>${statusText}</span>
        <span>N ${topic.article_count}</span>
        <span>new ${newest ? ageLabel(new Date(newest).toISOString()) : '-'}</span>
        <span>tau ${fmtHours(fit.tau_hours)}</span>
        <span>beta ${fmtNumber(fit.beta, 2)}</span>
        <span>sleep ${topic.circadian_bias == null ? '-' : pct(topic.circadian_bias)}</span>
      </p>

      <div class="bar" title="${barTitle}">
        <i style="width:${width}%"></i>
      </div>
    </article>`;
  }).join('');

  els.topicBoard.querySelectorAll('.topic-card').forEach(card => {
    card.addEventListener('click', () => {
      state.selectedTopic = card.dataset.topic;
      render();
    });
  });
}

function renderTopicTable() {
  els.topicTable.innerHTML = '';
  getTopicsSorted().forEach(topic => {
    const fit = topic.fit || {};
    const newest = topicNewest(topic);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="topic-pill">${topic.label}</span></td>
      <td>${topic.phase}</td>
      <td>${topic.article_count}</td>
      <td>${newest ? ageLabel(new Date(newest).toISOString()) : '-'}</td>
      <td>${fmtHours(fit.tau_hours)}</td>
      <td>${fmtNumber(fit.beta, 2)}</td>
      <td>${fmtHours(halfLife(fit.tau_hours, fit.beta))}</td>
      <td>${fmtNumber(topicDust(topic), 3)}</td>
      <td>${fmtNumber(fit.delta_aic_vs_exp, 2)}</td>`;
    tr.addEventListener('click', () => { state.selectedTopic = topic.key; render(); });
    els.topicTable.appendChild(tr);
  });
}

function renderStories() {
  const topic = selectedTopic();
  const selected = topic?.key;
  const articles = state.data?.articles || [];
  const formal = topic?.fit?.fit_status === 'formal';

  let filtered = selected
    ? articles.filter(article => article.topic === selected)
    : [...articles];

  if (formal) {
    filtered = filtered
      .map(article => ({ article, sticky: storyStickiness(article, topic) }))
      .sort((a, b) => {
        if (b.sticky.score !== a.sticky.score) return b.sticky.score - a.sticky.score;
        return new Date(b.article.published_at).getTime() - new Date(a.article.published_at).getTime();
      })
      .slice(0, 18);
  } else {
    filtered = filtered
      .sort((a, b) => new Date(b.published_at).getTime() - new Date(a.published_at).getTime())
      .slice(0, 18)
      .map(article => ({ article, sticky: storyStickiness(article, topic) }));
  }

  els.storyList.innerHTML = '';
  els.storyCount.textContent = formal
    ? `${filtered.length} sticky-ranked`
    : `${filtered.length} latest`;

  if (!filtered.length) {
    els.storyList.innerHTML = '<div class="empty-state">No live stories yet for this topic. Run the scheduled update workflow or wait for the next RSS batch.</div>';
    return;
  }

  filtered.forEach(({ article, sticky }) => {
    const node = els.storyTemplate.content.cloneNode(true);

    const meta = [
      article.source || 'Unknown',
      fmtDate(article.published_at),
      ageLabel(article.published_at)
    ];

    if (formal) {
      meta.push(`stick ${sticky.score}/100`);
      if (sticky.residual != null) meta.push(`res +${fmtNumber(sticky.residual, 3)}`);
      if (sticky.xHours != null) meta.push(`${fmtHours(sticky.xHours)} after peak`);
      meta.push(sticky.role);
    }

    node.querySelector('.story__meta').textContent = meta.join(' · ');

    const link = node.querySelector('a');
    link.href = article.url || '#';
    link.textContent = article.title || '(untitled)';
    if (!article.url) link.removeAttribute('href');

    node.querySelector('.story__topic').textContent = formal
      ? (sticky.score > 0 ? `stick ${sticky.score}` : sticky.role)
      : (article.topic_label || article.topic || 'General');

    els.storyList.appendChild(node);
  });
}

function renderSources() {
  const sources = state.data?.sources || [];
  const errors = state.data?.summary?.fetch_errors || [];
  const errorMap = new Map(errors.map(e => [e.source, e.error]));
  els.sourceCount.textContent = `${sources.length} configured`;
  if (!sources.length) {
    els.sourceList.innerHTML = '<div class="empty-state">No sources listed. Add RSS feeds in scripts/sources.json.</div>';
    return;
  }
  els.sourceList.innerHTML = sources.map(source => {
    const err = errorMap.get(source.name);
    const label = err ? 'error' : 'ok';
    const home = source.home || source.url || '#';
    return `<article class="source-card ${err ? 'error' : ''}">
      <div><a href="${home}" target="_blank" rel="noopener noreferrer">${source.name}</a><p class="source-meta"><span>${err ? err : source.url}</span></p></div>
      <span class="source-badge">${label}</span>
    </article>`;
  }).join('');
}

els.topicSelect.addEventListener('change', event => { state.selectedTopic = event.target.value; render(); });
els.viewSelect.addEventListener('change', event => { state.view = event.target.value; renderSelected(); });
els.signalSelect.addEventListener('change', event => { state.signal = event.target.value; localStorage.setItem('dream-news-signal', state.signal); render(); });
els.sortSelect.addEventListener('change', event => { state.sort = event.target.value; render(); });
els.themeBtn.addEventListener('click', () => { state.theme = state.theme === 'dark' ? 'light' : 'dark'; applyTheme(); if (state.chart) state.chart.dispose(); state.chart = null; renderSelected(); });
els.refreshBtn.addEventListener('click', loadData);
window.addEventListener('resize', () => { if (state.chart) state.chart.resize(); });

applyTheme();
loadData({ quiet: false });
setInterval(() => loadData({ quiet: true }), REFRESH_MS);
