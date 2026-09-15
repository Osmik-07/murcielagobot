import { useEffect, useState } from 'react';
import { Cell, List, Placeholder, Section, Spinner } from '@telegram-apps/telegram-ui';

import { api, type Order } from '../api';
import { CheckIcon, PremiumIcon, StarIcon } from '../icons';
import { haptic, mainButton, tg } from '../telegram';

const POLL_INTERVAL_MS = 3000;
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
        if (order.status === 'fulfilled') onRestart();
        else tg()?.close();
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
          {view.spinner ? <Spinner size="l" /> : <CheckIcon />}
        </div>
      </Placeholder>

      <Section>
        <Cell
          before={order.product === 'premium' ? <PremiumIcon size={24} /> : <StarIcon size={24} boxed />}
          subtitle={`для @${order.recipient}`}
          after={<span style={{ fontSize: 13, opacity: 0.6 }}>{order.amount_formatted}</span>}
        >
          {order.title}
        </Cell>
      </Section>
    </List>
  );
}

const STATUS_VIEW: Record<
  Order['status'],
  { title: string; description: string; tint: string; spinner: boolean }
> = {
  pending: {
    title: 'Ждём оплату',
    description: 'Как только перевод придёт, заказ выполнится автоматически.',
    tint: 'var(--tgui--secondary_bg_color)',
    spinner: true,
  },
  paid: {
    title: 'Оплата получена',
    description: 'Покупаем на Fragment — это займёт несколько секунд.',
    tint: 'var(--tgui--secondary_bg_color)',
    spinner: true,
  },
  fulfilling: {
    title: 'Выполняем заказ',
    description: 'Уже покупаем. Ничего нажимать не нужно.',
    tint: 'var(--tgui--secondary_bg_color)',
    spinner: true,
  },
  fulfilled: {
    title: 'Готово!',
    description: 'Подарок уже у получателя, ему ушло уведомление.',
    tint: 'var(--tgui--link_color)',
    spinner: false,
  },
  failed: {
    title: 'Нужен ручной разбор',
    description: 'Оплата получена, но выдать не смогли. Мы уже видим это и решим вопрос.',
    tint: 'var(--tgui--destructive_text_color)',
    spinner: false,
  },
  expired: {
    title: 'Срок оплаты истёк',
    description: 'Счёт больше не действителен. Если ты всё же оплатил — напиши нам.',
    tint: 'var(--tgui--secondary_bg_color)',
    spinner: false,
  },
};
