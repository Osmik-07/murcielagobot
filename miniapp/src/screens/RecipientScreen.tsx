import { useEffect, useState } from 'react';
import { Input, List, Section } from '@telegram-apps/telegram-ui';

import { haptic, mainButton } from '../telegram';

// То же правило, что на сервере (services/miniapp_api.py) и в шлюзе Fragment.
// Разойдутся — примем заказ, который Fragment потом отвергнет уже после оплаты.
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_]{2,30}[A-Za-z0-9]$/;

interface Props {
  summary: string;
  onNext: (recipient: string) => void;
}

export function RecipientScreen({ summary, onNext }: Props) {
  const [value, setValue] = useState('');
  const clean = value.trim().replace(/^@+/, '');
  const valid = USERNAME_RE.test(clean);
  // Не ругаемся, пока человек не начал печатать что-то осмысленное.
  const showError = clean.length >= 3 && !valid;

  useEffect(
    () =>
      mainButton({
        text: 'Продолжить',
        disabled: !valid,
        onClick: () => {
          haptic('tap');
          onNext(clean);
        },
      }),
    [valid, clean, onNext],
  );

  return (
    <List>
      <div style={{ padding: '20px 22px 4px', fontSize: 22, fontWeight: 700 }}>Кому дарим?</div>
      <div style={{ padding: '0 22px 8px', fontSize: 15, opacity: 0.6 }}>{summary}</div>

      <Section
        footer={
          showError
            ? 'Латиница, цифры и подчёркивания, от 4 до 32 символов. Например: durov'
            : 'Username получателя в Telegram, без @'
        }
      >
        <Input
          header="Получатель"
          placeholder="durov"
          value={value}
          status={showError ? 'error' : undefined}
          onChange={(e) => setValue(e.target.value)}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      </Section>
    </List>
  );
}
