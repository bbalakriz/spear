import {
  DocumentTitle,
  ListPageHeader,
  consoleFetchJSON,
} from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Content,
  Card,
  CardBody,
  CardTitle,
  Flex,
  FlexItem,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  Label,
  Modal,
  ModalBody,
  ModalHeader,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import {
  Chart,
  ChartAxis,
  ChartLine,
  ChartScatter,
  ChartThemeColor,
  ChartThreshold,
  ChartVoronoiContainer,
} from '@patternfly/react-charts/victory';
import { CheckCircleIcon, ExclamationCircleIcon, MinusCircleIcon } from '@patternfly/react-icons';
import { useEffect, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/spear-console/backend';

// pinned colors so broken line segments do not cycle per victory child
const COLOR_HIGHER_BETTER = '#06c';
const COLOR_LOWER_BETTER = '#ec7a08';
// off white title on card background in light and dark theme
const COLOR_TILE_TITLE = '#f0f0f0';

interface CycleBenchmark {
  run_id: string;
  job_name: string;
  job_id: string;
  provider_id: string;
  benchmark_id: string;
  metrics: Record<string, number>;
  params: Record<string, string>;
  report_url: string | null;
}

// only job this page can act on, not just chart
const CONFIDENTIAL_LEAK_JOB = 'spear-shield-confidential-data-leak-scan';

interface Cycle {
  run_id: string;
  run_name: string;
  start_time: number;
  mlflow_url: string;
  benchmarks: CycleBenchmark[];
}

interface MetricDef {
  key: string;
  label: string;
  lowerIsBetter: boolean;
}

// mlflow metric keys per job; first entry is the default dropdown
const METRICS_BY_JOB: Record<string, MetricDef[]> = {
  'spear-shield-ragas-rag-eval': [
    { key: 'faithfulness', label: 'Faithfulness', lowerIsBetter: false },
    { key: 'answer_relevancy', label: 'Answer relevancy', lowerIsBetter: false },
    { key: 'context_relevance', label: 'Context relevance', lowerIsBetter: false },
    { key: 'response_groundedness', label: 'Response groundedness', lowerIsBetter: false },
  ],
  'spear-shield-ibm-clear-agentic-eval': [
    { key: 'overall_score', label: 'Overall score', lowerIsBetter: false },
    {
      key: 'pct_interactions_with_issues',
      label: 'Interactions with issues (%)',
      lowerIsBetter: true,
    },
    { key: 'issues_per_interaction', label: 'Issues per interaction', lowerIsBetter: true },
    { key: 'total_issues', label: 'Total issues', lowerIsBetter: true },
    { key: 'total_interactions', label: 'Total interactions', lowerIsBetter: false },
    { key: 'agent._handle.avg_score', label: 'Handle agent avg score', lowerIsBetter: false },
    {
      key: 'agent.chat_completion.avg_score',
      label: 'Chat completion agent avg score',
      lowerIsBetter: false,
    },
  ],
  'spear-shield-garak-quick-scan': [
    { key: 'attack_success_rate', label: 'Attack success rate', lowerIsBetter: true },
    { key: 'dan.Dan_11_0_asr', label: 'DAN 11.0 attack success rate', lowerIsBetter: true },
  ],
  'spear-shield-confidential-data-leak-scan': [
    { key: 'confidential_leak_count', label: 'Confidential leak count', lowerIsBetter: true },
  ],
};

const JOB_TITLES: Record<string, string> = {
  'spear-shield-ragas-rag-eval': 'RAG Answer Quality (Ragas)',
  'spear-shield-ibm-clear-agentic-eval': 'Agentic Trace Health (IBM CLEAR)',
  'spear-shield-garak-quick-scan': 'Prompt Attack Resistance (Garak)',
  'spear-shield-confidential-data-leak-scan': 'Confidential Data Leaks',
};

function shortModelName(raw?: string): string {
  if (!raw) return 'unknown model';
  const parts = raw.split('/');
  return parts[parts.length - 1];
}

function formatValue(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(3);
}

function formatCycleLabel(cycle?: Cycle): string {
  if (!cycle) return '';
  return new Date(cycle.start_time).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function benchmarkFor(cycle: Cycle, jobName: string): CycleBenchmark | undefined {
  return cycle.benchmarks.find((b) => b.job_name === jobName);
}

// latest cycle for this job, shared by tiles, details and report link
function latestCycleAndBenchmarkFor(
  cycles: Cycle[],
  jobName: string,
): { cycle: Cycle; benchmark: CycleBenchmark } | undefined {
  for (let i = cycles.length - 1; i >= 0; i -= 1) {
    const bench = benchmarkFor(cycles[i], jobName);
    if (bench) return { cycle: cycles[i], benchmark: bench };
  }
  return undefined;
}

function latestBenchmarkFor(cycles: Cycle[], jobName: string): CycleBenchmark | undefined {
  return latestCycleAndBenchmarkFor(cycles, jobName)?.benchmark;
}

interface SeriesPoint {
  x: number;
  y: number;
  cycle: Cycle;
  benchmark: CycleBenchmark;
}

// keep x across gap cycles; segments avoid misleading slopes across missing data
function buildSegments(cycles: Cycle[], jobName: string, metricKey: string): SeriesPoint[][] {
  const points: (SeriesPoint | null)[] = cycles.map((cycle, i) => {
    const bench = benchmarkFor(cycle, jobName);
    if (bench && typeof bench.metrics[metricKey] === 'number') {
      return { x: i + 1, y: bench.metrics[metricKey], cycle, benchmark: bench };
    }
    return null;
  });

  const segments: SeriesPoint[][] = [];
  let current: SeriesPoint[] = [];
  points.forEach((p) => {
    if (p) {
      current.push(p);
    } else if (current.length) {
      segments.push(current);
      current = [];
    }
  });
  if (current.length) segments.push(current);
  return segments;
}

// compact summary of the same latest benchmark the trend cards use
const JobStatusTile = ({ jobName, cycles }: { jobName: string; cycles: Cycle[] }) => {
  const latest = latestCycleAndBenchmarkFor(cycles, jobName);
  const passValue = latest?.benchmark.metrics.pass;
  const hasPass = typeof passValue === 'number';

  let icon = <MinusCircleIcon color="#8a8d90" />;
  let statusText = 'No runs yet';
  let statusColor = COLOR_TILE_TITLE;
  if (hasPass) {
    const passed = passValue === 1;
    icon = passed ? <CheckCircleIcon color="#3e8635" /> : <ExclamationCircleIcon color="#c9190b" />;
    statusText = passed ? 'Pass' : 'Fail';
    statusColor = passed ? '#3e8635' : '#c9190b';
  } else if (latest) {
    statusText = 'No threshold';
  }

  return (
    <Card isCompact style={{ height: '100%' }}>
      <CardBody>
        <Content component="small" style={{ color: COLOR_TILE_TITLE, display: 'block' }}>
          {JOB_TITLES[jobName] ?? jobName}
        </Content>
        <Flex
          alignItems={{ default: 'alignItemsCenter' }}
          gap={{ default: 'gapSm' }}
          style={{ marginTop: '0.25rem' }}
        >
          <FlexItem>{icon}</FlexItem>
          <FlexItem>
            <strong style={{ color: statusColor }}>{statusText}</strong>
          </FlexItem>
        </Flex>
        {latest && (
          <Content
            component="small"
            style={{ color: COLOR_TILE_TITLE, display: 'block', marginTop: '0.25rem' }}
          >
            last run: {formatCycleLabel(latest.cycle)}
          </Content>
        )}
      </CardBody>
    </Card>
  );
};

const TrendCard = ({ jobName, cycles }: { jobName: string; cycles: Cycle[] }) => {
  const metricDefs = METRICS_BY_JOB[jobName];
  const [metricKey, setMetricKey] = useState(metricDefs[0].key);
  const metric = metricDefs.find((m) => m.key === metricKey) ?? metricDefs[0];

  // proxy csp blocks navigated html; fetch + iframe srcDoc sidesteps that
  const [reportOpen, setReportOpen] = useState(false);
  const [reportHtml, setReportHtml] = useState<string | null>(null);
  const [reportError, setReportError] = useState('');

  // closed loop patches coordinator config from this scan job id only
  const [closedLoopBusy, setClosedLoopBusy] = useState(false);
  const [closedLoopResult, setClosedLoopResult] = useState<{
    added: string[];
    message: string;
  } | null>(null);
  const [closedLoopError, setClosedLoopError] = useState('');

  const triggerClosedLoop = (jobId: string) => {
    setClosedLoopBusy(true);
    setClosedLoopResult(null);
    setClosedLoopError('');
    consoleFetchJSON(`${PROXY_PATH}/trigger/closed-loop`, 'POST', {
      body: JSON.stringify({ job_id: jobId }),
    })
      .then((result: { added: string[]; message: string }) => setClosedLoopResult(result))
      .catch((err: Error) => setClosedLoopError(err.message))
      .finally(() => setClosedLoopBusy(false));
  };

  const openReport = (reportUrl: string) => {
    setReportOpen(true);
    setReportHtml(null);
    setReportError('');
    // consoleFetchJSON cannot load html; same origin fetch + cookie is enough
    fetch(`${PROXY_PATH}${reportUrl}`, { credentials: 'include' })
      .then((res) => {
        if (!res.ok) throw new Error(`status ${res.status}`);
        return res.text();
      })
      .then((html) => setReportHtml(html))
      .catch((err: Error) => setReportError(err.message));
  };

  const segments = buildSegments(cycles, jobName, metric.key);
  const allPoints = segments.flat();
  const latest = allPoints[allPoints.length - 1];
  const latestBenchmark = latestBenchmarkFor(cycles, jobName);
  const color = metric.lowerIsBetter ? COLOR_LOWER_BETTER : COLOR_HIGHER_BETTER;

  // threshold line only when selected metric matches primary_score_metric
  const thresholdValue =
    latestBenchmark?.params.primary_score_metric === metric.key
      ? Number(latestBenchmark.params.threshold)
      : NaN;
  const hasThreshold = !Number.isNaN(thresholdValue);

  return (
    // height 100% fills the grid row
    <Card style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <CardTitle>
        <Flex
          justifyContent={{ default: 'justifyContentSpaceBetween' }}
          alignItems={{ default: 'alignItemsCenter' }}
        >
          <FlexItem>{JOB_TITLES[jobName] ?? jobName}</FlexItem>
          {metricDefs.length > 1 && (
            <FlexItem>
              <FormSelect
                value={metricKey}
                onChange={(_e, value) => setMetricKey(value)}
                aria-label={`Metric to chart for ${JOB_TITLES[jobName] ?? jobName}`}
              >
                {metricDefs.map((m) => (
                  <FormSelectOption key={m.key} value={m.key} label={m.label} />
                ))}
              </FormSelect>
            </FlexItem>
          )}
        </Flex>
      </CardTitle>
      {/* flex column; marginTop auto docks the action row to the card bottom */}
      <CardBody style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        {jobName === CONFIDENTIAL_LEAK_JOB &&
          latestBenchmark &&
          (latestBenchmark.metrics.confidential_leak_count ?? 0) > 0 && (
            <div style={{ marginBottom: '0.75rem' }}>
              <Button
                variant="danger"
                isLoading={closedLoopBusy}
                isDisabled={closedLoopBusy || !latestBenchmark.job_id}
                onClick={() => triggerClosedLoop(latestBenchmark.job_id)}
              >
                Tighten guardrails from this leak
              </Button>
              {!latestBenchmark.job_id && (
                <Content component="small" style={{ color: '#6b6f76', marginLeft: '0.5rem' }}>
                  this cycle predates the closed loop, no leak detail was recorded for it
                </Content>
              )}
              {closedLoopResult && (
                <Alert
                  variant={closedLoopResult.added.length > 0 ? 'success' : 'info'}
                  title={closedLoopResult.message}
                  isInline
                  style={{ marginTop: '0.5rem' }}
                />
              )}
              {closedLoopError && (
                <Alert
                  variant="danger"
                  title={`Could not tighten guardrails: ${closedLoopError}`}
                  isInline
                  style={{ marginTop: '0.5rem' }}
                />
              )}
            </div>
          )}

        {allPoints.length === 0 ? (
          <Content component="small" style={{ color: '#6b6f76' }}>
            No cycles have logged this metric yet.
          </Content>
        ) : (
          <>
            <div style={{ height: '170px' }}>
              <Chart
                ariaDesc={metric.label}
                containerComponent={
                  <ChartVoronoiContainer
                    labels={({ datum }: { datum: Partial<SeriesPoint> }) => {
                      // threshold points have no benchmark on the datum
                      if (!datum.benchmark) return `threshold: ${formatValue(datum.y as number)}`;
                      return `${formatCycleLabel(datum.cycle)}\n${metric.label}: ${formatValue(datum.y as number)}\nmodel: ${shortModelName(
                        datum.benchmark.params.model ?? datum.benchmark.params.model_name,
                      )}`;
                    }}
                  />
                }
                height={170}
                padding={{ top: 16, bottom: 50, left: 55, right: 16 }}
                themeColor={metric.lowerIsBetter ? ChartThemeColor.orange : ChartThemeColor.blue}
              >
                <ChartAxis
                  tickValues={cycles.map((_c, i) => i + 1)}
                  tickFormat={(x: number) => formatCycleLabel(cycles[x - 1])}
                  style={{
                    tickLabels: { angle: -30, textAnchor: 'end', fontSize: 8 },
                  }}
                  fixLabelOverlap
                />
                <ChartAxis dependentAxis style={{ tickLabels: { fontSize: 9 } }} />
                {hasThreshold && (
                  <ChartThreshold
                    data={[
                      { x: 1, y: thresholdValue },
                      { x: cycles.length, y: thresholdValue },
                    ]}
                    style={{ data: { stroke: '#8a8d90', strokeDasharray: '4,3' } }}
                  />
                )}
                {segments.map((segment, i) => (
                  <ChartLine key={i} data={segment} style={{ data: { stroke: color } }} />
                ))}
                {/* dots so a lone point after a gap stays visible */}
                <ChartScatter data={allPoints} style={{ data: { fill: color } }} size={3} />
              </Chart>
            </div>

            {/* compact metadata line for narrow cards */}
            {latestBenchmark && (
              <Content
                component="small"
                style={{ color: '#6b6f76', display: 'block', marginTop: '0.5rem' }}
              >
                {shortModelName(latestBenchmark.params.model ?? latestBenchmark.params.model_name)}
                {' · '}
                {latestBenchmark.provider_id || 'n/a'}
                {latestBenchmark.params.num_examples_evaluated &&
                  ` · ${latestBenchmark.params.num_examples_evaluated} examples`}
                {latestBenchmark.params.duration_seconds &&
                  ` · ${Number(latestBenchmark.params.duration_seconds).toFixed(1)}s`}
              </Content>
            )}

            <Flex
              justifyContent={{ default: 'justifyContentSpaceBetween' }}
              alignItems={{ default: 'alignItemsCenter' }}
              style={{ marginTop: 'auto', paddingTop: '0.5rem' }}
            >
              <FlexItem>
                <Content component="small">
                  Latest: <strong>{formatValue(latest.y)}</strong>{' '}
                  {metric.lowerIsBetter ? '(lower is better)' : '(higher is better)'}
                  {typeof latestBenchmark?.metrics.pass === 'number' && (
                    <Label
                      isCompact
                      color={latestBenchmark.metrics.pass === 1 ? 'green' : 'red'}
                      style={{ marginLeft: '0.5rem' }}
                    >
                      {latestBenchmark.metrics.pass === 1 ? 'Pass' : 'Fail'}
                    </Label>
                  )}
                </Content>
              </FlexItem>
              <FlexItem>
                <Flex gap={{ default: 'gapMd' }}>
                  {latestBenchmark?.report_url && (
                    <FlexItem>
                      <Button
                        variant="link"
                        isInline
                        onClick={() => openReport(latestBenchmark.report_url as string)}
                      >
                        View full report
                      </Button>
                    </FlexItem>
                  )}
                  <FlexItem>
                    <Button
                      component="a"
                      href={latest.cycle.mlflow_url}
                      target="_blank"
                      rel="noreferrer"
                      variant="link"
                      isInline
                    >
                      View in MLflow
                    </Button>
                  </FlexItem>
                </Flex>
              </FlexItem>
            </Flex>
          </>
        )}
      </CardBody>
      <Modal isOpen={reportOpen} onClose={() => setReportOpen(false)} variant="large" width="90vw">
        <ModalHeader title={`${JOB_TITLES[jobName] ?? jobName} full report`} />
        <ModalBody>
          {reportError && (
            <Content
              component="small"
              style={{ color: '#ff8787' }}
            >{`Error: ${reportError}`}</Content>
          )}
          {!reportError && reportHtml === null && <Spinner size="lg" />}
          {!reportError && reportHtml !== null && (
            <iframe
              title={`${JOB_TITLES[jobName] ?? jobName} full report`}
              srcDoc={reportHtml}
              sandbox="allow-scripts"
              style={{ width: '100%', height: '78vh', border: 'none', background: '#fff' }}
            />
          )}
        </ModalBody>
      </Modal>
    </Card>
  );
};

export default function TrendPage() {
  const { t } = useTranslation('plugin__spear-console');
  const [limit, setLimit] = useState('10');
  const [cycles, setCycles] = useState<Cycle[] | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    // ignore stale responses after limit changes
    let cancelled = false;
    consoleFetchJSON(`${PROXY_PATH}/trend/eval-cycles?limit=${limit}`)
      .then((data: { cycles: Cycle[] }) => {
        if (!cancelled) setCycles(data.cycles);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [limit]);

  return (
    <>
      <DocumentTitle>{t('Evaluation Trends')}</DocumentTitle>
      <ListPageHeader title={t('Evaluation Trends')} />
      <PageSection>
        <Content component="p" >
          Agent safety and quality evaluation history. Each data point represents a completed evaluation cycle, covering response quality, workflow integrity, prompt injection resilience and confidential data protection, based on recorded evaluation results.
        </Content>

        <Flex
          alignItems={{ default: 'alignItemsFlexEnd' }}
          gap={{ default: 'gapMd' }}
          style={{ marginBottom: '1rem' }}
        >
          <FlexItem>
            <FormGroup label="Cycles to show" fieldId="cycle-limit-select">
              <FormSelect
                id="cycle-limit-select"
                value={limit}
                onChange={(_e, value) => setLimit(value)}
              >
                <FormSelectOption value="5" label="Last 5" />
                <FormSelectOption value="10" label="Last 10" />
                <FormSelectOption value="25" label="Last 25" />
                <FormSelectOption value="100" label="All" />
              </FormSelect>
            </FormGroup>
          </FlexItem>
        </Flex>

        {error && (
          <Content component="small" style={{ color: '#ff8787' }}>{`Error: ${error}`}</Content>
        )}
        {!error && cycles === null && <Spinner size="lg" />}
        {!error && cycles !== null && cycles.length === 0 && (
          <Content component="small">No eval cycles have run yet.</Content>
        )}
        {!error && cycles !== null && cycles.length > 0 && (
          <>
            <Flex
              alignItems={{ default: 'alignItemsStretch' }}
              gap={{ default: 'gapMd' }}
              style={{ marginBottom: '1rem' }}
            >
              {Object.keys(METRICS_BY_JOB).map((jobName) => (
                <FlexItem key={jobName} flex={{ default: 'flex_1' }}>
                  <JobStatusTile jobName={jobName} cycles={cycles} />
                </FlexItem>
              ))}
            </Flex>
            {/* 2x2 grid avoids a long single column scroll */}
            <Grid hasGutter>
              {Object.keys(METRICS_BY_JOB).map((jobName) => (
                <GridItem key={jobName} span={6}>
                  <TrendCard jobName={jobName} cycles={cycles} />
                </GridItem>
              ))}
            </Grid>
          </>
        )}
      </PageSection>
    </>
  );
}
