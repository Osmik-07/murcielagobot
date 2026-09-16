import { useEffect, useState } from 'react';
import { Badge, Cell, List, Placeholder, Radio, Section, Spinner } from '@telegram-apps/telegram-ui';

import { api, type PriceEntry, type Product } from '../api';
import { approxAmount, discountPercent, months, starsCount } from '../format';
import { PremiumIcon, StarIcon, TonLogo, UsdtLogo } from '../icons';
import { haptic, mainButton } from '../telegram';

const PREMIUM_PERIODS = [3, 6, 12] as const;
const STAR_PRESETS = [50, 100, 500, 1000] as const;
const STARS_MIN = 50;
const STARS_MAX = 100_000;

type PriceMap = Record<number, Record<'ton' | 'usdt_ton', PriceEntry>>;

interface Props {
  product: Product;
  onNext: (choice: { months?: number; stars?: number }) => void;
}

/**
 * Выбор срока (Premium) или количества (Stars).
 *
 * Все цены — с сервера, в реальном времени: у клиента нет ни одной зашитой
 * цифры, потому что цена Fragment меняется.
 */
export function ProductScreen({ product, onNext }: Props) {
  const isPremium = product === 'premium';
  const [selected, setSelected] = useState<number>(isPremium ? 12 : 500);
  const [prices, setPrices] = useState<PriceMap>({});
  const [error, setError] = useState<string | null>(null);

  // Premium: сразу все три срока — они показываются списком и нужны для скидки.
  // Stars: только выбранное количество, вариантов слишком много.
  useEffect(() => {
    let cancelled = false;
    const wanted = isPremium ? [...PREMIUM_PERIODS] : [selected];
    const missing = wanted.filter((v) => !prices[v]);
    if (missing.length === 0) return;

    setError(null);
    Promise.all(
      missing.map((value) =>
        api
          .price(isPremium ? { product, months: value } : { product, stars: value })
          .then((r) => [value, r.prices] as const),
      ),
    )
      .then((entries) => {
        if (!cancelled) setPrices((prev) => ({ ...prev, ...Object.fromEntries(entries) }));
      })
      .catch((e: Error) => !cancelled && setError(e.message));

    return () => {
      cancelled = true;
    };
    // prices намеренно не в зависимостях: иначе каждая загрузка перезапускала бы эффект.
  }, [product, isPremium, selected]);

  const ready = Boolean(prices[selected]);

  useEffect(
    () =>
      mainButton({
        text: 'Продолжить',
        disabled: !ready,
        onClick: () => {
          haptic('tap');
          onNext(isPremium ? { months: selected } : { stars: selected });
        },
      }),
    [ready, selected, isPremium, onNext],
  );

  const choose = (value: number) => {
    haptic('tap');
    setSelected(value);
  };

  if (error) {
    return (
      <List>
        <Placeholder header="Цена недоступна" description={error} />
      </List>
    );
  }

  return (
    <List>
      <Placeholder
        header={isPremium ? 'Telegram Premium' : 'Telegram Stars'}
        description="Подарок напрямую через Fragment"
      >
        {isPremium ? <PremiumIcon size={88} /> : <StarIcon size={88} boxed />}
      </Placeholder>

      {isPremium ? (
        <PremiumPeriods prices={prices} selected={selected} onSelect={choose} />
      ) : (
        <StarsAmount prices={prices} selected={selected} onSelect={choose} />
      )}
    </List>
  );
}

function PremiumPeriods({
  prices,
  selected,
  onSelect,
}: {
  prices: PriceMap;
  selected: number;
  onSelect: (months: number) => void;
}) {
  const baseline = prices[3]?.ton.base;

  return (
    <Section header="Срок подписки" footer="Цена Fragment в реальном времени, с учётом комиссии сервиса.">
      {PREMIUM_PERIODS.map((period) => {
        const entry = prices[period];
        const discount =
          entry && baseline && period !== 3
            ? discountPercent(entry.ton.base, period, baseline, 3)
            : null;

        return (
          <Cell
            key={period}
            Component="label"
            before={
              <Radio
                name="premium-period"
                checked={selected === period}
                onChange={() => onSelect(period)}
              />
            }
            titleBadge={discount ? <Badge type="number">−{discount}%</Badge> : undefined}
            subtitle={
              entry ? `${approxAmount(String(Number(entry.ton.approx) / period), 'ton')} в месяц` : ' '
            }
            after={
              entry ? (
                <div style={{ textAlign: 'right' }}>
                  <div style={{ fontWeight: 600, color: 'var(--tgui--text_color)' }}>
                    {approxAmount(entry.ton.approx, 'ton')}
                  </div>
                  <div style={{ fontSize: 14, color: 'var(--tgui--hint_color)' }}>
                    {approxAmount(entry.usdt_ton.approx, 'usdt_ton')}
                  </div>
                </div>
              ) : (
                <Spinner size="s" />
              )
            }
          >
            {months(period)}
          </Cell>
        );
      })}
    </Section>
  );
}

function StarsAmount({
  prices,
  selected,
  onSelect,
}: {
  prices: PriceMap;
  selected: number;
  onSelect: (stars: number) => void;
}) {
  const step = selected >= 1000 ? 500 : selected >= 200 ? 100 : 50;
  const entry = prices[selected];

  return (
    <>
      <Section footer={`От ${starsCount(STARS_MIN)} до ${starsCount(STARS_MAX)} за один заказ`}>
        <div className="stepper">
          <button
            className="stepper__button"
            aria-label="Меньше"
            disabled={selected <= STARS_MIN}
            onClick={() => onSelect(Math.max(STARS_MIN, selected - step))}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M5 12H19" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
            </svg>
          </button>
          <div className="stepper__value">
            <StarIcon size={30} />
            {starsCount(selected)}
          </div>
          <button
            className="stepper__button"
            aria-label="Больше"
            disabled={selected >= STARS_MAX}
            onClick={() => onSelect(Math.min(STARS_MAX, selected + step))}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M12 5V19M5 12H19" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </Section>

      <Section header="Популярное">
        <div className="presets">
          {STAR_PRESETS.map((preset) => (
            <button
              key={preset}
              className="preset"
              aria-pressed={selected === preset}
              onClick={() => onSelect(preset)}
            >
              {starsCount(preset)}
            </button>
          ))}
        </div>
      </Section>

      <Section header="Стоимость">
        <Cell
          before={<TonLogo size={32} />}
          after={entry ? <b>{approxAmount(entry.ton.approx, 'ton')}</b> : <Spinner size="s" />}
        >
          В TON
        </Cell>
        <Cell
          before={<UsdtLogo size={32} />}
          after={entry ? <b>{approxAmount(entry.usdt_ton.approx, 'usdt_ton')}</b> : <Spinner size="s" />}
        >
          В USDT
        </Cell>
      </Section>
    </>
  );
}
