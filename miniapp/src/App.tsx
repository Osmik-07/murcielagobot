import { useCallback, useEffect, useState } from 'react';
import { Cell, List, Placeholder, Section, Spinner } from '@telegram-apps/telegram-ui';

import { api, type Order, type Product } from './api';
import { months, starsCount } from './format';
import { PremiumIcon, StarIcon } from './icons';
import { PaymentScreen } from './screens/PaymentScreen';
import { ProductScreen } from './screens/ProductScreen';
import { RecipientScreen } from './screens/RecipientScreen';
import { StatusScreen } from './screens/StatusScreen';
import { backButton, haptic, startProduct } from './telegram';

type Step =
  | { name: 'loading' }
  | { name: 'pick-product' }
  | { name: 'product'; product: Product }
  | { name: 'recipient'; product: Product; months?: number; stars?: number }
  | { name: 'payment'; product: Product; months?: number; stars?: number; recipient: string }
  | { name: 'status'; order: Order };

const firstStep = (): Step => {
  const product = startProduct();
  return product ? { name: 'product', product } : { name: 'pick-product' };
};

/**
 * Поток — линейный мастер, поэтому экран хранится обычным useState, без
 * роутера: адресной строки у Mini App нет, а «назад» — нативная кнопка Telegram.
 */
export function App() {
  const [step, setStep] = useState<Step>({ name: 'loading' });

  // Сразу на статус ведём только заказ, который уже оплачен и выполняется.
  // Неоплаченный (pending) — нет: со статуса «ждём оплату» оплатить нельзя, и
  // человек застрял бы на спиннере. Он пройдёт поток заново, а сервер сам
  // переиспользует тот же счёт или погасит его, если параметры поменялись.
  useEffect(() => {
    let cancelled = false;
    api
      .activeOrder()
      .then(({ order }) => {
        if (cancelled) return;
        setStep(order && order.status !== 'pending' ? { name: 'status', order } : firstStep());
      })
      .catch(() => !cancelled && setStep(firstStep()));
    return () => {
      cancelled = true;
    };
  }, []);

  const goBack = useCallback(() => {
    haptic('tap');
    setStep((current) => {
      switch (current.name) {
        case 'product':
          return startProduct() ? current : { name: 'pick-product' };
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

  // Стрелка «назад» только там, где есть куда вернуться.
  const canGoBack =
    step.name === 'recipient' ||
    step.name === 'payment' ||
    (step.name === 'product' && !startProduct());
  useEffect(() => (canGoBack ? backButton(goBack) : undefined), [canGoBack, goBack]);

  const restart = useCallback(() => setStep(firstStep()), []);

  switch (step.name) {
    case 'loading':
      return (
        <Placeholder>
          <Spinner size="l" />
        </Placeholder>
      );

    case 'pick-product':
      return (
        <List>
          <Placeholder header="Что покупаем?" description="Оплата в TON или USDT напрямую, без посредников" />
          <Section>
            <Cell
              before={<PremiumIcon size={40} />}
              subtitle="На 3, 6 или 12 месяцев"
              onClick={() => setStep({ name: 'product', product: 'premium' })}
            >
              Telegram Premium
            </Cell>
            <Cell
              before={<StarIcon size={40} boxed />}
              subtitle="От 50 штук любому получателю"
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
            setStep({ name: 'recipient', product: step.product, months: choice.months, stars: choice.stars })
          }
        />
      );

    case 'recipient':
      return (
        <RecipientScreen
          premium={step.product === 'premium'}
          title={
            step.product === 'premium'
              ? `Telegram Premium · ${months(step.months!)}`
              : `${starsCount(step.stars!)} Telegram Stars`
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
