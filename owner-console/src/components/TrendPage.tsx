import {
  DocumentTitle,
  ListPageHeader,
  consoleFetchJSON,
} from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Button,
  Content,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Label,
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
import { useEffect, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

// blue for "higher is better" lines, orange for "lower is better" ones,
// the same two colors every card already used, just pinned explicitly now
// so every segment of a broken line (see buildSegments below) stays the
// same color instead of victory's own per child color cycling
const COLOR_HIGHER_BETTER = '#06c';
const COLOR_LOWER_BETTER = '#ec7a08';

interface CycleBenchmark {
  run_id: string;
  job_name: string;
  provider_id: string;
  benchmark_id: string;
  metrics: Record<string, number>;
  params: Record<string, string>;
  report_url: string | null;
}

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

// every real metric each job type actually logs, confirmed live against
// this project's own mlflow runs, not guessed. primary first in each
// list, that is what a card opens on. ibm-clear's two agent._handle /
// agent.chat_completion entries are this cluster's real current agent
// names, see log_eval_to_mlflow.py and ibm-clear's own native save path,
// a third agent showing up later would need a third entry added here
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

// the most recent cycle that actually ran this job, independent of which
// metric is selected in the dropdown, used for the details panel and the
// report link below the chart
function latestBenchmarkFor(cycles: Cycle[], jobName: string): CycleBenchmark | undefined {
  for (let i = cycles.length - 1; i >= 0; i -= 1) {
    const bench = benchmarkFor(cycles[i], jobName);
    if (bench) return bench;
  }
  return undefined;
}

interface SeriesPoint {
  x: number;
  y: number;
  cycle: Cycle;
  benchmark: CycleBenchmark;
}

// one point per cycle that actually logged this metric, x kept as the
// real position across every fetched cycle (not just the ones with data)
// so a gap in the real history shows up as a real gap, split into
// contiguous segments rather than one array: a plain victory line drawn
// straight across a skipped cycle would draw a long, misleading slope
// between two real but non adjacent points, exactly what made ragas look
// like it was sliding toward zero when the real story was two cycles
// with no ragas data at all (the wait_for_job timeout bug, since fixed)
// followed by one real new data point
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

const TrendCard = ({ jobName, cycles }: { jobName: string; cycles: Cycle[] }) => {
  const metricDefs = METRICS_BY_JOB[jobName];
  const [metricKey, setMetricKey] = useState(metricDefs[0].key);
  const metric = metricDefs.find((m) => m.key === metricKey) ?? metricDefs[0];

  const segments = buildSegments(cycles, jobName, metric.key);
  const allPoints = segments.flat();
  const latest = allPoints[allPoints.length - 1];
  const latestBenchmark = latestBenchmarkFor(cycles, jobName);
  const color = metric.lowerIsBetter ? COLOR_LOWER_BETTER : COLOR_HIGHER_BETTER;

  // a dashed reference line at the pass/fail threshold, only drawn when
  // the selected metric is the one the job's own threshold actually
  // applies to (log_eval_to_mlflow.py only ever logs one threshold, for
  // whichever metric its own primary_score_metric param names)
  const thresholdValue =
    latestBenchmark?.params.primary_score_metric === metric.key
      ? Number(latestBenchmark.params.threshold)
      : NaN;
  const hasThreshold = !Number.isNaN(thresholdValue);

  return (
    <Card>
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
      <CardBody>
        {allPoints.length === 0 ? (
          <Content component="small" style={{ color: '#6b6f76' }}>
            No cycles have logged this metric yet.
          </Content>
        ) : (
          <>
            <div style={{ height: '220px' }}>
              <Chart
                ariaDesc={metric.label}
                containerComponent={
                  <ChartVoronoiContainer
                    labels={({ datum }: { datum: SeriesPoint }) =>
                      `${formatCycleLabel(datum.cycle)}\n${metric.label}: ${formatValue(datum.y)}\nmodel: ${shortModelName(
                        datum.benchmark.params.model ?? datum.benchmark.params.model_name,
                      )}`
                    }
                  />
                }
                height={220}
                padding={{ top: 20, bottom: 60, left: 60, right: 20 }}
                themeColor={metric.lowerIsBetter ? ChartThemeColor.orange : ChartThemeColor.blue}
              >
                <ChartAxis
                  label="Eval cycle"
                  tickValues={cycles.map((_c, i) => i + 1)}
                  tickFormat={(x: number) => formatCycleLabel(cycles[x - 1])}
                  style={{
                    tickLabels: { angle: -30, textAnchor: 'end', fontSize: 9 },
                    axisLabel: { padding: 48 },
                  }}
                  fixLabelOverlap
                />
                <ChartAxis
                  dependentAxis
                  label={metric.label}
                  style={{ axisLabel: { padding: 45 } }}
                />
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
                {/* a single new cycle after a gap is otherwise invisible,
                    a line needs two points to draw at all, these dots
                    make every real data point visible on its own */}
                <ChartScatter data={allPoints} style={{ data: { fill: color } }} size={3} />
              </Chart>
            </div>

            {latestBenchmark && (
              <DescriptionList isCompact isHorizontal style={{ marginTop: '0.5rem' }}>
                <DescriptionListGroup>
                  <DescriptionListTerm>Model</DescriptionListTerm>
                  <DescriptionListDescription>
                    {shortModelName(
                      latestBenchmark.params.model ?? latestBenchmark.params.model_name,
                    )}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Provider</DescriptionListTerm>
                  <DescriptionListDescription>
                    {latestBenchmark.provider_id || 'n/a'}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                {latestBenchmark.params.num_examples_evaluated && (
                  <DescriptionListGroup>
                    <DescriptionListTerm>Examples evaluated</DescriptionListTerm>
                    <DescriptionListDescription>
                      {latestBenchmark.params.num_examples_evaluated}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                )}
                {latestBenchmark.params.duration_seconds && (
                  <DescriptionListGroup>
                    <DescriptionListTerm>Duration</DescriptionListTerm>
                    <DescriptionListDescription>
                      {Number(latestBenchmark.params.duration_seconds).toFixed(1)}s
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                )}
              </DescriptionList>
            )}

            <Flex
              justifyContent={{ default: 'justifyContentSpaceBetween' }}
              alignItems={{ default: 'alignItemsCenter' }}
              style={{ marginTop: '0.5rem' }}
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
                        component="a"
                        href={`${PROXY_PATH}${latestBenchmark.report_url}`}
                        target="_blank"
                        rel="noreferrer"
                        variant="link"
                        isInline
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
    </Card>
  );
};

export default function TrendPage() {
  const { t } = useTranslation('plugin__phase1-ingestion-console');
  const [limit, setLimit] = useState('10');
  const [cycles, setCycles] = useState<Cycle[] | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    // a stale response from a superseded limit change should never
    // overwrite a newer one, the cancelled flag below guards that rather
    // than resetting cycles to null synchronously up front, which would
    // trigger an extra cascading render for no real benefit here
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
      <DocumentTitle>{t('Eval Trend')}</DocumentTitle>
      <ListPageHeader title={t('Eval Trend')} />
      <PageSection>
        <Content component="small" style={{ display: 'block', marginBottom: '1rem' }}>
          One real evaluation cycle per point. Every Ragas, IBM CLEAR, Garak and confidential data
          leak scan job that scripts/11-submit-evalhub-jobs.sh has actually run against the real
          coordinator agent, read straight back from the same MLflow runs that cycle logged, nothing
          recomputed here.
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
          <Flex direction={{ default: 'column' }} gap={{ default: 'gapLg' }}>
            {Object.keys(METRICS_BY_JOB).map((jobName) => (
              <FlexItem key={jobName}>
                <TrendCard jobName={jobName} cycles={cycles} />
              </FlexItem>
            ))}
          </Flex>
        )}
      </PageSection>
    </>
  );
}
