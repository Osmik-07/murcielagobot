import { useEffect, useState } from 'react';
import { Cell, List, Placeholder, Section, Spinner } from '@telegram-apps/telegram-ui';

import { api, type Order } from '../api';
import { CheckIcon, PremiumIcon, StarIcon } from '../icons';
import { exactAmount } from '../format';
import { haptic, mainButton } from '../telegram';

const POLL_INTERVAL_MS = 3000;

/**
 * У каждого исхода — своя иконка. Галочка только у реального успеха:
 * нарисовать её на проваленном или просроченном заказе значит сказать
 * человеку «всё хорошо», когда его деньги требуют разбора.
 */
const STATUS_ICON = {
  spinner: <Spinner size="l" />,
  check: <CheckIcon />,
  alert: (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none">
      <path d="M12 7v6" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="12" cy="17" r="1.5" fill="#fff" />
    </svg>
  ),
  clock: (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" style={{ color: 'var(--tgui--hint_color)' }}>
      <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="2" />
      <path d="M12 7.5V12l3 2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  ),
};
const TERMINAL = new Set(['fulfilled', 'failed', 'expired']);

interface Props {
  order: Order;
  onRestart: () => void;
}

/**
 * Статус заказа: опрашиваем сервер, пока он не придёт в терминальное
 * состояние. Клиент здесь ничего не решает — только показывает то, что
 * сервер уже установил по данным TonConsole.
 */
export function StatusScreen({ order: initial, onRestart }: Props) {
  const [order, setOrder] = useState<Order>(initial);
  const done = TERMINAL.has(order.status);

  useEffect(() => {
    if (done) return;
    let cancelled = false;

    const timer = setInterval(async () => {
      try {
        const { order: fresh } = await api.order(order.id);
        if (cancelled) return;
        setOrder((prev) => {
          if (fresh.status !== prev.status && TERMINAL.has(fresh.status)) {
            haptic(fresh.status === 'fulfilled' ? 'success' : 'error');
          }
          return fresh;
        });
      } catch {
        // Сеть моргнула — просто ждём следующего круга, экран не роняем.
      }
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [order.id, done]);

  useEffect(() => {
    if (!done) return;
    return mainButton({
      text: order.status === 'fulfilled' ? 'Купить ещё' : 'Начать заново',
      onClick: () => {
        haptic('tap');
        onRestart();
      },
    });
  }, [done, order.status, onRestart]);

  const view = STATUS_VIEW[order.status];

  return (
    <List>
      <Placeholder header={view.title} description={view.description}>
        <div
          style={{
            width: 88,
            height: 88,
            borderRadius: '50%',
            background: view.tint,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {STATUS_ICON[view.icon]}
        </div>
      </Placeholder>

      <Section>
        <Cell
          before={order.product === 'premium' ? <PremiumIcon size={40} /> : <StarIcon size={40} boxed />}
          subtitle={`для @${order.recipient}`}
          after={
            <span style={{ color: 'var(--tgui--hint_color)' }}>{exactAmount(order.amount, order.asset)}</span>
          }
        >
          {order.title}
        </Cell>
      </Section>
    </List>
  );
}

const STATUS_VIEW: Record<
  Order['status'],
  { title: string; description: string; tint: string; icon: keyof typeof STATUS_ICON }
> = {
  pending: {
    title: 'Ждём оплату',
    description: 'Как только перевод придёт, заказ выполнится автоматически.',
    tint: 'var(--tgui--section_bg_color)',
    icon: 'spinner',
  },
  paid: {
    title: 'Оплата получена',
    description: 'Покупаем на Fragment — это займёт несколько секунд.',
    tint: 'var(--tgui--section_bg_color)',
    icon: 'spinner',
  },
  fulfilling: {
    title: 'Выполняем заказ',
    description: 'Уже покупаем. Ничего нажимать не нужно.',
    tint: 'var(--tgui--section_bg_color)',
    icon: 'spinner',
  },
  fulfilled: {
    title: 'Готово!',
    description: 'Подарок уже у получателя, ему ушло уведомление.',
    tint: 'var(--tgui--link_color)',
    icon: 'check',
  },
  failed: {
    title: 'Нужен ручной разбор',
    description: 'Оплата получена, но выдать не смогли. Мы уже видим это и решим вопрос.',
    tint: 'var(--tgui--destructive_text_color)',
    icon: 'alert',
  },
  expired: {
    title: 'Срок оплаты истёк',
    description: 'Счёт больше не действителен. Если ты всё же оплатил — напиши нам.',
    tint: 'var(--tgui--section_bg_color)',
    icon: 'clock',
  },
};
