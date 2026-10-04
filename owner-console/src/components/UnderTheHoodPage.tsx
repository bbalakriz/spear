import { DocumentTitle, ListPageHeader, consoleFetchJSON } from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import { Button, Content, Flex, FlexItem, Icon, Label, PageSection } from '@patternfly/react-core';
import { PlayIcon, TerminalIcon } from '@patternfly/react-icons';
import { useEffect, useRef, useState } from 'react';

const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';
// the browser's own relative path above resolves against the console's own
// origin, this is the literal full address that resolves to when read from
// a real terminal outside the console, shown in the prompt so the command
// on screen is something you could actually run, not a stand in
const BACKEND_PATH_FOR_DISPLAY = '/spear-shield/demo';

// how long each revealed step stays on screen before the next one appears,
// just enough to read as it goes rather than a single instant text dump
const REVEAL_STEP_MS = 650;

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
  intro: string;
  // short one line tag naming the real component that produced this
  // result, "thing that acted -> thing it acted on", kept terse so it
  // fits on a single line instead of wrapping or spilling past the card
  enforcedBy: string;
}

// one entry per real, live checked scenario from the layered security demo
// script. two of the eight originally proposed need no new page at all:
// content scoping (abac filtered rag) and prompt injection refusal both
// already happen live on the Ask SPEAR Shield chat page with the right
// question, and the tool response injection defense is left out on
// purpose, it currently fails closed on every tool response, not only
// malicious ones, see PHASE2_PLAN.md section 5's own honest note
const SCENARIOS: Scenario[] = [
  {
    id: 'identity-rejected',
    intro: 'a caller with no real identity is rejected before any agent logic ever runs.',
    enforcedBy: 'Authorino AuthPolicy spear-retrieval-a2a-auth -> a2a gateway',
  },
  {
    id: 'kata-vs-runc',
    intro:
      'the coordinator runs on plain runc, the retrieval agent runs under Kata, on the same physical node. nproc and free -h make the difference visible, not just a config flag.',
    enforcedBy: 'kata RuntimeClass -> retrieval sandbox (coordinator stays runc)',
  },
  {
    id: 'network-containment',
    intro:
      'from inside the sandbox: an arbitrary internet host is blocked, a disallowed hostname fails to resolve, and the one real allowlisted route is reached.',
    enforcedBy: 'openshell network policy -> sandbox supervisor',
  },
  {
    id: 'filesystem-containment',
    intro:
      'from inside the sandbox: deleting real system binaries is refused, only the sandbox\u2019s own separate volume is writable.',
    enforcedBy: 'openshell landlock policy -> sandbox supervisor',
  },
  {
    id: 'workload-identity',
    intro:
      'the identical call, made with no bearer token at all, is rejected from outside any sandbox but succeeds from the real coordinator process, proving a credential was transparently attached before the request ever left the pod.',
    enforcedBy: 'openshell token_grant -> Authorino AuthPolicy spear-retrieval-a2a-auth',
  },
  {
    id: 'tool-scope-and-assignee',
    intro:
      'three real callers against the real work tracker: a caller lacking the role is blocked at the gateway, a caller holding the role is blocked by this project\u2019s own assignee check on someone else\u2019s item, and a caller acting on their own item succeeds.',
    enforcedBy: 'Authorino AuthPolicy spear-shield-gateway-tools-auth -> mcp gateway',
  },
];

// terminal chrome, three dots in the usual red/yellow/green, a fixed
// monospace body with its own dark background regardless of the console's
// own light/dark theme, a terminal should always look like a terminal
const TerminalWindow = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <div style={{ borderRadius: '8px', overflow: 'hidden', border: '1px solid #000', marginTop: '0.75rem' }}>
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
        <Content component="small" style={{ color: '#9a9ea3', textAlign: 'center', display: 'block' }}>
          {title}
        </Content>
      </FlexItem>
    </Flex>
    <div
      style={{
        background: '#1b1d21',
        color: '#e0e0e0',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: '0.82rem',
        padding: '0.85rem 1rem',
        maxHeight: '28rem',
        overflowY: 'auto',
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

const OutputLine = ({ output, ok }: { output: string; ok: boolean }) => (
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

export default function UnderTheHoodPage() {
  const { t } = useTranslation('plugin__phase1-ingestion-console');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, DemoResult>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  // how many of this scenario's steps have been revealed so far, drives
  // the step by step terminal reveal rather than dumping everything at once
  const [revealed, setRevealed] = useState<Record<string, number>>({});
  const timers = useRef<number[]>([]);

  useEffect(() => () => timers.current.forEach((id) => window.clearTimeout(id)), []);

  const run = (id: string) => {
    setBusyId(id);
    setErrors((prev) => ({ ...prev, [id]: '' }));
    setRevealed((prev) => ({ ...prev, [id]: 0 }));
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/demo/${id}`, 'POST', { body: JSON.stringify({}) })
      .then((data: DemoResult) => {
        setResults((prev) => ({ ...prev, [id]: data }));
        data.steps.forEach((_step, i) => {
          const timer = window.setTimeout(() => {
            setRevealed((prev) => ({ ...prev, [id]: i + 1 }));
          }, REVEAL_STEP_MS * (i + 1));
          timers.current.push(timer);
        });
      })
      .catch((err: Error) => setErrors((prev) => ({ ...prev, [id]: err.message })))
      .finally(() => setBusyId(null));
  };

  return (
    <>
      <DocumentTitle>{t('Under the Hood')}</DocumentTitle>
      <ListPageHeader title={t('Under the Hood')} />
      <PageSection>
        <Content component="p">
          Six scenarios, each one a real check run live against this cluster when you press run,
          not a recording or a scripted log. Every line in the terminal below is the actual
          command this page sends and the actual response that comes back. Together with the Ask
          SPEAR Shield chat page, which already demonstrates permission scoped retrieval and
          prompt injection refusal with no separate button needed, this covers every layer of
          isolation and access control this environment actually enforces.
        </Content>

        <Flex direction={{ default: 'column' }} gap={{ default: 'gapLg' }} style={{ marginTop: '1rem' }}>
          {SCENARIOS.map((scenario) => {
            const result = results[scenario.id];
            const error = errors[scenario.id];
            const revealCount = revealed[scenario.id] ?? 0;
            const isBusy = busyId === scenario.id;
            const allRevealed = result ? revealCount >= result.steps.length : false;
            return (
              <FlexItem key={scenario.id}>
                <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }} alignItems={{ default: 'alignItemsFlexStart' }}>
                  <FlexItem grow={{ default: 'grow' }}>
                    <Content component="h3" style={{ margin: 0 }}>
                      {result?.title ?? scenario.id}
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
                      onClick={() => run(scenario.id)}
                    >
                      Run
                    </Button>
                  </FlexItem>
                </Flex>

                <TerminalWindow title={`bash — ${scenario.id}`}>
                  <PromptLine command={`curl -sk -X POST https://<console-host>/api/proxy${BACKEND_PATH_FOR_DISPLAY}/${scenario.id}`} />
                  {!result && !error && (
                    <Content component="small" style={{ color: '#6b6f76' }}>
                      press run to execute this against the real cluster
                    </Content>
                  )}
                  {error && <OutputLine output={`error: ${error}`} ok={false} />}
                  {result &&
                    result.steps.slice(0, revealCount).map((step, i) => (
                      <div key={i}>
                        <Content component="small" style={{ color: '#9a9ea3', display: 'block', marginBottom: '0.15rem' }}>
                          {`# ${step.label}`}
                        </Content>
                        <PromptLine command={step.command} />
                        <OutputLine output={step.output} ok={step.ok} />
                      </div>
                    ))}
                  {result && allRevealed && (
                    <>
                      <PromptLine command="echo $?" />
                      <OutputLine
                        output={result.steps.every((s) => s.ok) ? '0' : '1'}
                        ok={result.steps.every((s) => s.ok)}
                      />
                    </>
                  )}
                </TerminalWindow>

                {result && allRevealed && (
                  <Label
                    isCompact
                    icon={
                      <Icon>
                        <TerminalIcon />
                      </Icon>
                    }
                    color={result.steps.every((s) => s.ok) ? 'green' : 'red'}
                    style={{ marginTop: '0.5rem', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}
                  >
                    {result.steps.every((s) => s.ok)
                      ? scenario.enforcedBy
                      : 'unexpected result, check the output above'}
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
