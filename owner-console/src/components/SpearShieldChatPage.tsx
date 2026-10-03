import { DocumentTitle, ListPageHeader, consoleFetchJSON } from '@openshift-console/dynamic-plugin-sdk';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Card,
  CardBody,
  Content,
  Flex,
  FlexItem,
  PageSection,
  Spinner,
  TextArea,
} from '@patternfly/react-core';
import { useRef, useState } from 'react';

// PHASE2_PLAN.md section 8: no persona picker here, this page always asks
// as whoever is actually signed into the console right now. the backend
// resolves that real identity from the forwarded token itself, this page
// never sends a username anywhere, see owner-console-backend/server.py's
// ask_spear_shield.
const PROXY_PATH = '/api/proxy/plugin/phase1-ingestion-console/backend';

interface ChatTurn {
  question: string;
  answer?: string;
  error?: string;
}

export default function SpearShieldChatPage() {
  const { t } = useTranslation('plugin__phase1-ingestion-console');
  const [draft, setDraft] = useState('');
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [busy, setBusy] = useState(false);
  // scrolls the latest turn into view once it lands, a chat page reads
  // oddest when a new answer appears below the fold
  const bottomRef = useRef<HTMLDivElement>(null);

  const ask = () => {
    const question = draft.trim();
    if (!question || busy) return;
    setDraft('');
    setBusy(true);
    const turnIndex = turns.length;
    setTurns((prev) => [...prev, { question }]);
    consoleFetchJSON(`${PROXY_PATH}/spear-shield/ask`, 'POST', {
      body: JSON.stringify({ message: question }),
    })
      .then((data: { answer: string }) => {
        setTurns((prev) =>
          prev.map((turn, i) => (i === turnIndex ? { ...turn, answer: data.answer } : turn)),
        );
      })
      .catch((err: Error) => {
        setTurns((prev) =>
          prev.map((turn, i) => (i === turnIndex ? { ...turn, error: err.message } : turn)),
        );
      })
      .finally(() => {
        setBusy(false);
        setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
      });
  };

  return (
    <>
      <DocumentTitle>{t('Ask SPEAR Shield')}</DocumentTitle>
      <ListPageHeader title={t('Ask SPEAR Shield')} />
      <PageSection>
        <Content component="p">
          ask a question against this project{'\u2019'}s real knowledge base, answered as you, the
          signed in console user. every scoping rule and guardrails check the rest of this project
          builds still applies here, this page is just a thin front end on top of
          spear-coordinator-agent{'\u2019'}s own a2a endpoint.
        </Content>

        <Card style={{ marginTop: '1rem' }}>
          <CardBody>
            {turns.length === 0 && (
              <Content component="small">no questions asked yet in this session</Content>
            )}
            <Flex direction={{ default: 'column' }} gap={{ default: 'gapMd' }}>
              {turns.map((turn, i) => (
                <FlexItem key={i}>
                  <Content component="p" style={{ fontWeight: 'bold', marginBottom: '0.25rem' }}>
                    {turn.question}
                  </Content>
                  {turn.error && (
                    <Alert variant="danger" title="spear shield could not answer this" isInline>
                      {turn.error}
                    </Alert>
                  )}
                  {!turn.error && turn.answer === undefined && <Spinner size="md" />}
                  {!turn.error && turn.answer !== undefined && (
                    <Content component="p" style={{ whiteSpace: 'pre-wrap' }}>
                      {turn.answer}
                    </Content>
                  )}
                </FlexItem>
              ))}
              <div ref={bottomRef} />
            </Flex>
          </CardBody>
        </Card>

        <Flex
          alignItems={{ default: 'alignItemsFlexEnd' }}
          gap={{ default: 'gapMd' }}
          style={{ marginTop: '1rem' }}
        >
          <FlexItem grow={{ default: 'grow' }}>
            <TextArea
              aria-label="question for spear shield"
              value={draft}
              onChange={(_e, value) => setDraft(value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  ask();
                }
              }}
              rows={2}
              placeholder="ask about work packages, policy documents, anything this project's knowledge base covers"
            />
          </FlexItem>
          <FlexItem>
            <Button variant="primary" isLoading={busy} isDisabled={busy || !draft.trim()} onClick={ask}>
              Ask
            </Button>
          </FlexItem>
        </Flex>
      </PageSection>
    </>
  );
}
