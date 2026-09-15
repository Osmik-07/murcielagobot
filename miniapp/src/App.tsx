import { useCallback, useEffect, useState } from 'react';
import { Cell, List, Section, Spinner } from '@telegram-apps/telegram-ui';

import { api, type Order, type Product } from './api';
import { PremiumIcon, StarIcon } from './icons';
import { ProductScreen } from './screens/ProductScreen';
import { RecipientScreen } from './screens/RecipientScreen';
import { PaymentScreen } from './screens/PaymentScreen';
import { StatusScreen } from './screens/StatusScreen';
import { backButton, haptic, startProduct } from './telegram';

type Step =
  | { name: 'loading' }
  | { name: 'pick-product' }
  | { name: 'product'; product: Product }
  | { name: 'recipient'; product: Product; months?: number; stars?: number }
  | { name: 'payment'; product: Product; months?: number; stars?: number; recipient: string }
  | { name: 'status'; order: Order };

/**
 * Поток — линейный мастер, поэтому состояние экрана хранится обычным
 * useState, без роутера: у Mini App нет адресной строки, а «назад» — это
 * нативная кнопка Telegram, которую мы ведём сами.
 */
export function App() {
  const [step, setStep] = useState<Step>({ name: 'loading' });

  // При открытии проверяем, нет ли у человека незакрытого заказа: если есть,
  // сразу показываем его статус, а не заставляем оформлять заново.
  useEffect(() => {
    let cancelled = false;
    api
      .activeOrder()
      .then(({ order }) => {
        if (cancelled) return;
        if (order) {
          setStep({ name: 'status', order });
          return;
        }
        const product = startProduct();
        setStep(product ? { name: 'product', product } : { name: 'pick-product' });
      })
      .catch(() => {
        if (cancelled) return;
        const product = startProduct();
        setStep(product ? { name: 'product', product } : { name: 'pick-product' });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const goBack = useCallback(() => {
    haptic('tap');
    setStep((current) => {
      switch (current.name) {
        case 'recipient':
          return { name: 'product', product: current.product };
        case 'payment':
          return {
            name: 'recipient',
            product: current.product,
            months: current.months,
            stars: current.stars,
          };
        default:
          return current;
      }
    });
  }, []);

  // Нативная стрелка «назад» показывается только там, где есть куда вернуться.
  useEffect(() => {
    if (step.name !== 'recipient' && step.name !== 'payment') return;
    return backButton(goBack);
  }, [step.name, goBack]);

  const restart = useCallback(() => {
    const product = startProduct();
    setStep(product ? { name: 'product', product } : { name: 'pick-product' });
  }, []);

  switch (step.name) {
    case 'loading':
      return (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
          <Spinner size="l" />
        </div>
      );

    case 'pick-product':
      return (
        <List>
          <div style={{ padding: '24px 22px 6px', fontSize: 22, fontWeight: 700 }}>Что покупаем?</div>
          <div style={{ padding: '0 22px 8px', fontSize: 15, opacity: 0.6 }}>
            Оплата в TON или USDT напрямую, без посредников.
          </div>
          <Section>
            <Cell
              before={<PremiumIcon />}
              subtitle="Подписка на 3, 6 или 12 месяцев"
              onClick={() => setStep({ name: 'product', product: 'premium' })}
            >
              Telegram Premium
            </Cell>
            <Cell
              before={<StarIcon size={32} boxed />}
              subtitle="От 50 штук, любому получателю"
              onClick={() => setStep({ name: 'product', product: 'stars' })}
            >
              Telegram Stars
            </Cell>
          </Section>
        </List>
      );

    case 'product':
      return (
        <ProductScreen
          product={step.product}
          onNext={(choice) =>
            setStep({
              name: 'recipient',
              product: step.product,
              months: choice.months,
              stars: choice.stars,
            })
          }
        />
      );

    case 'recipient':
      return (
        <RecipientScreen
          summary={
            step.product === 'premium' ? `Premium на ${step.months} мес.` : `${step.stars} Stars`
          }
          onNext={(recipient) =>
            setStep({
              name: 'payment',
              product: step.product,
              months: step.months,
              stars: step.stars,
              recipient,
            })
          }
        />
      );

    case 'payment':
      return (
        <PaymentScreen
          product={step.product}
          months={step.months}
          stars={step.stars}
          recipient={step.recipient}
          onPaid={(order) => setStep({ name: 'status', order })}
        />
      );

    case 'status':
      return <StatusScreen order={step.order} onRestart={restart} />;
  }
}
