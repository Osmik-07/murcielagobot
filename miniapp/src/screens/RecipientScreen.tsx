import { useEffect, useState } from 'react';
import { Cell, Input, List, Section } from '@telegram-apps/telegram-ui';

import { PremiumIcon, StarIcon } from '../icons';
import { haptic, mainButton } from '../telegram';

// То же правило, что на сервере (services/miniapp_api.py) и в шлюзе Fragment.
// Разойдутся — примем заказ, который Fragment потом отвергнет уже после оплаты.
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_]{2,30}[A-Za-z0-9]$/;

interface Props {
  title: string;
  premium: boolean;
  onNext: (recipient: string) => void;
}

export function RecipientScreen({ title, premium, onNext }: Props) {
  const [value, setValue] = useState('');
  const clean = value.trim().replace(/^@+/, '');
  const valid = USERNAME_RE.test(clean);
  // Не ругаемся, пока человек не ввёл хоть что-то осмысленное.
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
      <Section header="Заказ">
        <Cell before={premium ? <PremiumIcon size={40} /> : <StarIcon size={40} boxed />}>{title}</Cell>
      </Section>

      <Section
        header="Кому дарим"
        footer={
          showError
            ? 'Латиница, цифры и подчёркивания, от 4 до 32 символов.'
            : 'Username получателя в Telegram. Можно купить и себе.'
        }
      >
        <Input
          before={<span style={{ color: 'var(--tgui--hint_color)', fontSize: 17 }}>@</span>}
          placeholder="username"
          value={value}
          status={showError ? 'error' : 'default'}
          onChange={(e) => setValue(e.target.value)}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="done"
        />
      </Section>
    </List>
  );
}
