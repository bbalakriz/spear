import {
  DocumentTitle,
  ListPageHeader,
  consoleFetchJSON,
} from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Button,
  Content,
  Flex,
  FlexItem,
  Grid,
  GridItem,
  Icon,
  Label,
  PageSection,
  TextInput,
} from '@patternfly/react-core';
import { PaperPlaneIcon, PlayIcon, TerminalIcon } from '@patternfly/react-icons';
import { Dispatch, ReactNode, SetStateAction, useEffect, useRef, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

// how long each revealed probe stays on screen before the next one
// appears, both sandbox columns reveal the same probe at the same time,
// that lockstep is the whole point of a side by side compare
const REVEAL_STEP_MS = 700;

interface SandboxInfo {
  pod: string;
  phase: string;
  ready: boolean;
  runtime_class: string;
  node: string;
}

interface Probe {
  id: string;
  label: string;
  command: string;
  coordinator_output: string;
  retrieval_output: string;
  verdict: 'same' | 'differs';
}

interface ProbeGroup {
  id: string;
  label: string;
  probes: Probe[];
}

interface CompareResult {
  namespace: string;
  coordinator: SandboxInfo;
  retrieval: SandboxInfo;
  groups: ProbeGroup[];
}

// one command a visitor typed themselves, run against both real sandboxes
// at once, same pod pair the scripted probes above already use
interface TypedBoth {
  command: string;
  coordinator_output: string;
  retrieval_output: string;
}

interface DemoStep {
  label: string;
  command: string;
  output: string;
  ok: boolean;
}

interface DemoResult {
  title: string;
  steps: DemoStep[];
}

interface Scenario {
  id: string;
  title: string;
  intro: string;
  enforcedBy: string;
}

// display order for the compare groups, mirrors COMPARE_GROUP_IDS on the
// backend, a run all walks this same order so it lands identically to
// pressing every one of these buttons in order by hand
const COMPARE_GROUPS: { id: string; label: string }[] = [
  { id: 'runtime-isolation', label: 'Runtime Isolation' },
  { id: 'resource-visibility', label: 'Resource Visibility' },
  { id: 'common-containment', label: 'Common Containment' },
  { id: 'least-privilege-egress', label: 'Least Privilege Egress' },
];

// the two scenarios below the compare grid test caller identity and tool
// rbac across real human personas, not sandbox runtime isolation, folding
// them into the two runtime diff above would misrepresent what they
// actually check, so they keep their own single terminal card format
const ACCESS_CONTROL_SCENARIOS: Scenario[] = [
  {
    id: 'identity-rejected',
    title: 'SPEAR Shield Identity Rejected at the Gateway',
    intro: 'A caller with no real identity is rejected before any agent logic ever runs.',
    enforcedBy: 'Authorino AuthPolicy spear-retrieval-a2a-auth -> a2a gateway',
  },
  {
    id: 'tool-scope-and-assignee',
    title: 'SPEAR Shield Tool Scope and Assignee Enforcement',
    intro:
      'Three real callers against the real work tracker: a caller lacking the role is blocked at the gateway, a caller holding the role is blocked by this project\u2019s own assignee check on someone else\u2019s item, and a caller acting on their own item succeeds.',
    enforcedBy: 'Authorino AuthPolicy spear-shield-gateway-tools-auth -> mcp gateway',
  },
];

// terminal chrome, three dots in the usual red/yellow/green, a fixed
// monospace body with its own dark background regardless of the console's
// own light/dark theme, a terminal should always look like a terminal
const TerminalWindow = ({ title, children }: { title: string; children: ReactNode }) => (
  <div
    style={{ borderRadius: '8px', overflow: 'hidden', border: '1px solid #000', height: '100%' }}
  >
    <Flex
      alignItems={{ default: 'alignItemsCenter' }}
      gap={{ default: 'gapSm' }}
      style={{ background: '#2a2d31', padding: '0.4rem 0.75rem' }}
    >
      <FlexItem>
        <span style={{ display: 'inline-flex', gap: '0.3rem' }}>
          <span style={dotStyle('#ff5f56')} />
          <span style={dotStyle('#ffbd2e')} />
          <span style={dotStyle('#27c93f')} />
        </span>
      </FlexItem>
      <FlexItem grow={{ default: 'grow' }}>
        <Content
          component="small"
          style={{ color: '#9a9ea3', textAlign: 'center', display: 'block' }}
        >
          {title}
        </Content>
      </FlexItem>
    </Flex>
    <div
      style={{
        background: '#1b1d21',
        color: '#e0e0e0',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: '0.8rem',
        padding: '0.85rem 1rem',
        // tall enough up front to hold all four groups side by side without
        // its own scrollbar, the page itself scrolls instead, so a visitor
        // comparing two columns never has to scroll one column out of sync
        // with the other
        minHeight: '56rem',
      }}
    >
      {children}
    </div>
  </div>
);

const dotStyle = (color: string) => ({
  width: '10px',
  height: '10px',
  borderRadius: '50%',
  background: color,
  display: 'inline-block',
});

const PromptLine = ({ command }: { command: string }) => (
  <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', marginBottom: '0.2rem' }}>
    <span style={{ color: '#6ec1ff' }}>$ </span>
    {command}
  </div>
);

const OutputLine = ({ output, ok = true }: { output: string; ok?: boolean }) => (
  <div
    style={{
      whiteSpace: 'pre-wrap',
      wordBreak: 'break-word',
      color: ok ? '#8be28b' : '#ff8787',
      paddingLeft: '1rem',
      marginBottom: '0.85rem',
    }}
  >
    {output}
  </div>
);

// the group divider line that appears once per group inside both
// terminals, reads as a section break in an otherwise continuous session
const GroupDivider = ({ label }: { label: string }) => (
  <Content
    component="small"
    style={{
      display: 'block',
      color: '#6b6f76',
      textTransform: 'uppercase',
      letterSpacing: '0.04em',
      borderTop: '1px solid #33363b',
      marginTop: '0.75rem',
      paddingTop: '0.6rem',
      marginBottom: '0.4rem',
    }}
  >
    {label}
  </Content>
);

// one probe block inside a terminal: the comment line carries the
// probe's own verdict so each side stays self contained, same prompt and
// output styling the rest of the page already uses
const ProbeBlock = ({ probe, output }: { probe: Probe; output: string }) => (
  <div>
    <Flex
      justifyContent={{ default: 'justifyContentSpaceBetween' }}
      alignItems={{ default: 'alignItemsCenter' }}
    >
      <FlexItem>
        <Content component="small" style={{ color: '#9a9ea3' }}>{`# ${probe.label}`}</Content>
      </FlexItem>
      <FlexItem>
        <Label isCompact color={probe.verdict === 'differs' ? 'orange' : 'green'}>
          {probe.verdict === 'differs' ? 'differs' : 'same'}
        </Label>
      </FlexItem>
    </Flex>
    <PromptLine command={probe.command} />
    <OutputLine output={output} />
  </div>
);

const RuntimeBadge = ({ runtimeClass }: { runtimeClass: string }) => {
  const isKata = runtimeClass.toLowerCase().includes('kata');
  return (
    <Label isCompact color={isKata ? 'orange' : 'blue'}>
      {isKata ? 'kata micro vm' : 'runc'}
    </Label>
  );
};

// a free, editable prompt shared by both sandbox columns, enter runs the
// typed command against both real pods at once, nothing canned about it
const DualPromptInput = ({
  value,
  busy,
  onChange,
  onSubmit,
}: {
  value: string;
  busy: boolean;
  onChange: Dispatch<SetStateAction<string>>;
  onSubmit: () => void;
}) => (
  <Flex
    alignItems={{ default: 'alignItemsCenter' }}
    gap={{ default: 'gapSm' }}
    style={{ marginTop: '0.75rem' }}
  >
    <FlexItem>
      <Content component="small" style={{ color: '#9a9ea3' }}>
        Both $
      </Content>
    </FlexItem>
    <FlexItem grow={{ default: 'grow' }}>
      <TextInput
        value={value}
        isDisabled={busy}
        placeholder="Type any command to be run on both sandboxes at once"
        onChange={(_e, next) => onChange(next)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            onSubmit();
          }
        }}
      />
    </FlexItem>
    <FlexItem>
      <Button
        variant="primary"
        isDisabled={busy || !value.trim()}
        isLoading={busy}
        onClick={onSubmit}
        icon={
          <Icon>
            <PaperPlaneIcon />
          </Icon>
        }
      >
        Run on both
      </Button>
    </FlexItem>
  </Flex>
);

export default function UnderTheHoodPage() {
  const { t } = useTranslation('plugin__phase1-ingestion-console');

  // pod identity for both terminals, fetched once on mount, no exec
  // involved, so the two panels already show who they are before anyone
  // presses a single run button
  const [identity, setIdentity] = useState<Pick<
    CompareResult,
    'namespace' | 'coordinator' | 'retrieval'
  > | null>(null);
  const [identityError, setIdentityError] = useState('');

  // one group's own result, keyed by group id, present only once that
  // group's own button (or run all) has actually run it
  const [groupResults, setGroupResults] = useState<Record<string, ProbeGroup>>({});
  const [groupRevealed, setGroupRevealed] = useState<Record<string, number>>({});
  const [compareError, setCompareError] = useState('');
  const [runningGroup, setRunningGroup] = useState<string | null>(null);
  const [runningAll, setRunningAll] = useState(false);

  const [typedBoth, setTypedBoth] = useState<TypedBoth[]>([]);
  const [typedBothInput, setTypedBothInput] = useState('');
  const [typedBothBusy, setTypedBothBusy] = useState(false);
  const timers = useRef<number[]>([]);

  // the two access control scenarios below, same per scenario state shape
  // this page already used before this redesign
  const [results, setResults] = useState<Record<string, DemoResult>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [scenarioRevealed, setScenarioRevealed] = useState<Record<string, number>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => () => timers.current.forEach((id) => window.clearTimeout(id)), []);

  useEffect(() => {
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/sandbox-identity`)
      .then((data: Pick<CompareResult, 'namespace' | 'coordinator' | 'retrieval'>) =>
        setIdentity(data),
      )
      .catch((err: Error) => setIdentityError(err.message));
  }, []);

  // runs one group for real and resolves once its own reveal animation
  // has finished, run all below just walks every group through this same
  // function one at a time so a run all lands exactly like pressing
  // every button in order by hand
  const runGroup = (groupId: string): Promise<void> => {
    setCompareError('');
    setRunningGroup(groupId);
    return consoleFetchJSON(`${PROXY_PATH}/spear-shield/demo/sandbox-compare`, 'POST', {
      body: JSON.stringify({ group: groupId }),
    })
      .then((data: CompareResult) => {
        setIdentity({
          namespace: data.namespace,
          coordinator: data.coordinator,
          retrieval: data.retrieval,
        });
        const group = data.groups[0];
        setGroupResults((prev) => ({ ...prev, [groupId]: group }));
        setGroupRevealed((prev) => ({ ...prev, [groupId]: 0 }));
        return new Promise<void>((resolve) => {
          if (group.probes.length === 0) {
            resolve();
            return;
          }
          group.probes.forEach((_probe, i) => {
            const timer = window.setTimeout(
              () => {
                setGroupRevealed((prev) => ({ ...prev, [groupId]: i + 1 }));
                if (i === group.probes.length - 1) {
                  resolve();
                }
              },
              REVEAL_STEP_MS * (i + 1),
            );
            timers.current.push(timer);
          });
        });
      })
      .catch((err: Error) => setCompareError(err.message))
      .finally(() => setRunningGroup(null));
  };

  const runAllGroups = async () => {
    setRunningAll(true);
    setTypedBoth([]);
    for (const def of COMPARE_GROUPS) {
      await runGroup(def.id);
    }
    setRunningAll(false);
  };

  const runTypedBoth = () => {
    const command = typedBothInput.trim();
    if (!command) {
      return;
    }
    setTypedBothBusy(true);
    setTypedBothInput('');
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/demo-exec-both`, 'POST', {
      body: JSON.stringify({ command }),
    })
      .then((data: TypedBoth) => setTypedBoth((prev) => [...prev, data]))
      .catch((err: Error) =>
        setTypedBoth((prev) => [
          ...prev,
          {
            command,
            coordinator_output: `error: ${err.message}`,
            retrieval_output: `error: ${err.message}`,
          },
        ]),
      )
      .finally(() => setTypedBothBusy(false));
  };

  const runScenario = (id: string) => {
    setBusyId(id);
    setErrors((prev) => ({ ...prev, [id]: '' }));
    setScenarioRevealed((prev) => ({ ...prev, [id]: 0 }));
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/demo/${id}`, 'POST', { body: JSON.stringify({}) })
      .then((data: DemoResult) => {
        setResults((prev) => ({ ...prev, [id]: data }));
        data.steps.forEach((_step, i) => {
          const timer = window.setTimeout(
            () => {
              setScenarioRevealed((prev) => ({ ...prev, [id]: i + 1 }));
            },
            REVEAL_STEP_MS * (i + 1),
          );
          timers.current.push(timer);
        });
      })
      .catch((err: Error) => setErrors((prev) => ({ ...prev, [id]: err.message })))
      .finally(() => setBusyId(null));
  };

  // canonical top to bottom order regardless of which button was
  // actually clicked first, a lone group run lands in the same place a
  // run all would have put it
  const groupsToRender = COMPARE_GROUPS.map((def) => ({
    group: groupResults[def.id],
    visibleCount: groupRevealed[def.id] ?? 0,
  })).filter((entry) => entry.group);

  return (
    <>
      <DocumentTitle>{t('Under the Hood')}</DocumentTitle>
      <ListPageHeader title={t('Under the Hood')} />
      <PageSection>
        <Flex direction={{ default: 'column' }} gap={{ default: 'gapLg' }}>
          <FlexItem>
            <Content component="h2" style={{ margin: 0 }}>
              Agent security checks in action
            </Content>
            <Content component="small">
              Probe both agent sandbox containers concurrently live for security checks.
            </Content>
          </FlexItem>

          <FlexItem>
            <Flex gap={{ default: 'gapSm' }} flexWrap={{ default: 'wrap' }}>
              <FlexItem>
                <Button
                  variant="primary"
                  icon={
                    <Icon>
                      <PlayIcon />
                    </Icon>
                  }
                  isLoading={runningAll}
                  isDisabled={runningAll || runningGroup !== null}
                  onClick={runAllGroups}
                >
                  Run all
                </Button>
              </FlexItem>
              {COMPARE_GROUPS.map((def) => (
                <FlexItem key={def.id}>
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={
                      <Icon>
                        <PlayIcon />
                      </Icon>
                    }
                    isLoading={runningGroup === def.id}
                    isDisabled={runningAll || runningGroup !== null}
                    onClick={() => {
                      runGroup(def.id);
                    }}
                  >
                    {def.label}
                  </Button>
                </FlexItem>
              ))}
            </Flex>
          </FlexItem>

          {identityError && (
            <FlexItem>
              <OutputLine output={`error: ${identityError}`} ok={false} />
            </FlexItem>
          )}
          {compareError && (
            <FlexItem>
              <OutputLine output={`error: ${compareError}`} ok={false} />
            </FlexItem>
          )}

          <FlexItem>
            <Grid hasGutter>
              <GridItem span={6}>
                <Flex
                  direction={{ default: 'column' }}
                  gap={{ default: 'gapSm' }}
                  style={{ marginBottom: '0.5rem' }}
                >
                  <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }}>
                    <FlexItem>
                      <Content component="h4" style={{ margin: 0 }}>
                        Coordinator sandbox
                      </Content>
                    </FlexItem>
                    {identity && (
                      <FlexItem>
                        <RuntimeBadge runtimeClass={identity.coordinator.runtime_class} />
                      </FlexItem>
                    )}
                  </Flex>
                </Flex>
                <TerminalWindow title={`bash — ${identity?.coordinator.pod ?? 'coordinator'}`}>
                  {groupsToRender.length === 0 && (
                    <Content component="small" style={{ color: '#6b6f76' }}>
                      press run all, or run a single check above, to execute it against the real
                      cluster
                    </Content>
                  )}
                  {groupsToRender.map(({ group, visibleCount }) =>
                    visibleCount > 0 ? (
                      <div key={group.id}>
                        <GroupDivider label={group.label} />
                        {group.probes.slice(0, visibleCount).map((probe) => (
                          <ProbeBlock
                            key={probe.id}
                            probe={probe}
                            output={probe.coordinator_output}
                          />
                        ))}
                      </div>
                    ) : null,
                  )}
                  {typedBoth.map((exec, i) => (
                    <div key={`typed-c-${i}`}>
                      <PromptLine command={exec.command} />
                      <OutputLine output={exec.coordinator_output} />
                    </div>
                  ))}
                </TerminalWindow>
              </GridItem>

              <GridItem span={6}>
                <Flex
                  direction={{ default: 'column' }}
                  gap={{ default: 'gapSm' }}
                  style={{ marginBottom: '0.5rem' }}
                >
                  <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }}>
                    <FlexItem>
                      <Content component="h4" style={{ margin: 0 }}>
                        Retrieval sandbox
                      </Content>
                    </FlexItem>
                    {identity && (
                      <FlexItem>
                        <RuntimeBadge runtimeClass={identity.retrieval.runtime_class} />
                      </FlexItem>
                    )}
                  </Flex>
                </Flex>
                <TerminalWindow title={`bash — ${identity?.retrieval.pod ?? 'retrieval'}`}>
                  {groupsToRender.length === 0 && (
                    <Content component="small" style={{ color: '#6b6f76' }}>
                      press run all, or run a single check above, to execute it against the real
                      cluster
                    </Content>
                  )}
                  {groupsToRender.map(({ group, visibleCount }) =>
                    visibleCount > 0 ? (
                      <div key={group.id}>
                        <GroupDivider label={group.label} />
                        {group.probes.slice(0, visibleCount).map((probe) => (
                          <ProbeBlock
                            key={probe.id}
                            probe={probe}
                            output={probe.retrieval_output}
                          />
                        ))}
                      </div>
                    ) : null,
                  )}
                  {typedBoth.map((exec, i) => (
                    <div key={`typed-r-${i}`}>
                      <PromptLine command={exec.command} />
                      <OutputLine output={exec.retrieval_output} />
                    </div>
                  ))}
                </TerminalWindow>
              </GridItem>
            </Grid>

            <DualPromptInput
              value={typedBothInput}
              busy={typedBothBusy}
              onChange={setTypedBothInput}
              onSubmit={runTypedBoth}
            />
          </FlexItem>

          <FlexItem>
            <Content component="h3" style={{ marginBottom: '0.25rem' }}>
              Identity and access control
            </Content>
            <Content component="small">
              These two check real human caller identity and per tool rbac, not sandbox runtime
              isolation, so they keep their own single terminal rather than a side by side diff.
            </Content>
          </FlexItem>

          {ACCESS_CONTROL_SCENARIOS.map((scenario) => {
            const result = results[scenario.id];
            const error = errors[scenario.id];
            const revealed = scenarioRevealed[scenario.id] ?? 0;
            const isBusy = busyId === scenario.id;
            const scenarioAllRevealed = result ? revealed >= result.steps.length : false;
            return (
              <FlexItem key={scenario.id}>
                <Flex
                  justifyContent={{ default: 'justifyContentSpaceBetween' }}
                  alignItems={{ default: 'alignItemsFlexStart' }}
                >
                  <FlexItem grow={{ default: 'grow' }}>
                    <Content component="h4" style={{ margin: 0 }}>
                      {scenario.title}
                    </Content>
                    <Content component="small">{scenario.intro}</Content>
                  </FlexItem>
                  <FlexItem>
                    <Button
                      variant="primary"
                      icon={
                        <Icon>
                          <PlayIcon />
                        </Icon>
                      }
                      isLoading={isBusy}
                      isDisabled={busyId !== null}
                      onClick={() => runScenario(scenario.id)}
                    >
                      Run
                    </Button>
                  </FlexItem>
                </Flex>

                <div style={{ marginTop: '0.75rem' }}>
                  <TerminalWindow title={`bash — ${scenario.id}`}>
                    {!result && !error && (
                      <Content component="small" style={{ color: '#6b6f76' }}>
                        press run to execute this against the real cluster
                      </Content>
                    )}
                    {error && <OutputLine output={`error: ${error}`} ok={false} />}
                    {result &&
                      result.steps.slice(0, revealed).map((step, i) => (
                        <div key={i}>
                          <Content
                            component="small"
                            style={{ color: '#9a9ea3', display: 'block', marginBottom: '0.15rem' }}
                          >
                            {`# ${step.label}`}
                          </Content>
                          <PromptLine command={step.command} />
                          <OutputLine output={step.output} ok={step.ok} />
                        </div>
                      ))}
                    {result && scenarioAllRevealed && (
                      <>
                        <PromptLine command="echo $?" />
                        <OutputLine
                          output={result.steps.every((s) => s.ok) ? '0' : '1'}
                          ok={result.steps.every((s) => s.ok)}
                        />
                      </>
                    )}
                  </TerminalWindow>
                </div>

                {result && scenarioAllRevealed && (
                  <Label
                    isCompact
                    icon={
                      <Icon>
                        <TerminalIcon />
                      </Icon>
                    }
                    color={result.steps.every((s) => s.ok) ? 'green' : 'red'}
                    style={{
                      marginTop: '0.5rem',
                      maxWidth: '100%',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {result.steps.every((s) => s.ok)
                      ? scenario.enforcedBy
                      : 'check the output above, a step reported a real failure'}
                  </Label>
                )}
              </FlexItem>
            );
          })}
        </Flex>
      </PageSection>
    </>
  );
}
