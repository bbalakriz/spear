import {
  DocumentTitle,
  ListPageHeader,
  consoleFetchJSON,
} from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Card,
  CardBody,
  CardTitle,
  Content,
  Flex,
  FlexItem,
  Label,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import {
  Chart,
  ChartAxis,
  ChartLine,
  ChartThemeColor,
  ChartVoronoiContainer,
} from '@patternfly/react-charts/victory';
import { useEffect, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

interface CycleBenchmark {
  run_id: string;
  job_name: string;
  provider_id: string;
  benchmark_id: string;
  metrics: Record<string, number>;
}

interface Cycle {
  run_id: string;
  run_name: string;
  start_time: number;
  mlflow_url: string;
  benchmarks: CycleBenchmark[];
}

// one real job type scripts/11-submit-evalhub-jobs.sh submits every real
// cycle, mapped to the one metric worth tracking over time for each.
// ibm-clear's own native run (the provider's own save path, the script
// only tags it, never logs a metric onto it) never gets the plain
// primary_score/pass pair the other three get from log_eval_to_mlflow.py,
// confirmed live, overall_score is its own real equivalent instead
const METRIC_BY_JOB: Record<string, { key: string; label: string; lowerIsBetter: boolean }> = {
  'spear-shield-ragas-rag-eval': {
    key: 'primary_score',
    label: 'Ragas Faithfulness',
    lowerIsBetter: false,
  },
  'spear-shield-ibm-clear-agentic-eval': {
    key: 'overall_score',
    label: 'IBM CLEAR Overall Score',
    lowerIsBetter: false,
  },
  'spear-shield-garak-quick-scan': {
    key: 'attack_success_rate',
    label: 'Garak Attack Success Rate',
    lowerIsBetter: true,
  },
  'spear-shield-confidential-data-leak-scan': {
    key: 'confidential_leak_count',
    label: 'Confidential Data Leak Count',
    lowerIsBetter: true,
  },
};

// proper case titles for each small card, same reason every other page
// here keeps its own title map, a heading should read as a heading
const JOB_TITLES: Record<string, string> = {
  'spear-shield-ragas-rag-eval': 'RAG Answer Quality (Ragas)',
  'spear-shield-ibm-clear-agentic-eval': 'Agentic Trace Health (IBM CLEAR)',
  'spear-shield-garak-quick-scan': 'Prompt Attack Resistance (Garak)',
  'spear-shield-confidential-data-leak-scan': 'Confidential Data Leaks',
};

interface TrendPoint {
  x: number;
  y: number;
  cycleLabel: string;
  mlflowUrl: string;
}

function buildTrend(cycles: Cycle[], jobName: string, metricKey: string): TrendPoint[] {
  const points: TrendPoint[] = [];
  cycles.forEach((cycle, i) => {
    const bench = cycle.benchmarks.find((b) => b.job_name === jobName);
    if (bench && typeof bench.metrics[metricKey] === 'number') {
      points.push({
        x: i + 1,
        y: bench.metrics[metricKey],
        cycleLabel: cycle.run_name,
        mlflowUrl: cycle.mlflow_url,
      });
    }
  });
  return points;
}

const TrendCard = ({ jobName, cycles }: { jobName: string; cycles: Cycle[] }) => {
  const config = METRIC_BY_JOB[jobName];
  const points = buildTrend(cycles, jobName, config.key);
  const latest = points[points.length - 1];

  return (
    <Card>
      <CardTitle>{JOB_TITLES[jobName] ?? jobName}</CardTitle>
      <CardBody>
        {points.length === 0 ? (
          <Content component="small" style={{ color: '#6b6f76' }}>
            no real cycles have logged this metric yet
          </Content>
        ) : (
          <>
            <div style={{ height: '180px' }}>
              <Chart
                ariaDesc={config.label}
                containerComponent={
                  <ChartVoronoiContainer
                    labels={({ datum }: { datum: TrendPoint }) => `${datum.cycleLabel}: ${datum.y}`}
                  />
                }
                height={180}
                padding={{ top: 20, bottom: 30, left: 50, right: 20 }}
                themeColor={config.lowerIsBetter ? ChartThemeColor.orange : ChartThemeColor.blue}
              >
                <ChartAxis tickFormat={() => ''} />
                <ChartAxis dependentAxis />
                <ChartLine data={points} />
              </Chart>
            </div>
            <Flex
              justifyContent={{ default: 'justifyContentSpaceBetween' }}
              alignItems={{ default: 'alignItemsCenter' }}
            >
              <FlexItem>
                <Content component="small">
                  {config.label}, latest: <strong>{latest.y}</strong>
                  {config.lowerIsBetter ? ' (lower is better)' : ' (higher is better)'}
                </Content>
              </FlexItem>
              <FlexItem>
                <Label
                  isCompact
                  render={() => (
                    <a href={latest.mlflowUrl} target="_blank" rel="noreferrer">
                      view in MLflow
                    </a>
                  )}
                />
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
  const [cycles, setCycles] = useState<Cycle[] | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    consoleFetchJSON(`${PROXY_PATH}/trend/eval-cycles`)
      .then((data: { cycles: Cycle[] }) => setCycles(data.cycles))
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <>
      <DocumentTitle>{t('Eval Trend')}</DocumentTitle>
      <ListPageHeader title={t('Eval Trend')} />
      <PageSection>
        <Content component="small" style={{ display: 'block', marginBottom: '1rem' }}>
          one real evaluation cycle per point, every ragas, ibm-clear, garak and confidential data
          leak scan job scripts/11-submit-evalhub-jobs.sh has actually run against the real
          coordinator agent, read straight back from the same mlflow runs that cycle logged, nothing
          recomputed here.
        </Content>
        {error && (
          <Content component="small" style={{ color: '#ff8787' }}>{`error: ${error}`}</Content>
        )}
        {!error && cycles === null && <Spinner size="lg" />}
        {!error && cycles !== null && cycles.length === 0 && (
          <Content component="small">no real eval cycles have run yet</Content>
        )}
        {!error && cycles !== null && cycles.length > 0 && (
          <Flex direction={{ default: 'column' }} gap={{ default: 'gapLg' }}>
            {Object.keys(METRIC_BY_JOB).map((jobName) => (
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
